"""Slim recorded pilot of local native execution; never contacts infrastructure."""

import argparse
import asyncio
import builtins
import json
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from datasets.lift.local import LocalRunner, configure_native
from datasets.lift.tests.offline_native import RecordedClient


@contextmanager
def infrastructure_guard():
    """Fail every service connection, trace emission, child tool or dotenv open."""
    real_open = builtins.open
    real_path_open = Path.open
    real_popen = subprocess.Popen

    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file).name in {".env", "model.env"}:
            raise AssertionError("Secret dotenv discovery forbidden")
        return real_open(file, *args, **kwargs)

    def guarded_path_open(path, *args, **kwargs):
        if path.name in {".env", "model.env"}:
            raise AssertionError("Secret dotenv discovery forbidden")
        return real_path_open(path, *args, **kwargs)

    def guarded_popen(cmd, *args, **kwargs):
        if not isinstance(cmd, (list, tuple)) or cmd[0] != "git":
            raise AssertionError("Only pinned-source git inspection may spawn a process")
        return real_popen(cmd, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("builtins.open", guarded_open))
        stack.enter_context(patch.object(Path, "open", guarded_path_open))
        stack.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("No service calls"))
        )
        stack.enter_context(
            patch("socket.socket.connect_ex", side_effect=AssertionError("No service calls"))
        )
        stack.enter_context(patch("subprocess.Popen", guarded_popen))
        stack.enter_context(
            patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No child tools"))
        )
        stack.enter_context(
            patch("datasets.lift.tracing.emit", side_effect=AssertionError("No trace emission"))
        )
        stack.enter_context(
            patch(
                "datasets.lift.tracing.emit_model_role",
                side_effect=AssertionError("No trace emission"),
            )
        )
        yield


async def pilot(
    upstream,
    data,
    output,
    *,
    failures=False,
    all_failed=False,
    worker_limit=8,
    max_tokens=4096,
):
    from darwinagent.config import Config
    from darwinagent.runtime.artifacts import atomic_json
    from datasets.lift.data import LiftDataset
    from datasets.lift.kernel import initial_kernel
    from datasets.lift.transport import SharedTransport

    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    initial = initial_kernel(root / "K0")
    cfg = Config(
        model_strong="offline-recorded",
        model_middle="offline-recorded",
        model_fast="offline-recorded",
        work_dir=root / "transport",
        max_retries=1,
        max_http_requests=100,
    )
    cfg.role_tiers.update(worker="strong", judge="strong")
    client = RecordedClient(cfg, initial)
    client.failure_injection, client.fail_all_holdout = failures, all_failed
    original_chat = client.chat
    generation_requests = []
    proposal_queries = 0

    async def generation_guard(**kwargs):
        nonlocal proposal_queries
        assert kwargs.get("max_tokens") == max_tokens
        generation_requests.append({"role": kwargs["role"], "max_tokens": kwargs["max_tokens"]})
        if kwargs["role"] in {"worker", "judge"}:
            users = [json.loads(m["content"]) for m in kwargs["messages"] if m["role"] == "user"]
            assert sum("view" in u for u in users) == 1
            assert sum("task" in u for u in users) == 1
            assert "task" not in users[-1]["view"]
        if kwargs["role"] == "proposal" and proposal_queries == 0:
            proposal_queries += 1
            client.calls += 1
            payload = json.loads(kwargs["messages"][1]["content"])
            scope = payload["training"]["entries"][0]["facts"]["wiki_scope"]
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "action": "query_wiki",
                        "query": {
                            "question": "Inspect the public training original",
                            "scope": scope,
                            "view": "raw",
                            "cursor": None,
                            "max_chars": 8000,
                        },
                    }
                ),
                usage={"prompt_tokens": 20, "completion_tokens": 10},
                model="offline-recorded",
                elapsed_s=0.0,
            )
        return await original_chat(**kwargs)

    client.chat = generation_guard
    budget = root.parent / (root.name + "-budget") / "shared"
    if worker_limit == 16:
        from datasets.lift.tests.fixtures import worker16_fixture

        shared = worker16_fixture(client, budget)
    else:
        shared = SharedTransport(client, budget)
    runner = LocalRunner(
        dataset=LiftDataset(data),
        shared=shared,
        root=root,
        initial=initial,
        run_id="recorded-" + root.name,
        max_tokens=max_tokens,
    )
    result = await runner.run()
    rows = [r for r in result["rows"] if r["task_id"] == "q5"]
    assert {r["arm"] for r in rows} == {"A", "B", "C"}
    selected = result["selected"]
    if failures:
        assert selected["fallback"]
        assert {r["arm"]: r["status"] for r in rows} == {
            "A": "failed",
            "B": "failed" if all_failed else "completed",
            "C": "failed" if all_failed else "completed",
        }
        assert next(r for r in rows if r["arm"] == "A")["native"]["content_score"] is None
    else:
        assert not selected["fallback"], selected
        assert all(r["native"]["success"] for r in rows)
        assert next(r for r in rows if r["arm"] == "C")["native"]["turns"] == 1
        assert next(r for r in rows if r["arm"] == "C")["worker_macro_invocations"] == 1
        assert result["native_tool_count_semantics"] == "worker_primitive_leaf_calls"
    assert not result[
        "acceptance_complete"
    ]  # Synthetic mechanism evidence is never real acceptance.
    assert client.http_attempts() == 0
    counts = json.loads((shared.root / "worker-calls.json").read_text())
    assert max(counts.values()) <= worker_limit
    assert not (shared.root / "pause.json").exists()
    assert len({r["native"]["workspace_dir"] for r in rows}) == 3
    calls = client.calls
    # Exclude reports/lease bookkeeping: task/model/tool receipts and artifacts must
    # replay byte-for-byte and without writes when execution is already settled.
    protected = [root / n for n in ("sessions", "task-receipts", "workspaces", "loads")]
    before = {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for base in protected
        for p in base.rglob("*")
        if p.is_file()
    }
    await runner.run()
    assert client.calls == calls
    assert before == {
        str(p): (p.read_bytes(), p.stat().st_mtime_ns)
        for base in protected
        for p in base.rglob("*")
        if p.is_file()
    }
    assert any(r["task_id"] == "q2" and r["status"] == "completed" for r in result["rows"])
    c_loads = list((root / "loads").glob("C-*/load-receipt.json"))
    assert len(c_loads) == 1
    assert json.loads(c_loads[0].read_text())["kernel_id"] == selected["kernel_id"]
    atomic_json(
        root / "offline-local-acceptance.json",
        {
            "status": "PASS",
            "actual_model_http": 0,
            "recorded_calls": calls,
            "infrastructure_calls": 0,
            "native_protocol": True,
            "quality_claim": False,
            "acceptance_complete": False,
            "selected_kernel": selected["kernel_id"],
            "fallback": selected["fallback"],
            "resume_new_calls": 0,
            "resume_receipt_artifact_writes": 0,
            "source_sha256": result["source_sha256"],
            "worker_session_cap": worker_limit,
            "worker_turn_model_cap": 8,
            "generation_max_tokens": max_tokens,
            "role_generation_limits": {r["role"]: r["max_tokens"] for r in generation_requests},
            "http_cap": shared.cfg.max_http_requests,
            "proposal_call_limit": runner.identity()["proposal_call_limit"],
            "react_view_layout": runner.identity()["react_view_layout"],
            "proposal_original_queries": proposal_queries,
        },
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--failures", action="store_true")
    parser.add_argument("--all-failed", action="store_true")
    parser.add_argument("--worker-limit", type=int, choices=(8, 16), default=8)
    parser.add_argument("--max-tokens", type=int, choices=(4096, 16384), default=4096)
    args = parser.parse_args()
    with infrastructure_guard():
        configure_native(args.upstream)
        result = asyncio.run(
            pilot(
                args.upstream,
                args.data,
                args.output,
                failures=args.failures,
                all_failed=args.all_failed,
                worker_limit=args.worker_limit,
                max_tokens=args.max_tokens,
            )
        )
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": args.output,
                "recorded_calls": result["campaign_request_records"],
                "model_http": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
