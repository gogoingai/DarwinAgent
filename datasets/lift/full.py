"""Opt-in full local LIFT campaign; importing and CLI help perform no dispatch."""

import argparse
import asyncio
import json
from pathlib import Path

from darwinagent.agent import AgentKernelBundle, ExecutionBackend
from darwinagent.config import RunConfig
from darwinagent.experiments import AgentExperimentSpec, CampaignController, EpisodeEvaluation
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.diagnostics import exception_evidence
from darwinagent.runtime.identity import assert_files, snapshot_files
from darwinagent.runtime.leases import execution_lease

from .full_tools import FullLocalTools


async def execute_episode(*args, **kwargs):
    from .full_episode import execute_episode as native_execute

    return await native_execute(*args, **kwargs)


class SceneInputs:
    """Public-only projection: a campaign cannot request another scene or test task."""

    def __init__(self, dataset, scene):
        self.dataset, self.scene = dataset, scene
        self.allowed = dataset.task_ids(scene, "train")

    def task(self, task_id):
        if task_id not in self.allowed:
            raise PermissionError("Campaign inputs are restricted to scene training tasks")
        return self.dataset.task(task_id)

    def identity(self):
        return dict(dataset=self.dataset.identity(), scene=self.scene, allowed=list(self.allowed))


class FullCampaignBackend(ExecutionBackend):
    def __init__(self, owner, scene, workspace=None):
        from .full_agents import FullNativeReAct, full_worker_profile

        self.owner, self.scene = owner, scene
        super().__init__(
            FullNativeReAct(
                max_turns=None,
                max_tokens=16384,
                native_rounds=owner.correction_rounds,
                worker_session_cap=owner.task_policy.worker_request_limit
                if owner.task_policy
                else None,
            ),
            FullLocalTools(workspace or owner.root / "template").specs(),
            full_worker_profile(),
            owner.shared.scoped("campaign-worker", "worker"),
            owner.root / "campaign-receipts",
        )

    def for_episode(self, episode_root, task):
        return type(self)(self.owner, self.scene, episode_root)

    async def run(self, task, kernel=None, *, run_id):
        phase = (
            "validation"
            if task.id == self.owner.dataset.task_ids(self.scene, "train")[3]
            else "train"
        )
        episode, _ = await execute_episode(self.owner, task, kernel, "campaign", phase)
        return episode


class FullEvaluator:
    def __init__(self, owner, scene):
        self.owner, self.scene = owner, scene

    def declaration(self):
        result = dict(
            kind="full-native-content-and-trajectory-v1",
            scene=self.scene,
            dataset=self.owner.dataset.identity(),
            model=self.owner.shared.identity(),
            correction_rounds=self.owner.correction_rounds,
        )
        if self.owner.task_policy is not None:
            result["task_execution_policy"] = self.owner.task_policy.declaration()
        return result

    async def evaluate(self, task, episode):
        key = episode.usage["full_evidence"]
        row = json.loads((self.owner.root / "episodes" / key / "receipt.json").read_text())["row"]
        native = row["native"] or {}
        training = task.id in self.owner.dataset.task_ids(self.scene, "train")[:3]
        return EpisodeEvaluation(
            float(native.get("success", False)),
            native.get("content_score") or 0,
            healthy=row["healthy"],
            public_feedback=tuple(row["public_feedback"]) if training else (),
            correction_turns=max(0, native.get("turns", 1) - 1),
        )


class FullLocalRunner:
    def __init__(
        self, *, dataset, shared, root, initial, run_id, rounds=1, scenes=None, task_policy=None
    ):
        with shared.campaign_lease():
            self._initialize(
                dataset=dataset,
                shared=shared,
                root=root,
                initial=initial,
                run_id=run_id,
                rounds=rounds,
                scenes=scenes,
                task_policy=task_policy,
            )

    def _initialize(self, *, dataset, shared, root, initial, run_id, rounds, scenes, task_policy):
        from src.lift.eval import task_exec

        if type(rounds) is not int or rounds < 1:
            raise ValueError("Full campaign rounds must be positive")
        from .task_budget import TaskExecutionPolicy

        if task_policy is not None and not isinstance(task_policy, TaskExecutionPolicy):
            raise ValueError("Task policy must be an explicit validated declaration")
        self.task_policy = task_policy
        self.correction_rounds = task_policy.correction_rounds if task_policy else 30
        if (
            shared.trace
            or shared.worker_limit is not None
            or shared.cfg.max_http_requests is not None
        ):
            raise ValueError("Full protocol requires trace=False and explicit null request caps")
        shared.cfg.model_for("trajectory_judge")
        if shared.cfg.api_base_url and (
            shared.cfg.api_base_url != "https://api.minimax.cn/v1"
            or any(
                shared.cfg.model_for(role) != "MiniMax-M3.1-Flash-Preview"
                for role in ("worker", "judge", "trajectory_judge")
            )
            or shared.cfg.provider_request_policy != "minimax-adaptive-v1"
            or shared.cfg.thinking_type != "adaptive"
            or shared.cfg.reasoning_effort != "max"
        ):
            raise ValueError("Full runtime requires the fixed authorized MiniMax policy")
        self.dataset, self.shared, self.root = dataset, shared, Path(root).resolve()
        if self.root.is_relative_to(shared.root.resolve()) or shared.root.resolve().is_relative_to(
            self.root
        ):
            raise ValueError("Run root must be independent of preserved shared ledger")
        self.initial, self.run_id, self.rounds = initial, run_id, rounds
        self.scenes = tuple(dataset.scenes if scenes is None else scenes)
        if (
            not self.scenes
            or len(set(self.scenes)) != len(self.scenes)
            or any(s not in dataset.scenes for s in self.scenes)
        ):
            raise ValueError("Unknown or duplicate scene scope")
        from .full_agents import full_worker_profile

        if initial.profile_declaration != full_worker_profile().declaration():
            raise ValueError("Initial kernel must use the full seven-tool profile")
        shared.generation_max_tokens = 16384
        shared.run_id = run_id
        self.source = snapshot_files(
            [
                Path(__file__).parent,
                Path(__file__).parents[2] / "src/darwinagent",
                Path(task_exec.__file__).parents[2],
            ]
        )
        self.frozen_identity = dict(
            protocol="full-lift-local-v1",
            run_id=run_id,
            rounds=rounds,
            scenes=list(self.scenes),
            dataset=dataset.identity(),
            source=self.source,
            initial=initial.content_id,
            transport=shared.identity(),
            correction_rounds=self.correction_rounds,
            generation_max_tokens=16384,
            tools=FullLocalTools(self.root / "template").identity(),
            confirmation_policy="q5-health-before-q6-v1",
        )
        if task_policy is not None:
            self.frozen_identity["protocol"] = "full-lift-local-bounded-v1"
            self.frozen_identity["task_execution_policy"] = task_policy.declaration()
        guard = getattr(shared.client, "resource_guard", None)
        if guard is not None:
            self.frozen_identity["resource_policy"] = guard.declaration()
        self.frozen_identity = json.loads(json.dumps(self.frozen_identity))
        identity = self.root / "integration-identity.json"
        if identity.exists():
            if json.loads(identity.read_text()) != self.frozen_identity:
                raise ValueError("Frozen source/data/config/tools changed; use fresh all-A/B/C run")
        else:
            atomic_json(identity, self.frozen_identity)

    def identity(self):
        return self.frozen_identity

    def verify(self):
        assert_files(self.source)
        guard = getattr(self.shared.client, "resource_guard", None)
        if (guard.declaration() if guard is not None else None) != self.frozen_identity.get(
            "resource_policy"
        ):
            raise ValueError("Frozen resource policy changed")
        if (
            snapshot_files(
                [
                    Path(__file__).parent,
                    Path(__file__).parents[2] / "src/darwinagent",
                    Path(
                        next(p for p in self.source if p.endswith("/lift/eval/task_exec.py"))
                    ).parents[2],
                ]
            )
            != self.source
        ):
            raise ValueError("Frozen source inventory changed")
        if (
            list(self.scenes) != self.frozen_identity["scenes"]
            or self.rounds != self.frozen_identity["rounds"]
            or self.correction_rounds != self.frozen_identity["correction_rounds"]
            or (self.task_policy.declaration() if self.task_policy else None)
            != self.frozen_identity.get("task_execution_policy")
        ):
            raise ValueError("Frozen scene scope or rounds changed")
        if self.initial.content_id != self.frozen_identity["initial"]:
            raise ValueError("Frozen initial kernel changed")
        self.initial.verify()
        if (
            self.dataset.identity() != self.frozen_identity["dataset"]
            or self.shared.identity() != self.frozen_identity["transport"]
        ):
            raise ValueError("Frozen data or model identity changed")
        if self.shared.generation_max_tokens != 16384 or self.shared.trace:
            raise ValueError("Frozen generation policy changed")
        if FullLocalTools(self.root / "template").identity() != self.frozen_identity["tools"]:
            raise ValueError("Frozen tool policy changed")

    def cell_path(self, task_id, arm, phase):
        return (
            self.root / "cells" / (digest(dict(task=task_id, arm=arm, phase=phase))[:24] + ".json")
        )

    def plan(self):
        for scene in self.scenes:
            for task_id in self.dataset.task_ids(scene, "train"):
                phase = (
                    "validation" if task_id == self.dataset.task_ids(scene, "train")[3] else "train"
                )
                self.plan_cell(task_id, "campaign", phase)
            for task_id in self.dataset.task_ids(scene, "test"):
                for arm in ("A", "B", "C"):
                    self.plan_cell(task_id, arm, "holdout")

    def plan_cell(self, task_id, arm, phase):
        path = self.cell_path(task_id, arm, phase)
        if not path.exists():
            atomic_json(
                path,
                dict(
                    task_id=task_id,
                    arm=arm,
                    phase=phase,
                    status="not_started",
                    metadata=self.dataset.task_metadata(task_id),
                ),
            )

    def load_kernel(self, kernel, key):
        kernel.verify()
        root = self.root / "loads" / key
        for source in kernel.root.rglob("*"):
            if source.is_file():
                target = root / source.relative_to(kernel.root)
                if target.exists() and target.read_bytes() != source.read_bytes():
                    raise ValueError("Loaded kernel changed")
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
        loaded = AgentKernelBundle(root)
        if loaded.content_id != kernel.content_id:
            raise ValueError("Loaded kernel identity differs")
        return loaded

    async def evolve(self, scene):
        state_path = self.root / "selections" / (digest(scene)[:24] + ".json")
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["sha256"] != digest({k: v for k, v in state.items() if k != "sha256"}):
                raise ValueError("Frozen selected state changed")
            selected = AgentKernelBundle(state["bundle_root"])
            if selected.content_id != state["kernel_id"]:
                raise ValueError("Frozen selected kernel changed")
            return selected
        ids = self.dataset.task_ids(scene, "train")
        healthy = True
        for task_id in ids:
            phase = "validation" if task_id == ids[3] else "train"
            _, row = await execute_episode(
                self, self.dataset.task(task_id), self.initial, "campaign", phase
            )
            healthy = healthy and row["healthy"]
        selected = self.initial
        summary = dict(status="failed_training", healthy=False)
        if healthy:
            campaign_root = self.root / "campaigns" / digest(scene)[:24]
            controller = CampaignController(
                SceneInputs(self.dataset, scene),
                lambda client, path: FullEvaluator(self, scene),
                self.shared.cfg,
                RunConfig(temperature=0, concurrency=1, max_tokens=16384, wiki_max_tokens=16384),
                AgentExperimentSpec(
                    train=ids[:3],
                    validation=ids[3:],
                    admission=ids[:3],
                    rounds=self.rounds,
                    max_episode_runs=8 + 10 * self.rounds,
                    proposal_call_limit=None,
                ),
                campaign_root,
                frozen_files=[Path(__file__).parent],
                client_factory=lambda stage: self.campaign_client(scene, stage),
                wiki_call_limit=None,
                backend=FullCampaignBackend(self, scene),
            )
            try:
                summary = await controller.run(
                    self.initial, resume=(campaign_root / "agent-campaign.json").exists()
                )
                if summary.get("status") == "complete":
                    selected = AgentKernelBundle(campaign_root / "selected")
            except Exception as exc:
                self.shared.check()
                summary = dict(
                    status="failed_campaign",
                    healthy=False,
                    error=type(exc).__name__,
                    diagnostics=exception_evidence(exc, stage="full_campaign"),
                )
        self.shared.check()
        state = dict(
            kernel_id=selected.content_id,
            bundle_root=str(selected.root),
            fallback=selected.content_id == self.initial.content_id,
            campaign=summary,
        )
        atomic_json(state_path, dict(state, sha256=digest(state)))
        return selected

    def campaign_client(self, scene, stage):
        self.verify()
        self.shared.check()
        return self.shared.scoped("evolution-" + digest([scene, str(stage)])[:24], "proposal_wiki")

    async def run(self):
        with self.shared.campaign_lease():
            return await self._run_owned()

    async def _run_owned(self):
        self.verify()
        with execution_lease(self.root / "full-run.lock"):
            self.plan()  # All 84 holdout cells are durable before any dispatch.
            try:
                selected_by_scene = {scene: await self.evolve(scene) for scene in self.scenes}
                selection = {
                    scene: bundle.content_id for scene, bundle in selected_by_scene.items()
                }
                path = self.root / "frozen-selections.json"
                if path.exists():
                    if json.loads(path.read_text()) != selection:
                        raise ValueError("Frozen scene selection manifest changed")
                else:
                    atomic_json(path, selection)
                for test_index in (0, 1):
                    if test_index == 1 and not self.development_ready():
                        return self.write_report()
                    for scene in self.scenes:
                        selected = selected_by_scene[scene]
                        task_id = self.dataset.task_ids(scene, "test")[test_index]
                        for arm, kernel in (("A", None), ("B", self.initial), ("C", selected)):
                            await execute_episode(
                                self, self.dataset.task(task_id), kernel, arm, "holdout"
                            )
            finally:
                self.write_report()
        return self.write_report()

    def development_ready(self):
        """Keep every q6 untouched until all q5 executions/judgments are healthy."""
        for scene in self.scenes:
            task_id = self.dataset.task_ids(scene, "test")[0]
            for arm in ("A", "B", "C"):
                path = self.cell_path(task_id, arm, "holdout")
                if not path.exists():
                    return False
                row = json.loads(path.read_text())
                if row.get("status") != "completed" or not row.get("healthy"):
                    return False
        return True

    def write_report(self):
        from .full_report import write_report

        return write_report(self)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    from .local import configure_native, run_and_close

    configure_native(args.upstream)
    from .full_config import build_runner

    runner = build_runner(json.loads(Path(args.config).read_text()), args.run_id)
    asyncio.run(run_and_close(runner))


if __name__ == "__main__":
    main()
