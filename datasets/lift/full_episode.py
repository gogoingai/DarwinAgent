"""Full native episode execution and immutable terminal replay."""

import json
import time

from darwinagent.agent import EpisodeResult
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.diagnostics import exception_evidence
from darwinagent.runtime.steps import UnknownRequest

from .data import sha
from .full_agents import FullDarwinChatAgent
from .full_tools import FullLocalTools
from .full_trajectory import (
    build_native_judge_input,
    extract_trajectory,
    score_trajectory_with_retries,
)
from .native_date import native_date_scope
from .report import available_sum


def workspace_hashes(root):
    if any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Episode workspace symlink rejected")
    return {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()}


async def execute_episode(owner, task, kernel, arm, phase):
    from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
    from src.lift.eval.task_exec import execute_task
    from src.lift.eval.worker_judger import WorkerJudgerPair

    owner.verify()
    if arm == "campaign" and kernel.content_id != owner.initial.content_id:
        arm = "candidate-" + kernel.content_id[:16]
    key = digest(
        dict(task=task.id, kernel=kernel.content_id if kernel else None, arm=arm, phase=phase)
    )[:24]
    directory = owner.root / "episodes" / key
    receipt = directory / "receipt.json"
    workspace = directory / "workspace"
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved["sha256"] != digest({k: v for k, v in saved.items() if k != "sha256"}):
            raise ValueError("Settled full episode receipt changed")
        calendar = saved["row"].get("native_calendar")
        if calendar is not None and calendar["sha256"] != sha(directory / "native-date.json"):
            raise ValueError("Settled native evaluation date changed")
        if saved["workspace"] != workspace_hashes(workspace):
            raise ValueError("Settled full episode workspace changed")
        cell_path = owner.cell_path(task.id, arm, phase)
        if not cell_path.exists() or json.loads(cell_path.read_text()) != saved["row"]:
            atomic_json(cell_path, saved["row"])
        return EpisodeResult(**saved["episode"]), saved["row"]
    owner.shared.check()
    cell_path = owner.cell_path(task.id, arm, phase)
    row = (
        json.loads(cell_path.read_text())
        if cell_path.exists()
        else dict(task_id=task.id, arm=arm, phase=phase, status="not_started")
    )
    row.update(status="running", started=row.get("started", time.time()), evidence_key=key)
    atomic_json(cell_path, row)
    owner.dataset.stage(task, workspace)
    if kernel:
        kernel = owner.load_kernel(kernel, key)
    sid = digest(dict(run_id=owner.run_id, root=str(owner.root), key=key))[:24]
    worker_sid, judge_sid = "worker-" + sid, "judge-" + sid
    worker = FullDarwinChatAgent(
        task,
        FullLocalTools(workspace).specs(),
        owner.shared,
        directory / "worker",
        kernel,
        role="worker",
        run_id=owner.run_id,
        session=worker_sid,
        cell_id=key,
        arm=arm,
        phase=phase,
        task_policy=owner.task_policy,
    )
    judge = FullDarwinChatAgent(
        task,
        FullLocalTools(workspace, readonly=True).specs(),
        owner.shared,
        directory / "judge",
        None,
        role="judge",
        run_id=owner.run_id,
        session=judge_sid,
        cell_id=key,
        arm=arm,
        phase=phase,
        task_policy=owner.task_policy,
    )
    native_task = owner.dataset.privileged_task(
        task.id, split=owner.dataset.task_metadata(task.id)["split"]
    )
    pair = WorkerJudgerPair(worker, judge, worker_sid, judge_sid)

    async def factory(native):
        return pair

    scores = []

    def on_turn(index, prompt, result, scored):
        scores.append(scored.model_dump())
        path = directory / "native-turns" / (str(index) + ".json")
        if not path.exists():
            atomic_json(path, scored.model_dump())

    error = None
    diagnostics = None
    native = None
    trajectory = None
    native_calendar = None
    try:
        native_phase = (
            SuiteRunPhase.warmup()
            if phase in ("train", "validation")
            else (
                SuiteRunPhase.holdout(
                    HoldoutLoadState.EVOLVED if arm == "C" else HoldoutLoadState.BASELINE
                )
            )
        )
        with native_date_scope(
            directory,
            {
                "run_id": owner.run_id,
                "task_id": task.id,
                "kernel_id": kernel.content_id if kernel else None,
                "cell_id": key,
            },
        ) as calendar_date:
            native_calendar = {"date": calendar_date, "sha256": sha(directory / "native-date.json")}
            native = (
                await execute_task(
                    task=native_task,
                    run_id=owner.run_id,
                    workspace_dir=workspace,
                    factory=factory,
                    run_phase=native_phase,
                    max_conversation_turns=owner.correction_rounds,
                    on_turn=on_turn,
                )
            ).model_dump()
        if native["success"] and native["content_score"] != 1:
            raise ValueError("Native success requires final content score 1")
        transcript = extract_trajectory(worker.root, include_snapshots=False)
        judge_input = build_native_judge_input(
            transcript,
            task_query=native_task.query,
            trajectory_reqs=native_task.expected_result.trajectory_reqs,
            source_hashes=owner.source,
            value_projection=True,
            readable_value_projection=True,
        )
        trajectory = await score_trajectory_with_retries(
            owner.shared,
            "trajectory-" + sid,
            directory / "trajectory",
            judge_input,
            stop_on_saturated_empty=True,
        )
        if trajectory["status"] != "completed":
            raise ValueError("Native trajectory judgment unhealthy")
    except UnknownRequest:
        owner.shared.pause("unknown")
        raise
    except Exception as exc:
        owner.shared.check()  # An unknown or budget gate always escapes local failure handling.
        error = type(exc).__name__
        diagnostics = exception_evidence(exc, stage="full_native")
    episodes = worker.episodes()
    usage = dict(
        model_calls=available_sum(e["usage"]["model_calls"] for e in episodes),
        leaf_calls=available_sum(e["usage"]["leaf_calls"] for e in episodes),
        unknown_calls=available_sum(e["usage"].get("unknown_calls") for e in episodes),
        pending_calls=available_sum(e["usage"].get("pending_calls") for e in episodes),
        full_evidence=key,
    )
    episode = EpisodeResult(
        "failed" if error else "completed",
        episodes[-1]["final_response"] if episodes else "",
        tuple(episodes[-1]["artifacts"]) if episodes else (),
        tuple(event for ep in episodes for event in ep["events"]),
        usage,
    )
    row.update(
        status=episode.status,
        healthy=error is None,
        error=error,
        diagnostics=diagnostics,
        native=native,
        trajectory=trajectory,
        native_calendar=native_calendar,
        intermediate_judges=scores,
        finished=time.time(),
        kernel_id=kernel.content_id if kernel else None,
        usage=usage,
        public_feedback=[
            m["content"]
            for m in json.loads((worker.root / "chat.json").read_text())["history"][2:]
            if m["role"] == "user"
        ]
        if (worker.root / "chat.json").exists()
        else [],
    )
    saved = dict(episode=episode.to_dict(), row=row, workspace=workspace_hashes(workspace))
    atomic_json(receipt, dict(saved, sha256=digest(saved)))
    atomic_json(cell_path, row)
    return episode, row


_execute_native_episode = execute_episode


async def execute_episode(owner, task, kernel, arm, phase):
    """Own the whole standalone episode, including startup checks and writes."""
    with owner.shared.campaign_lease():
        return await _execute_checked_episode(owner, task, kernel, arm, phase)


async def _execute_checked_episode(owner, task, kernel, arm, phase):
    """Settle setup failures locally; frozen identity and shared gates remain global."""
    owner.verify()
    owner.shared.check()
    effective_arm = (
        "candidate-" + kernel.content_id[:16]
        if arm == "campaign" and kernel.content_id != owner.initial.content_id
        else arm
    )
    key = digest(
        dict(
            task=task.id,
            kernel=kernel.content_id if kernel else None,
            arm=effective_arm,
            phase=phase,
        )
    )[:24]
    directory = owner.root / "episodes" / key
    receipt = directory / "receipt.json"
    try:
        return await _execute_native_episode(owner, task, kernel, arm, phase)
    except UnknownRequest:
        owner.shared.pause("unknown")
        raise
    except Exception as exc:
        owner.verify()
        owner.shared.check()
        if receipt.exists():
            raise  # Terminal receipt/integrity errors are never recast as local failures.
        row = dict(
            task_id=task.id,
            arm=effective_arm,
            phase=phase,
            status="failed",
            healthy=False,
            error=type(exc).__name__,
            diagnostics=exception_evidence(exc, stage="full_setup"),
            native=None,
            trajectory=None,
            intermediate_judges=[],
            finished=time.time(),
            evidence_key=key,
            kernel_id=kernel.content_id if kernel else None,
            public_feedback=[],
            metadata=owner.dataset.task_metadata(task.id),
        )
        usage = dict(
            model_calls=0, leaf_calls=0, unknown_calls=0, pending_calls=0, full_evidence=key
        )
        episode = EpisodeResult("failed", "", (), (), usage)
        row["usage"] = usage
        saved = dict(
            episode=episode.to_dict(), row=row, workspace=workspace_hashes(directory / "workspace")
        )
        atomic_json(receipt, dict(saved, sha256=digest(saved)))
        atomic_json(owner.cell_path(task.id, effective_arm, phase), row)
        return episode, row
