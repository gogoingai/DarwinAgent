"""Pinned native LIFT lifecycle with Darwin's reviewed campaign and proposal machinery."""

import json
import os
import time
from pathlib import Path

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.exec import docker_exec_async
from src.lift.adapters.container.session import ContainerSession
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.task_exec import execute_task
from src.lift.eval.worker_judger import WorkerJudgerPair

from darwinagent.agent import AgentKernelBundle, EpisodeResult, ExecutionBackend
from darwinagent.config import RunConfig
from darwinagent.experiments import AgentExperimentSpec, CampaignController, EpisodeEvaluation
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import snapshot_files

from .agents import DarwinChatAgent, NativeReAct, tools_for, worker_profile
from .data import UPSTREAM_REVISION
from .state import install, load, snapshot
from .tools import DockerTools


class NativeEpisodeBackend(ExecutionBackend):
    def __init__(self, owner, workspace):
        self.owner, self.workspace = owner, Path(workspace).resolve()
        super().__init__(
            NativeReAct(),
            DockerTools("not-started", self.workspace).specs(),
            worker_profile(),
            owner.shared.scoped("campaign-worker", "worker"),
            owner.root / "campaign-receipts",
        )

    def for_episode(self, episode_root, task):
        self.owner.dataset.stage(task, episode_root)
        return type(self)(self.owner, episode_root)

    async def run(self, task, kernel=None, *, run_id):
        return await self.owner.execute_episode(task, kernel, self.workspace, run_id)


class NativeEvaluator:
    def __init__(self, owner):
        self.owner = owner

    def declaration(self):
        return {
            "kind": "native-lift-content",
            "revision": UPSTREAM_REVISION,
            "references": digest(
                {k: v.expected_result.model_dump() for k, v in self.owner.dataset.native.items()}
            ),
            "model": self.owner.shared.identity(),
            "criterion_scale": [0, 1],
            "max_turns": self.owner._options.max_conversation_turns,
        }

    async def evaluate(self, task, episode):
        row = json.loads(
            (
                self.owner.root / "evaluations" / (episode.usage["native_evaluation"] + ".json")
            ).read_text()
        )
        return EpisodeEvaluation(
            float(row["native"]["success"]),
            row["native"]["content_score"],
            healthy=episode.status == "completed",
            public_feedback=tuple(row["public_feedback"]) if task.id == "q1" else (),
            correction_turns=max(0, row["native"]["turns"] - 1),
        )


class DarwinLiftAdapter(ContainerAgentRuntimeAdapter):
    evolve_paths = ("/opt/darwin-state",)
    _EVOLVE_HOOK_ATTEMPTS = 1

    def __init__(self, options, *, dataset, shared, root, initial, trace=True):
        if os.environ.get("DOCKER_CONTEXT") != "colima-darwin-lift":
            raise ValueError("Use explicit DOCKER_CONTEXT=colima-darwin-lift")
        if (
            options.repeat != 1
            or options.max_parallel_suites != 1
            or options.max_concurrent_tasks != 1
            or options.holdout_container_policy.value != "serial_multi"
            or options.max_conversation_turns != 3
            or options.warmup_container_policy.value != "serial_single"
            or options.holdout_phase_policy.value != "serial"
        ):
            raise ValueError("Pilot requires 3 total native rounds and serial policies")
        self.dataset, self.shared, self.root = dataset, shared, Path(root).resolve()
        self.initial, self.trace, self.pairs = initial, trace, {}
        self.shared.trace = trace
        super().__init__(options)
        self.root.mkdir(parents=True, exist_ok=True)
        import subprocess

        self.base_image_id = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self._docker_image], text=True
        ).strip()
        self.source = snapshot_files(
            [Path(__file__).parent, Path(__file__).parents[2] / "src/darwinagent"]
        )
        identity = self.identity()
        path = self.root / "integration-identity.json"
        if path.exists() and json.loads(path.read_text()) != identity:
            raise ValueError("Source/configuration changed; fresh A/B/C run identities required")
        atomic_json(path, identity)

    def identity(self):
        return {
            "runtime": "darwinagent",
            "upstream": UPSTREAM_REVISION,
            "dataset": self.dataset.identity(),
            "initial": self.initial.content_id,
            "source": self.source,
            "options": self._options.model_dump(mode="json"),
            "shared_transport": self.shared.identity(),
            "image": self._docker_image,
            "base_image_id": self.base_image_id,
            "trace": self.trace,
            "trajectory_judge": "not_run",
            "worker_session_cap": 8,
        }

    async def create_suite_run_resources(self, ctx):
        self.shared.run_id = ctx.run_id
        return await super().create_suite_run_resources(ctx)

    @classmethod
    def resolve_docker_image(cls, *, override=None):
        return override or "darwinagent-lift-tools:task3"

    async def start_container(
        self,
        *,
        instance_id,
        image,
        ctx,
        workspace_dir,
        seed_workspace,
        task,
        load_state=None,
        viz_role=None,
    ):
        self.shared.check()
        self.dataset.identity()
        import subprocess

        from darwinagent.runtime.identity import assert_files

        assert_files(self.source)
        current_image = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self._docker_image], text=True
        ).strip()
        if current_image != self.base_image_id:
            raise ValueError("Tool image changed; use a fresh comparison identity")
        judge = viz_role == "judge"
        workspace = Path(workspace_dir).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if task is not None:
            self.dataset.stage(self.dataset.task(task.name), workspace)
        session = await ContainerSession.start(
            instance_id=instance_id,
            container_name_prefix="darwin-lift",
            image=self._docker_image if judge else image,
            entrypoint_cmd=["python", "-c", "import time; time.sleep(86400)"],
            port_mappings=[],
            env_vars={},
            volume_binds=[(str(workspace), "/workspace/task", "ro" if judge else "rw")],
            extra_docker_args=[
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ],
            metadata={"workspace": str(workspace), "role": viz_role},
            viz_role=viz_role,
            viz_suite_name=ctx.suite_name,
            viz_repeat_index=ctx.repeat_index,
        )
        from .data import sha

        installed_toolbox = await docker_exec_async(
            session.container_name,
            [
                "python",
                "-c",
                "import hashlib; print(hashlib.sha256(open('/opt/darwin-tools/toolbox.py','rb').read()).hexdigest())",
            ],
        )
        if installed_toolbox.strip() != sha(Path(__file__).with_name("toolbox.py")):
            await session.cleanup()
            raise ValueError(
                "Tool image differs from frozen toolbox source; rebuild before running"
            )
        # Baseline state is deterministic; evolved state must already be in its image.
        if not judge and load_state != HoldoutLoadState.EVOLVED:
            await install(session, snapshot(self.initial))
        inspection = await docker_exec_async(
            session.container_name,
            [
                "python",
                "-c",
                'import json,os; print(json.dumps({"environment_keys":sorted(os.environ),"workspace_files":sorted(os.listdir("/workspace/task"))}))',
            ],
        )
        atomic_json(
            self.root / "containers" / (session.container_name + ".json"),
            {
                "container": session.container_name,
                "image": session.image,
                "mounts": [
                    {
                        "source": str(workspace),
                        "target": "/workspace/task",
                        "mode": "ro" if judge else "rw",
                    }
                ],
                "credential_env": False,
                "inspection": json.loads(inspection),
            },
        )
        return session

    def worker_judger_factory(self, env, ctx, *, run_phase, workspace_dir):
        async def factory(task):
            self.shared.check()
            public = self.dataset.task(task.name)
            phase = run_phase.workspace_segment
            key = digest(
                {
                    "run": ctx.run_id,
                    "workspace": str(Path(workspace_dir).resolve()),
                    "task": task.name,
                    "phase": phase,
                }
            )[:24]
            expected = env.handle.metadata.get("kernel_override") or self.initial.content_id
            if run_phase.is_evolve_turn:
                expected = json.loads((self.root / "selected-state.json").read_text())["value"][
                    "kernel_id"
                ]
            kernel, envelope = await load(env.handle, self.root / "loads" / key, expected)
            # Arm A is explicitly selected only for the additional isolated native execution.
            if env.handle.metadata.get("arm") == "A":
                kernel = None
            if env.handle.metadata.get("kernel_override"):
                kernel, envelope = await load(
                    env.handle, self.root / "loads" / (key + "-candidate")
                )
            worker_sid, judge_sid = "worker-" + key, "judge-" + key
            worker = DarwinChatAgent(
                public,
                tools_for(env.handle, workspace_dir),
                self.shared,
                self.root / "sessions" / worker_sid,
                kernel,
                role="worker",
                run_id=ctx.run_id,
                session=worker_sid,
                trace=self.trace,
            )
            judge = DarwinChatAgent(
                public,
                tools_for(env.judge_handle, workspace_dir, judge=True),
                self.shared,
                self.root / "sessions" / judge_sid,
                None,
                role="judge",
                run_id=ctx.run_id,
                session=judge_sid,
                trace=self.trace,
            )
            pair = WorkerJudgerPair(worker, judge, worker_sid, judge_sid)
            self.pairs[worker_sid] = pair
            return pair

        return factory

    async def run_before_load(self, task, resources, ctx):
        if task.name != "q5" or not (self.root / "selected-state.json").exists():
            raise ValueError("Only frozen q5 development comparison is authorized")
        arm_path = self.root / "arm-A" / task.name
        receipt = arm_path / "episode.json"
        if not receipt.exists():
            result = await self.execute_episode(
                self.dataset.task(task.name),
                None,
                arm_path / "workspace",
                ctx.run_id + "-A",
                arm="A",
            )
            atomic_json(receipt, result.to_dict())
        return await super().run_before_load(task, resources, ctx)

    async def count_tool_calls(self, env, task, result, ctx):
        count = self.pairs[result.work_session_id].work_agent.count_tools()
        self.record_native(
            result, task.name, "C" if env.handle.image != self._docker_image else "B"
        )
        return count

    def record_native(self, result, task_id, arm, *, evaluation_key=None, elapsed=None):
        pair = self.pairs[result.work_session_id]
        episodes = pair.work_agent.episodes()
        usage = {
            "model_calls": sum(e["usage"]["model_calls"] for e in episodes),
            "leaf_calls": sum(e["usage"]["leaf_calls"] for e in episodes),
            "unknown_calls": 0,
            "pending_calls": 0,
        }
        events = tuple(e for r in episodes for e in r["events"])
        tokens_known = all(
            "prompt_tokens" in e.get("usage", {}) and "completion_tokens" in e.get("usage", {})
            for e in events
            if e["kind"] == "model"
        )
        row = {
            "task_id": task_id,
            "arm": arm,
            "status": "completed",
            "native": result.model_dump(),
            "worker_usage": usage,
            "worker_tokens": sum(
                sum(e["usage"][k] for k in ("prompt_tokens", "completion_tokens"))
                for e in events
                if e["kind"] == "model"
            )
            if tokens_known
            else None,
            "worker_time_s": sum(
                t["elapsed_s"]
                for t in json.loads((pair.work_agent.root / "chat.json").read_text())["turns"]
            ),
            "task_time_s": elapsed,
            "native_tool_calls": pair.work_agent.count_tools(),
            "flattened_leaf_calls": usage["leaf_calls"],
            "trajectory_judge": "not_run",
            "public_feedback": [
                m["content"]
                for m in json.loads((pair.work_agent.root / "chat.json").read_text())["history"][2:]
                if m["role"] == "user"
            ],
        }
        atomic_json(self.root / "rows" / (result.work_session_id + ".json"), row)
        if evaluation_key:
            atomic_json(self.root / "evaluations" / (evaluation_key + ".json"), row)
            usage["native_evaluation"] = evaluation_key
        return EpisodeResult(
            "completed",
            episodes[-1]["final_response"],
            tuple(episodes[-1]["artifacts"]),
            events,
            usage,
        )

    async def execute_episode(self, task, kernel, workspace, run_id, arm="campaign"):
        self.shared.check()
        self.dataset.stage(task, workspace)
        ctx = SuiteRunContext(
            run_id=run_id,
            repeat_index=0,
            suite_index=0,
            suite_path=self.root / "suite.json",
            category_name="sales_ops_development",
            suite_name="campaign",
        )
        env = await self.start_holdout_environment(
            ctx,
            None,
            self.dataset.native[task.id],
            workspace,
            image=self._docker_image,
            seed_workspace=True,
            load_state=HoldoutLoadState.BASELINE,
        )
        started = time.monotonic()
        try:
            if kernel is not None:
                await install(env.handle, snapshot(kernel))
                env.handle.metadata["kernel_override"] = kernel.content_id
            if arm == "A":
                env.handle.metadata["arm"] = "A"
            result = await execute_task(
                task=self.dataset.native[task.id],
                run_id=run_id,
                workspace_dir=Path(workspace),
                factory=self.worker_judger_factory(
                    env,
                    ctx,
                    run_phase=SuiteRunPhase.holdout(HoldoutLoadState.BASELINE),
                    workspace_dir=workspace,
                ),
                run_phase=SuiteRunPhase.holdout(HoldoutLoadState.BASELINE),
                max_conversation_turns=3,
            )
            return self.record_native(
                result, task.id, arm, evaluation_key=run_id, elapsed=time.monotonic() - started
            )
        finally:
            await env.disposable.cleanup()

    async def evolve_after_task(self, env, task, result, ctx):
        row = self.record_native(result, task.name, "warmup")
        value = {
            "input": self.dataset.task(task.name).to_dict(),
            "episode": row.to_dict(),
            "source": digest(self.source),
            "initial": self.initial.content_id,
        }
        path = self.root / "hooks" / ("task-" + task.name + ".json")
        if path.exists() and json.loads(path.read_text()) != value:
            raise ValueError("Repeated warmup hook input changed")
        if not path.exists():
            atomic_json(path, value)

    def evaluator_factory(self, client, path):
        return NativeEvaluator(self)

    def client_factory(self, stage):
        return self.shared.scoped("evolution-" + digest(str(stage))[:16], "proposal_wiki")

    async def evolve_after_warmup(self, env, ctx):
        self.shared.check()
        root = self.root / "campaign"
        controller = CampaignController(
            self.dataset,
            self.evaluator_factory,
            self.shared.cfg,
            RunConfig(temperature=0, concurrency=1),
            AgentExperimentSpec(
                train=("q1",),
                validation=("q2",),
                admission=("q1",),
                rounds=1,
                max_episode_runs=6,
                proposal_call_limit=4,
            ),
            root,
            frozen_files=[Path(__file__).parent],
            client_factory=self.client_factory,
            wiki_call_limit=4,
            backend=NativeEpisodeBackend(self, self.root / "campaign-template"),
        )
        summary = await controller.run(self.initial, resume=(root / "agent-campaign.json").exists())
        if summary.get("status") != "complete":
            atomic_json(self.root / "partial-campaign.json", summary)
            raise BudgetExceeded("Campaign incomplete; no delta may be promoted")
        selected = AgentKernelBundle(root / "selected")
        wiki_root = root / "optimization"
        wiki = {
            str(p.relative_to(wiki_root)): p.read_text() for p in sorted(wiki_root.rglob("*.json"))
        }
        envelope = snapshot(selected, wiki)
        path = self.root / "selected-state.json"
        if path.exists() and json.loads(path.read_text()) != envelope:
            raise ValueError("Repeated hook selection or Wiki changed")
        if not path.exists():
            atomic_json(path, envelope)
        materialization = await install(env.handle, envelope)
        loaded, loaded_envelope = await load(
            env.handle, self.root / "accepted-load", selected.content_id
        )
        if loaded_envelope != envelope:
            raise ValueError("Materialized state differs from selection")
        evidence = {
            "initial": self.initial.content_id,
            "selected": loaded.content_id,
            "state_sha256": envelope["sha256"],
            "source": digest(self.source),
            "image": env.handle.image,
            "selection": digest((root / "selection.json").read_text()),
        }
        hook_path = self.root / "hooks" / "warmup.json"
        if hook_path.exists() and json.loads(hook_path.read_text()) != evidence:
            raise ValueError("Repeated warmup hook identity changed")
        if not hook_path.exists():
            atomic_json(hook_path, evidence)
        atomic_json(
            self.root / "hooks" / ("materialization-" + materialization + ".json"), evidence
        )

    async def materialize_delta(self, env, ctx):
        self.shared.check()
        delta = await super().materialize_delta(env, ctx)
        # Export image identity and diff while native cleanup has not removed the image.
        import asyncio

        process = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", delta.image_tag, stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError("Cannot preserve native delta image identity")
        atomic_json(self.root / "delta-image.json", json.loads(stdout))
        return delta
