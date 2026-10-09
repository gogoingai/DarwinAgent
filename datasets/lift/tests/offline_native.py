"""Recorded-response native pipeline acceptance; Docker and Langfuse are real, scores synthetic."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from datasets.lift.register import register, upstream


class RecordedClient:
    def __init__(self, cfg, initial):
        self.cfg, self.initial, self.calls = cfg, initial, 0
        self.failure_injection = False
        self.fail_all_holdout = False

    def http_attempts(self):
        return 0

    async def chat(self, **kwargs):
        self.calls += 1
        role = kwargs["role"]
        if role == "proposal":
            assets = [a.to_dict() for a in self.initial.assets.assets]
            for a in assets:
                if a["kind"] == "F":
                    a["description"] = "Read rules and table together"
                    a["content"] = (
                        'def run(params):\n    rules = call("read_text", {"path":params["rules"]})\n    rows = call("read_table", {"path":params["table"]})\n    return {"rules":rules,"rows":rows}\n'
                    )
                    a["trial_inputs"] = (
                        {"rules": "metric_definitions.txt", "table": "sales_daily.xlsx"},
                    )
            answer = {"action": "submit_patch", "patches": {"assets": assets}}
        elif role in {"worker", "judge"}:
            request = json.loads(kwargs["messages"][1]["content"])
            task = request["task"]
            task_id = task["id"]
            last_message = task["parameters"]["native_messages"][-1]["content"]
            steps = sum(m["role"] == "assistant" for m in kwargs["messages"])
            artifact = "result/result_" + task_id + "/report.md"
            if role == "judge":
                if not steps:
                    answer = {"tool": "read_text", "params": {"path": artifact}}
                else:
                    observation = json.loads(kwargs["messages"][-1]["content"])["observation"]
                    correct = "OFFLINE-CORRECTED" in observation.get("content", "")
                    answer = {
                        "final_response": json.dumps(
                            {
                                "success": correct,
                                "score": 1.0 if correct else 0.5,
                                "reason": ""
                                if correct
                                else "Please include the corrected weekly total.",
                            }
                        ),
                        "artifacts": [],
                    }
            else:
                candidate = any(
                    t.get("description") == "Read rules and table together"
                    for t in request["tools"]
                )
                correction = "corrected weekly total" in last_message
                rules = task_id + "_materials/metric_definitions.txt"
                table = (
                    task_id
                    + "_materials/"
                    + ("orders_detail.xlsx" if task_id == "q2" else "sales_daily.xlsx")
                )
                if correction:
                    actions = [
                        {
                            "tool": "write_text",
                            "params": {"path": artifact, "content": "OFFLINE-CORRECTED"},
                        }
                    ]
                elif candidate:
                    actions = [
                        {"tool": "inspect_materials", "params": {"rules": rules, "table": table}},
                        {
                            "tool": "write_text",
                            "params": {"path": artifact, "content": "OFFLINE-CORRECTED"},
                        },
                    ]
                else:
                    actions = [
                        {"tool": "read_text", "params": {"path": rules}},
                        {"tool": "read_table", "params": {"path": table}},
                        {
                            "tool": "write_text",
                            "params": {"path": artifact, "content": "OFFLINE-INITIAL"},
                        },
                    ]
                answer = (
                    actions[steps]
                    if steps < len(actions)
                    else {"final_response": "Saved " + artifact, "artifacts": [{"path": artifact}]}
                )
        else:
            raise AssertionError("Unexpected offline role: " + role)
        if (
            self.failure_injection
            and role == "worker"
            and (
                (task_id == "q1" and correction)
                or (
                    task_id == "q5"
                    and (
                        self.fail_all_holdout
                        or not any(t["name"] == "inspect_materials" for t in request["tools"])
                    )
                )
            )
        ):
            answer = (
                {"tool": "write_text", "params": {"path": artifact, "content": "PARTIAL-FAILURE"}}
                if steps == 0
                else {"tool": "list_files", "params": {}}
            )
        return SimpleNamespace(
            content=json.dumps(answer),
            usage={"prompt_tokens": 20, "completion_tokens": 10},
            model="offline-recorded",
            elapsed_s=0.0,
        )

    async def aclose(self):
        pass


async def run(args):
    if args.trace_env:
        from dotenv import dotenv_values

        values = dotenv_values(args.trace_env)
        os.environ["LANGFUSE_PUBLIC_KEY"] = values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
        os.environ["LANGFUSE_SECRET_KEY"] = values["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
        os.environ["LANGFUSE_BASE_URL"] = "http://127.0.0.1:13000"
        os.environ["EVAL_LANGFUSE_PRE_CHAT"] = "1"
    upstream(args.upstream)
    holder = {}
    register(lambda options: holder["adapter"])
    from src.lift.pipeline.lift_pipeline import LIFTPipeline
    from src.lift.pipeline.run_options import RunOptions

    from darwinagent.config import Config
    from darwinagent.runtime.artifacts import atomic_json
    from datasets.lift.adapter import DarwinLiftAdapter
    from datasets.lift.data import LiftDataset
    from datasets.lift.kernel import initial_kernel
    from datasets.lift.report import report
    from datasets.lift.transport import SharedTransport

    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    dataset = LiftDataset(args.data)
    dataset.write_suite(root / "suite.json")
    initial = initial_kernel(root / "K0")
    cfg = Config(
        model_strong="offline-recorded",
        model_middle="offline-recorded",
        model_fast="offline-recorded",
        work_dir=root / "transport",
        max_retries=1,
        max_http_requests=100,
    )
    client = RecordedClient(cfg, initial)
    client.failure_injection = args.failure_injection
    client.fail_all_holdout = args.fail_all_holdout
    cfg.role_tiers.update(worker="strong", judge="strong")
    shared = SharedTransport(client, root / "shared")
    options = RunOptions(
        evaluate=False,
        repeat=1,
        warmup_container_policy="serial_single",
        holdout_container_policy="serial_multi",
        holdout_phase_policy="serial",
        max_parallel_suites=1,
        max_concurrent_tasks=1,
        max_conversation_turns=3,
    )

    class ProbeAdapter(DarwinLiftAdapter):
        async def evolve_after_task(self, env, task, result, ctx):
            from src.lift.adapters.container.exec import docker_exec_async

            from datasets.lift.tools import DockerTools

            tools = DockerTools(env.handle.container_name, env.workspace_dir)
            try:
                await tools.read_text({"path": "/opt/darwin-state/accepted.json"})
            except ValueError:
                pass
            else:
                raise AssertionError("Workspace boundary escaped")
            readonly = await docker_exec_async(
                env.judge_handle.container_name,
                [
                    "python",
                    "/opt/darwin-tools/toolbox.py",
                    json.dumps(
                        {
                            "name": "write_text",
                            "params": {"path": "judge-write-denied.txt", "content": "denied"},
                        }
                    ),
                ],
            )
            assert not json.loads(readonly)["ok"]
            files = (await tools.list_files({}))["files"]
            assert all(not any(x in name for x in ["q2", "q5", "q6", ".md.md"]) for name in files)
            await super().evolve_after_task(env, task, result, ctx)
            hook = self.root / "hooks" / ("task-" + task.name + ".json")
            before = hook.stat().st_mtime_ns
            await super().evolve_after_task(env, task, result, ctx)
            assert hook.stat().st_mtime_ns == before

        async def evolve_after_warmup(self, env, ctx):
            from src.lift.adapters.container.exec import docker_exec_async

            await super().evolve_after_warmup(env, ctx)
            command = [
                "python",
                "-c",
                "import os; print(os.stat('/opt/darwin-state/accepted.json').st_mtime_ns)",
            ]
            first = await docker_exec_async(env.handle.container_name, command)
            calls = self.shared.client.calls
            await super().evolve_after_warmup(env, ctx)
            second = await docker_exec_async(env.handle.container_name, command)
            assert first == second and calls == self.shared.client.calls
            atomic_json(
                self.root / "hook-idempotency.json",
                {"container_state_mtime_ns": first.strip(), "repeated_hook_calls": 0},
            )

    adapter = ProbeAdapter(
        options, dataset=dataset, shared=shared, root=root, initial=initial, trace=args.trace
    )
    holder["adapter"] = adapter
    os.chdir(root)
    native = await LIFTPipeline().run(
        run_id="darwin-" + root.name,
        suite_paths=[root / "suite.json"],
        adapter=adapter,
        options=options,
    )
    all_pairs = report(root, shared)
    atomic_json(root / "offline-native-report.json", native.model_dump(mode="json"))
    if not (root / "delta-image.json").exists():
        raise AssertionError("Native pipeline did not produce a delta")
    selected = json.loads((root / "selected-state.json").read_text())
    final_rows = [r for r in all_pairs["rows"] if r["task_id"] == "q5"]
    assert {r["arm"] for r in final_rows} == {"A", "B", "C"}
    if args.failure_injection:
        assert selected["value"]["kernel_id"] == initial.content_id
        assert json.loads((root / "fallback-selection.json").read_text())["accepted"] is False
        assert {r["arm"]: r["status"] for r in final_rows} == {
            "A": "failed",
            "B": "failed" if args.fail_all_holdout else "completed",
            "C": "failed" if args.fail_all_holdout else "completed",
        }
        assert any(r["task_id"] == "q2" and r["status"] == "completed" for r in all_pairs["rows"])
        assert next(r for r in final_rows if r["arm"] == "A")["native"]["content_score"] is None
    else:
        assert selected["value"]["kernel_id"] != initial.content_id
        assert all(r["native"]["success"] for r in final_rows)
        assert {r["native"]["turns"] for r in final_rows if r["arm"] in {"A", "B"}} == {2}
        assert {r["native"]["turns"] for r in final_rows if r["arm"] == "C"} == {1}
    assert all(r["trajectory_judge"] == "not_run" for r in final_rows)
    counts = json.loads((root / "shared/worker-calls.json").read_text())
    assert max(counts.values()) <= 8
    assert not (root / "shared/pause.json").exists()
    assert client.http_attempts() == 0
    atomic_json(
        root / "acceptance.json",
        {
            "native_pipeline": True,
            "real_docker": True,
            "actual_model_http": 0,
            "recorded_calls": client.calls,
            "accepted_delta": selected["value"]["kernel_id"],
            "quality_claim": False,
            "q6": "sealed_not_opened",
        },
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(root),
                "recorded_calls": client.calls,
                "actual_model_http": 0,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--failure-injection", action="store_true")
    parser.add_argument("--fail-all-holdout", action="store_true")
    parser.add_argument("--trace-env")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
