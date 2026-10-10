"""Small local LIFT coordinator. Import and --help never dispatch models or services."""

import argparse
import asyncio
import csv
import json
import os
import time
from pathlib import Path

from darwinagent.agent import AgentKernelBundle, EpisodeResult, ExecutionBackend
from darwinagent.config import RunConfig
from darwinagent.experiments import AgentExperimentSpec, CampaignController, EpisodeEvaluation
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import assert_files, snapshot_files
from darwinagent.runtime.leases import execution_lease

from .data import UPSTREAM_REVISION, LiftConfirmationDataset, LiftDataset, sha
from .local_tools import LocalTools
from .report import available_sum, cell, report, token_count, update_cell


def configure_native(path):
    """Disable dotenv discovery and native reporting before importing upstream."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["EVAL_LANGFUSE_PRE_CHAT"] = "0"
    os.environ["DO_TRAJECTORY_JUDGE"] = "false"
    from .register import upstream

    upstream(path)
    from src.config import CONFIG

    if CONFIG.langfuse_pre_chat:
        raise ValueError("Native reporting already imported with tracing enabled")


class LocalEpisodeBackend(ExecutionBackend):
    def __init__(self, owner, workspace):
        from .agents import NativeReAct, worker_profile

        self.owner, self.workspace = owner, Path(workspace).resolve()
        super().__init__(
            NativeReAct(worker_session_cap=owner.shared.worker_limit, max_tokens=owner.max_tokens),
            LocalTools(self.workspace).specs(),
            worker_profile(),
            owner.shared.scoped("campaign-worker", "worker"),
            owner.root / "campaign-receipts",
        )

    def for_episode(self, episode_root, task):
        return type(self)(self.owner, episode_root)

    async def run(self, task, kernel=None, *, run_id):
        # Admission and training share public execution evidence for the exact
        # same task/K0 identity. Candidate q1 may similarly reuse admission.
        # Private q2 judgments never become training evidence.
        return await self.owner.execute(task, kernel, "campaign", "campaign", run_id)


class LocalEvaluator:
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
            "max_turns": 3,
        }

    async def evaluate(self, task, episode):
        row = json.loads(
            (
                self.owner.root / "evaluations" / (episode.usage["native_evaluation"] + ".json")
            ).read_text()
        )
        native = row["native"]
        # EpisodeEvaluation requires numbers; failed missing scores remain null in
        # all-pair evidence, while the internal unhealthy sentinel cannot be adopted.
        return EpisodeEvaluation(
            float(native["success"]),
            native["content_score"] if native["content_score"] is not None else 0,
            healthy=row["healthy"] and episode.status == "completed",
            public_feedback=tuple(row["public_feedback"]) if task.id == "q1" else (),
            correction_turns=max(0, native["turns"] - 1),
        )


class LocalRunner:
    def __init__(
        self,
        *,
        dataset,
        shared,
        root,
        initial,
        run_id,
        mode="development",
        selection_run_root=None,
        max_tokens=4096,
    ):
        from src.lift.eval import task_exec

        self.dataset, self.shared, self.root = dataset, shared, Path(root).resolve()
        if type(max_tokens) is not int or max_tokens not in (4096, 16384):
            raise ValueError("Local generation max_tokens must be explicit 4096 or 16384")
        if shared.worker_limit == 16 and max_tokens != 16384:
            raise ValueError("The cumulative16 diagnostic requires uniform generation16384")
        self.max_tokens = max_tokens
        shared.generation_max_tokens = max_tokens
        self.mode, self.selection_run_root = (
            mode,
            Path(selection_run_root).resolve() if selection_run_root else None,
        )
        if mode not in {"development", "confirmation"}:
            raise ValueError("Unknown local execution mode")
        if mode == "confirmation" and (
            not isinstance(dataset, LiftConfirmationDataset) or not self.selection_run_root
        ):
            raise ValueError(
                "Independent confirmation requires a separate q6 dataset and frozen selection run"
            )
        if mode == "development" and isinstance(dataset, LiftConfirmationDataset):
            raise ValueError("q6 cannot enter development training")
        if type(dataset) is LiftDataset and len(dataset.files) != 16:
            raise ValueError("Local pilot requires the existing exact 16-file manifest")
        if self.selection_run_root and (
            self.selection_run_root.is_relative_to(self.root)
            or self.root.is_relative_to(self.selection_run_root)
        ):
            raise ValueError("Confirmation and selection run roots must be independent")
        self.initial, self.run_id = initial, run_id
        if (
            shared.trace
            or shared.worker_limit not in (8, 16)
            or shared.cfg.max_http_requests not in (100, 500)
        ):
            raise ValueError("Local runner requires trace=False and authorized worker/HTTP limits")
        if shared.root.resolve().is_relative_to(self.root) or self.root.is_relative_to(
            shared.root.resolve()
        ):
            raise ValueError("Run and shared budget directories must be independent")
        self.shared.trace = False
        self.shared.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = snapshot_files(
            [
                Path(__file__).parent,
                Path(__file__).parents[2] / "src/darwinagent",
                Path(task_exec.__file__).parents[2],
            ]
        )
        identity = {
            "mode": "local-native-task-adaptation-v1"
            if mode == "development"
            else "local-independent-q6-confirmation-v1",
            "selection_run_root": str(self.selection_run_root) if self.selection_run_root else None,
            "run_id": run_id,
            "source": self.source,
            "upstream": UPSTREAM_REVISION,
            "dataset": self.dataset.identity(),
            "initial": self.initial.content_id,
            "transport": shared.identity(),
            "worker_limit": shared.worker_limit,
            "worker_turn_model_cap": 8,
            "generation_max_tokens": max_tokens,
            "correction_rounds": 3,
            "trace": False,
        }
        path = self.root / "integration-identity.json"
        if path.exists() and json.loads(path.read_text()) != identity:
            raise ValueError("Frozen source/data/config changed; use a fresh A/B/C directory")
        if not path.exists():
            atomic_json(path, identity)
        self.frozen_identity = identity

    def identity(self):
        return self.frozen_identity

    def verify(self):
        if (
            self.max_tokens != self.frozen_identity["generation_max_tokens"]
            or self.shared.generation_max_tokens != self.max_tokens
        ):
            raise ValueError("Frozen generation policy changed")
        assert_files(self.source)
        if self.dataset.identity() != self.frozen_identity["dataset"]:
            raise ValueError("Frozen dataset identity changed")
        self.initial.verify()
        if self.shared.identity() != self.frozen_identity["transport"] or self.shared.trace:
            raise ValueError("Frozen transport identity changed")

    def evaluator_factory(self, client, path):
        return LocalEvaluator(self)

    def client_factory(self, stage):
        self.verify()
        self.shared.check()
        if any(
            json.loads(p.read_text())["status"] == "failed"
            for p in (self.root / "cells").glob("*.json")
        ):
            raise RuntimeError("Unhealthy task forbids proposal/Wiki calls")
        return self.shared.scoped("evolution-" + digest(str(stage))[:16], "proposal_wiki")

    async def execute(self, task, kernel, arm, phase, evaluation_key):
        from src.lift.eval.run_task import _extract_judge_result
        from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
        from src.lift.eval.task_exec import execute_task
        from src.lift.eval.worker_judger import WorkerJudgerPair

        from .agents import DarwinChatAgent

        self.verify()
        key = digest({"task": task.id, "kernel": kernel.content_id if kernel else None})[:24]
        # Only training/admission evidence can be shared; independent q5 A/B/C
        # always get separate workspaces, histories and worker caps, including C=K0.
        evidence_key = key if arm == "campaign" else arm + "-" + key
        receipt = self.root / "task-receipts" / (evidence_key + ".json")
        evaluation = self.root / "evaluations" / (evaluation_key + ".json")
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if (
                digest(
                    {
                        "episode": saved["episode"],
                        "row": saved["row"],
                        "workspace": saved["workspace"],
                    }
                )
                != saved["sha256"]
            ):
                raise ValueError("Settled local task receipt changed")
            workspace = self.root / "workspaces" / evidence_key
            current = {
                str(p.relative_to(workspace)): sha(p) for p in workspace.rglob("*") if p.is_file()
            }
            if current != saved["workspace"] or any(p.is_symlink() for p in workspace.rglob("*")):
                raise ValueError("Settled local task artifacts changed")
            if not evaluation.exists():
                atomic_json(evaluation, saved["row"])
            return EpisodeResult(**saved["episode"])
        self.shared.check()
        if arm == "campaign":
            phase = "train" if task.id == "q1" else "validation"
            cell_arm = "campaign" if kernel.content_id == self.initial.content_id else "candidate"
        else:
            cell_arm = arm
        cell_key = cell(self.root, task.id, cell_arm, phase, self.run_id)
        update_cell(self.root, cell_key, status="running", started=time.time())
        workspace = self.root / "workspaces" / evidence_key
        self.dataset.stage(task, workspace)
        # Reload every selected asset into a separate host-only location; tools
        # cannot address loads, private criteria, transport or accepted state.
        if kernel is not None:
            kernel = self.load_kernel(kernel, evidence_key)
        sid = digest({"run_id": self.run_id, "run_root": str(self.root), "evidence": evidence_key})[
            :24
        ]
        worker_sid, judge_sid = "worker-" + sid, "judge-" + sid
        update_cell(self.root, cell_key, sessions=[worker_sid, judge_sid])
        worker = DarwinChatAgent(
            task,
            LocalTools(workspace).specs(),
            self.shared,
            self.root / "sessions" / worker_sid,
            kernel,
            role="worker",
            run_id=self.run_id,
            session=worker_sid,
            trace=False,
            cell_id=cell_key,
            arm=arm,
            phase=phase,
        )
        judge = DarwinChatAgent(
            task,
            LocalTools(workspace, readonly=True).specs(),
            self.shared,
            self.root / "sessions" / judge_sid,
            None,
            role="judge",
            run_id=self.run_id,
            session=judge_sid,
            trace=False,
            cell_id=cell_key,
            arm=arm,
            phase=phase,
        )
        pair = WorkerJudgerPair(worker, judge, worker_sid, judge_sid)

        async def factory(native):
            return pair

        scores = []

        def on_turn(index, prompt, result, scored):
            scores.append(scored.model_dump())
            atomic_json(
                self.root / "native-turns" / evidence_key / (str(index) + ".json"),
                scored.model_dump(),
            )

        failure = None
        try:
            native = (
                await execute_task(
                    task=self.dataset.native[task.id],
                    run_id=self.run_id,
                    workspace_dir=workspace,
                    factory=factory,
                    run_phase=SuiteRunPhase.holdout(HoldoutLoadState.BASELINE),
                    max_conversation_turns=3,
                    on_turn=on_turn,
                )
            ).model_dump()
        except Exception as exc:
            self.shared.check()  # Unknown/HTTP/global gate must escape, never settle as local.
            failure = type(exc).__name__
            for episode in judge.episodes():
                if episode["status"] == "completed":
                    try:
                        scores.append(_extract_judge_result(episode["final_response"]).model_dump())
                    except ValueError:
                        pass
            native = {
                "work_session_id": worker_sid,
                "judge_session_id": judge_sid,
                "success": False,
                "content_score": scores[-1]["score"] if scores else None,
                "turns": len(worker.episodes()),
                "workspace_dir": str(workspace),
            }
        finally:
            # A global interruption keeps running receipts and durable journals for resume.
            if failure is None and (self.shared.root / "pause.json").exists():
                try:
                    self.shared.check()
                except BudgetExceeded:
                    update_cell(self.root, cell_key, status="paused", finished=time.time())
        episodes = worker.episodes()
        usage = {
            "model_calls": sum(e["usage"]["model_calls"] for e in episodes),
            "leaf_calls": sum(e["usage"]["leaf_calls"] for e in episodes),
            "unknown_calls": 0,
            "pending_calls": 0,
            "native_evaluation": evaluation_key,
        }
        native["tool_calls"] = worker.count_tools()
        row = {
            "native": native,
            "healthy": failure is None,
            "error_type": failure,
            "content_score_scope": "final"
            if failure is None
            else "intermediate"
            if scores
            else "unjudged",
            "public_feedback": [
                m["content"]
                for m in json.loads((worker.root / "chat.json").read_text())["history"][2:]
                if m["role"] == "user"
            ]
            if (worker.root / "chat.json").exists()
            else [],
            "intermediate_judges": scores,
            "evidence_key": evidence_key,
            "kernel_id": kernel.content_id if kernel else None,
        }
        episode = EpisodeResult(
            "completed" if failure is None else "failed",
            episodes[-1]["final_response"] if episodes else "",
            tuple(episodes[-1]["artifacts"]) if episodes else (),
            tuple(e for ep in episodes for e in ep["events"]),
            usage,
        )
        update_cell(
            self.root,
            cell_key,
            status="completed" if failure is None else "failed",
            finished=time.time(),
            native=native,
            healthy=row["healthy"],
            error_type=failure,
            content_score_scope=row["content_score_scope"],
            intermediate_judges=scores,
        )
        atomic_json(evaluation, row)
        saved = {
            "episode": episode.to_dict(),
            "row": row,
            "workspace": {
                str(p.relative_to(workspace)): sha(p) for p in workspace.rglob("*") if p.is_file()
            },
        }
        atomic_json(receipt, dict(saved, sha256=digest(saved)))
        return episode

    def load_kernel(self, kernel, evidence_key):
        kernel.verify()
        root = self.root / "loads" / evidence_key / "bundle"
        for source in sorted(kernel.root.rglob("*")):
            if source.is_file():
                target = root / source.relative_to(kernel.root)
                if target.exists() and target.read_bytes() != source.read_bytes():
                    raise ValueError("Previously loaded assets changed")
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
        loaded = AgentKernelBundle(root)
        if loaded.content_id != kernel.content_id:
            raise ValueError("Fresh selected asset identity differs")
        receipt = root.parent / "load-receipt.json"
        if not receipt.exists():
            atomic_json(
                receipt,
                {
                    "kernel_id": loaded.content_id,
                    "source": str(kernel.root),
                    "source_sha256": digest(self.source),
                },
            )
        return loaded

    async def evolve(self):
        selected_path = self.root / "selected-state.json"
        if selected_path.exists():
            state = json.loads(selected_path.read_text())
            selected = AgentKernelBundle(state["bundle_root"])
            selected.verify()
            if selected.content_id != state["kernel_id"] or state["source_sha256"] != digest(
                self.source
            ):
                raise ValueError("Frozen selected assets or source identity changed")
            return selected
        warmup = await self.execute(
            self.dataset.task("q1"), self.initial, "campaign", "train", "warmup-q1"
        )
        summary = {"status": "failed_training", "healthy": False}
        selected = self.initial
        if warmup.status == "completed":
            controller = CampaignController(
                self.dataset,
                self.evaluator_factory,
                self.shared.cfg,
                RunConfig(
                    temperature=0,
                    concurrency=1,
                    max_tokens=self.max_tokens,
                    wiki_max_tokens=self.max_tokens,
                ),
                AgentExperimentSpec(
                    train=("q1",),
                    validation=("q2",),
                    admission=("q1",),
                    rounds=1,
                    max_episode_runs=6,
                    proposal_call_limit=1,
                ),
                self.root / "campaign",
                frozen_files=[Path(__file__).parent],
                client_factory=self.client_factory,
                wiki_call_limit=4,
                backend=LocalEpisodeBackend(self, self.root / "campaign-template"),
            )
            try:
                summary = await controller.run(
                    self.initial, resume=(self.root / "campaign/agent-campaign.json").exists()
                )
                if summary.get("status") == "complete":
                    selected = AgentKernelBundle(self.root / "campaign/selected")
            except Exception as exc:
                self.shared.check()
                summary = {
                    "status": "failed_campaign",
                    "healthy": False,
                    "error_type": type(exc).__name__,
                }
        self.shared.check()
        # q2 is always diagnosed, even when training/proposal is unavailable. Reuse
        # completed K0 validation, without exposing its private criteria to optimizer.
        await self.execute(
            self.dataset.task("q2"), self.initial, "campaign", "validation", "diagnostic-q2"
        )
        state = {
            "kernel_id": selected.content_id,
            "bundle_root": str(selected.root),
            "fallback": selected.content_id == self.initial.content_id,
            "campaign": summary,
            "source_sha256": digest(self.source),
        }
        atomic_json(selected_path, state)
        return selected

    async def run_confirmation(self, selected=None):
        """q6-only frozen confirmation: no training, proposal, Wiki or evolution."""
        if self.mode != "confirmation":
            raise ValueError("Confirmation must use its separate mode/root/dataset")
        self.verify()
        origin_identity = self.selection_run_root / "integration-identity.json"
        origin_selection = self.selection_run_root / "selected-state.json"
        origin_report = self.selection_run_root / "all-pair-report.json"
        origin = json.loads(origin_identity.read_text())
        selection = json.loads(origin_selection.read_text())
        development = json.loads(origin_report.read_text())
        compared = [row for row in development["rows"] if row["task_id"] == "q5"]
        if (
            development["status"] not in {"complete", "finished_with_failures"}
            or {row["arm"] for row in compared} != {"A", "B", "C"}
            or len(compared) != 3
            or any(row["status"] not in {"completed", "failed"} for row in compared)
        ):
            raise ValueError("Independent confirmation requires completed development scheduling")
        if (
            origin["mode"] != "local-native-task-adaptation-v1"
            or origin["source"] != self.source
            or origin["initial"] != self.initial.content_id
            or origin["transport"] != self.shared.identity()
            or origin["generation_max_tokens"] != self.max_tokens
        ):
            raise ValueError("Confirmation requires the same frozen development source and K0")
        if selection["source_sha256"] != digest(self.source):
            raise ValueError("Selected development source changed")
        if selected is None:
            selected = AgentKernelBundle(selection["bundle_root"])
        selected.verify()
        if selected.content_id != selection["kernel_id"]:
            raise ValueError(
                "Confirmation selected kernel differs from frozen development selection"
            )
        frozen_origin = snapshot_files([origin_identity, origin_selection, origin_report])
        proof_path = self.root / "confirmation-origin.json"
        proof = {"source": frozen_origin, "selected": selected.content_id, "training": "not_run"}
        if proof_path.exists() and json.loads(proof_path.read_text()) != proof:
            raise ValueError("Frozen confirmation origin changed")
        if not proof_path.exists():
            atomic_json(proof_path, proof)
        selected_path = self.root / "selected-state.json"
        frozen_selection = {
            "kernel_id": selected.content_id,
            "bundle_root": str(selected.root),
            "fallback": selection["fallback"],
            "campaign": {"status": "not_run_independent_confirmation"},
            "source_sha256": digest(self.source),
        }
        if selected_path.exists() and json.loads(selected_path.read_text()) != frozen_selection:
            raise ValueError("Frozen confirmation selection changed")
        if not selected_path.exists():
            atomic_json(selected_path, frozen_selection)
        for arm in ("A", "B", "C"):
            key = digest({"task_id": "q6", "arm": arm, "phase": "holdout", "run_id": self.run_id})[
                :24
            ]
            if not (self.root / "cells" / (key + ".json")).exists():
                cell(self.root, "q6", arm, "holdout", self.run_id)
        with execution_lease(self.root / "local-run.lock"):
            try:
                for arm, kernel in (("A", None), ("B", self.initial), ("C", selected)):
                    assert_files(frozen_origin)
                    await self.execute(self.dataset.task("q6"), kernel, arm, "holdout", "q6-" + arm)
            finally:
                self.write_reports()
        return json.loads((self.root / "all-pair-report.json").read_text())

    async def run(self):
        if self.mode == "confirmation":
            return await self.run_confirmation()
        self.verify()
        for task_id, arm, phase in (
            ("q1", "campaign", "train"),
            ("q2", "campaign", "validation"),
            ("q5", "A", "holdout"),
            ("q5", "B", "holdout"),
            ("q5", "C", "holdout"),
        ):
            path_key = digest(
                {"task_id": task_id, "arm": arm, "phase": phase, "run_id": self.run_id}
            )[:24]
            if not (self.root / "cells" / (path_key + ".json")).exists():
                cell(self.root, task_id, arm, phase, self.run_id)
        with execution_lease(self.root / "local-run.lock"):
            try:
                selected = await self.evolve()
                for arm, kernel in (("A", None), ("B", self.initial), ("C", selected)):
                    await self.execute(self.dataset.task("q5"), kernel, arm, "holdout", "q5-" + arm)
            finally:
                self.write_reports()
        return json.loads((self.root / "all-pair-report.json").read_text())

    def write_reports(self):
        value = report(self.root, self.shared)
        self.verify()
        state_path = self.root / "selected-state.json"
        value["selected"] = json.loads(state_path.read_text()) if state_path.exists() else None
        value["execution_mode"] = self.frozen_identity["mode"]
        value["worker_session_cap"] = self.shared.worker_limit
        value["worker_turn_model_cap"] = 8
        value["generation_max_tokens"] = self.max_tokens
        histories = {}
        for path in sorted((self.shared.root / "requests").glob("*.json")):
            request = json.loads(path.read_text())
            histories.setdefault(request.get("run_id", "unidentified"), []).append(request)
        value["global_request_histories"] = {}
        for run_id, requests in histories.items():
            roles = {}
            for role in sorted({r.get("role", "unidentified") for r in requests}):
                records = [r for r in requests if r.get("role", "unidentified") == role]
                counts = [token_count(r.get("usage") or {}) for r in records]
                roles[role] = {
                    "requests": len(records),
                    "actual_http_attempts": available_sum(
                        r.get("actual_http_attempts") for r in records
                    ),
                    "tokens": available_sum(counts),
                    "known_tokens_subtotal": sum(c for c in counts if c is not None),
                    "unknown_token_requests": sum(c is None for c in counts),
                    "elapsed_s": available_sum(
                        r["finished"] - r["started"] if "finished" in r else None for r in records
                    ),
                }
            value["global_request_histories"][run_id] = {
                "requests": len(requests),
                "roles": roles,
                "statuses": {
                    status: sum(r["status"] == status for r in requests)
                    for status in sorted({r["status"] for r in requests})
                },
            }
        for row in value["rows"]:
            native = row.get("native")
            row["correction_turns"] = max(0, native["turns"] - 1) if native else None
            worker_chats = [
                self.root / "sessions" / sid / "chat.json"
                for sid in row["sessions"]
                if sid.startswith("worker-")
            ]
            row["worker_macro_invocations"] = (
                sum(
                    event["kind"] == "macro"
                    for path in worker_chats
                    for turn in json.loads(path.read_text())["turns"]
                    for event in turn["episode"]["events"]
                )
                if worker_chats and all(p.exists() for p in worker_chats)
                else None
            )
        value["native_tool_count_semantics"] = "worker_primitive_leaf_calls"
        value["macro_count_semantics"] = (
            "completed_worker_F_invocations_in_receipted_episode_events"
        )
        comparison_task = "q6" if self.mode == "confirmation" else "q5"
        holdout = [r for r in value["rows"] if r["task_id"] == comparison_task]
        value["comparison_task"] = comparison_task
        if self.mode == "confirmation":
            value["q6"] = "independent_confirmation"
            value["status"] = (
                "paused"
                if value["status"] == "paused"
                else "finished_with_failures"
                if any(r["status"] == "failed" for r in holdout)
                else "complete"
                if len(holdout) == 3 and all(r["status"] == "completed" for r in holdout)
                else "incomplete"
            )
        value["evaluation_healthy"] = (
            bool(holdout) and len(holdout) == 3 and all(r.get("healthy") for r in holdout)
        )
        value["comparison_verification_complete"] = (
            value["status"] in {"complete", "finished_with_failures"}
            and value["evaluation_healthy"]
            and value["model_evidence"] == "host_transport"
        )
        value["q5_verification_complete"] = (
            self.mode == "development" and value["comparison_verification_complete"]
        )
        value["acceptance_complete"] = value["comparison_verification_complete"]
        value["acceptance_scope"] = (
            "local_" + comparison_task + "_A_B_C_execution_verification_only"
        )
        value["quality_improvement_claim"] = False
        value["evolution_complete"] = (
            bool(value["selected"]) and value["selected"]["campaign"].get("status") == "complete"
        )
        value["evolution_adopted"] = (
            self.mode == "development"
            and bool(value["selected"])
            and not value["selected"]["fallback"]
        )
        value["source_sha256"] = digest(self.source)
        atomic_json(self.root / "all-pair-report.json", value)
        fields = [
            "task_id",
            "arm",
            "status",
            "healthy",
            "success",
            "content_score",
            "final_content_score",
            "last_intermediate_content_score",
            "worker_macro_invocations",
            "turns",
            "correction_turns",
            "worker_tokens",
            "judge_tokens",
            "worker_time_s",
            "judge_time_s",
            "task_time_s",
            "native_tool_calls",
            "flattened_leaf_calls",
            "content_score_scope",
        ]
        with (self.root / "all-pair-report.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in value["rows"]:
                writer.writerow({k: row.get(k, (row.get("native") or {}).get(k)) for k in fields})
        lines = [
            "# Local LIFT all-pair evidence",
            "",
            f"Status: {value['status']}; acceptance complete: {value['acceptance_complete']}",
            f"Worker model requests: {self.shared.worker_limit} cumulative/session; 8 per native turn; generation max_tokens: {self.max_tokens} all roles",
            f"HTTP attempts: {value['actual_http_attempts']}/{self.shared.cfg.max_http_requests}; evaluation healthy: {value['evaluation_healthy']}",
            "q6: sealed, no independent generalization or measured improvement claim."
            if self.mode == "development"
            else "q6: separate independent confirmation; no training or measured improvement claim.",
            f"Acceptance scope: local {comparison_task} A/B/C execution verification only; release acceptance remains separate.",
            "Recorded scores are synthetic."
            if value["model_evidence"] == "recorded_offline"
            else "Scores use the authorized host model and unchanged native content judge.",
            f"Evolution complete: {value['evolution_complete']}; candidate adopted: {value['evolution_adopted']}",
            "",
            "Native/flattened tool counts both count primitive leaves. Macro invocations count completed F compositions separately.",
            "| Task | Arm | Status | Success | Final content | Last intermediate content | Score scope | Macro invocations |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in value["rows"]:
            native = row.get("native") or {}
            lines.append(
                f"| {row['task_id']} | {row['arm']} | {row['status']} | {native.get('success')} | "
                + (
                    str(row["final_content_score"])
                    if row.get("final_content_score") is not None
                    else "null"
                )
                + " | "
                + (
                    str(row["last_intermediate_content_score"])
                    if row.get("last_intermediate_content_score") is not None
                    else "null"
                )
                + f" | {row.get('content_score_scope', 'unjudged')} | {row.get('worker_macro_invocations')} |"
            )
        lines.extend(
            [
                "",
                "Selected state and fallback:",
                "```json",
                json.dumps(value["selected"], indent=2),
                "```",
                "",
                "All roles and actual measured costs:",
                "```json",
                json.dumps(value["roles"], indent=2),
                "```",
                "",
                "Global request histories (including prior preserved runs):",
                "```json",
                json.dumps(value["global_request_histories"], indent=2),
                "```",
            ]
        )
        (self.root / "all-pair-report.md").write_text("\n".join(lines) + "\n")


def build_runner(config, run_id):
    from darwinagent.config import Config
    from darwinagent.llm.client import LLMClient

    from .kernel import initial_kernel
    from .transport import SharedTransport

    for key, default in (
        ("correction_rounds", 3),
        ("temperature", 0),
    ):
        if config.get(key, default) != default:
            raise ValueError("Local pilot requires fixed " + key)
    worker_limit = config.get("worker_limit", 8)
    if type(worker_limit) is not int or worker_limit not in (8, 16):
        raise ValueError("Worker limit defaults to8;16 requires explicit audited authorization")
    max_tokens = config.get("max_tokens", 4096)
    if type(max_tokens) is not int or max_tokens not in (4096, 16384):
        raise ValueError("Local max_tokens must be explicit4096 or16384")
    if worker_limit == 16 and max_tokens != 16384:
        raise ValueError("Cumulative16 requires explicit max_tokens16384")
    http_cap = config.get("http_cap", 100)
    if type(http_cap) is not int or http_cap not in (100, 500):
        raise ValueError("HTTP cap defaults to 100; 500 requires explicit audited authorization")
    model, endpoint = os.environ["DARWINAGENT_MODEL"], os.environ["DARWINAGENT_BASE_URL"]
    if model != "glm-5.3-flash" or model != config["model"] or endpoint != config["base_url"]:
        raise ValueError("Model/endpoint differs from the authorized frozen config")
    root, budget = Path(config["run_root"]).resolve(), Path(config["budget_root"]).resolve()
    mode = config.get("mode", "development")
    selection_root = config.get("selection_run_root")
    if mode == "confirmation" and not selection_root:
        raise ValueError("Confirmation config requires the frozen development selection_run_root")
    if root.is_relative_to(budget) or budget.is_relative_to(root):
        raise ValueError("budget_root must be independent of run_root")
    cfg = Config(
        api_base_url=endpoint,
        api_key=os.environ["DARWINAGENT_API_KEY"],
        model_strong=model,
        model_middle=model,
        model_fast=model,
        max_retries=1,
        max_concurrency=1,
        max_http_requests=http_cap,
        request_budget_path=budget / "http-attempts.json",
        work_dir=root / "transport",
        model_profiles=False,
    )
    cfg.role_tiers.update(worker="strong", judge="strong")
    shared = SharedTransport(LLMClient(cfg), budget / "shared", worker_limit=worker_limit)
    initial = (
        AgentKernelBundle(Path(selection_root) / "K0")
        if mode == "confirmation"
        else AgentKernelBundle(root / "K0")
        if (root / "K0").exists()
        else initial_kernel(root / "K0")
    )
    return LocalRunner(
        dataset=LiftConfirmationDataset(config["data_root"])
        if mode == "confirmation"
        else LiftDataset(config["data_root"]),
        shared=shared,
        root=root,
        initial=initial,
        run_id=run_id,
        mode=mode,
        selection_run_root=selection_root,
        max_tokens=max_tokens,
    )


async def run_and_close(runner):
    """Own execution and HTTP connection cleanup on one still-running loop."""
    failure = None
    try:
        return await runner.run()
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await runner.shared.client.aclose()
        except Exception as exc:
            if failure is None:
                raise
            failure.add_note("Client cleanup failed: " + type(exc).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--config", default=os.environ.get("DARWIN_LIFT_CONFIG"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if not args.config:
        parser.error("--config or DARWIN_LIFT_CONFIG is required")
    configure_native(args.upstream)
    runner = build_runner(json.loads(Path(args.config).read_text()), args.run_id)
    asyncio.run(run_and_close(runner))


if __name__ == "__main__":
    main()
