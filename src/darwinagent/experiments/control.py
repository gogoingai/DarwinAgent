"""Human interventions and scoped execution previews, without model calls."""

from __future__ import annotations

import json
from pathlib import Path

from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.execution import ExecutionPlan, ExecutionSelection
from darwinagent.runtime.workspace import Workspace


def _recordable(value):
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {
                "api_key",
                "apikey",
                "password",
                "secret",
                "access_token",
                "authorization",
                "credentials",
                "credential",
            } or normalized.endswith(("_api_key", "_password", "_secret", "_access_token")):
                out[str(key) + "_changed"] = True
            else:
                out[key] = _recordable(child)
        return out
    if isinstance(value, (list, tuple)):
        return [_recordable(child) for child in value]
    return value


def intervene(root, change, *, branch="main", expected_revision=None):
    workspace = Workspace(Path(root) / "workspace")
    try:
        selected = workspace.branch(branch)
    except KeyError:
        selected = workspace.create_branch(branch)
    # Invalid asset drafts are also durable. Validation belongs to admission, not registration.
    draft = workspace.put_json(_recordable(change))
    event = workspace.append_event(
        "human_intervention",
        {
            "branch": branch,
            "draft_ref": draft,
            "kind": change.get("kind", "configuration"),
            "safe_boundary": True,
        },
    )
    candidate = change.get("candidate")
    draft_error = None
    if change.get("kind") == "assets" and change.get("assets") is not None:
        from uuid import uuid4

        from darwinagent.kernel.assets import Asset, KernelAssets

        try:
            values = tuple(Asset(**row) for row in change["assets"])
            assets = KernelAssets(
                values,
                origin={"kind": "human", "draft_ref": draft, "parent": selected.get("working")},
            )
            temporary = Path(root) / "workspace" / "candidate-drafts" / uuid4().hex
            bundle = assets.export(temporary)
            destination = Path(root) / "versions" / bundle.version
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.rename(destination)
            candidate = bundle.version
            workspace.append_event(
                "human_candidate",
                {
                    "candidate": candidate,
                    "path": str(destination),
                    "content_id": assets.content_id,
                    "draft_ref": draft,
                },
            )
        except (ValueError, TypeError, KeyError) as exc:
            draft_error = f"{type(exc).__name__}: {exc}"
            workspace.append_event(
                "human_draft_not_executable", {"draft_ref": draft, "error": draft_error}
            )
    if change.get("kind") == "fork":
        selected = workspace.create_branch(change["name"], parent=branch)
    elif change.get("kind") in ("select", "assets") and candidate:
        selected = workspace.select_branch(
            branch,
            expected_revision=selected["revision"]
            if expected_revision is None
            else expected_revision,
            working=candidate,
            source="human",
        )
    if change.get("kind") in ("wiki", "wiki_correction", "correction"):
        apply_wiki_controls(root, load_controls(root, branch=branch))
    return {
        "event": event,
        "draft_ref": draft,
        "branch": selected,
        "requires_restart": change.get("kind") == "code",
        "draft_error": draft_error,
    }


def preview(root, cases, selection=None):
    """Read-only plan; branch ancestry predicts the Pipeline's lazy references."""
    import sqlite3
    from contextlib import closing

    selection = selection or ExecutionSelection()
    result = ExecutionPlan(selection=selection.to_dict())
    generation_root = Path(root) / "generation"
    if not generation_root.exists() and (Path(root) / "B0" / "generation").exists():
        generation_root = Path(root) / "B0" / "generation"
    database = Path(root) / "workspace" / "workspace.sqlite3"
    parents = {}
    if database.exists():
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
            parents = dict(db.execute("SELECT name,parent FROM branches"))
    if selection.mode == "fork" and selection.branch not in parents and "main" in parents:
        parents[selection.branch] = "main"

    def saved_path(case_id, question):
        branch, seen = selection.branch, set()
        while branch and branch not in seen:
            if any(character in branch for character in ("/", "\\")) or branch in {".", ".."}:
                raise ValueError("Invalid branch ancestry")
            seen.add(branch)
            path = (
                generation_root
                / case_id
                / "branches"
                / branch
                / "answers"
                / (digest(question.to_dict()) + ".json")
            )
            if path.exists():
                return path, branch
            branch = parents.get(branch)
        return None, None

    for case in cases:
        if not selection.includes(case.id):
            continue
        selected_questions = [q for q in case.questions if selection.includes(case.id, q.id)]
        cached_case = (
            selection.mode in ("continue", "fork")
            and "answer" in selection.stages
            and bool(selected_questions)
            and all(saved_path(case.id, q)[0] is not None for q in selected_questions)
        )
        for question in case.questions:
            if not selection.includes(case.id, question.id):
                continue
            path, source_branch = saved_path(case.id, question)
            row = json.loads(path.read_text()) if path is not None else None
            if row is not None and (
                row.get("question_version") != digest(question.to_dict())
                or digest(row["result"]) != row["digest"]
            ):
                result.missing.append(
                    {
                        "case_id": case.id,
                        "question_id": question.id,
                        "requires": "valid saved answer",
                        "source": str(path),
                    }
                )
                continue
            if (
                row is not None
                and row.get("graph_path")
                and row["result"]["status"] != "execution_error"
            ):
                graph = Path(row["graph_path"])
                if not graph.exists() or digest(json.loads(graph.read_text())) != row.get(
                    "graph_fingerprint"
                ):
                    result.missing.append(
                        {
                            "case_id": case.id,
                            "question_id": question.id,
                            "requires": "unchanged original graph",
                            "source": str(graph),
                        }
                    )
                    continue
            for stage in selection.stages:
                item = {"case_id": case.id, "question_id": question.id, "stage": stage}
                if cached_case and stage in (
                    "facts",
                    "vector",
                    "graph",
                    "retrieval",
                    "check",
                    "review",
                ):
                    result.reuse.append({**item, "status": "not_required_for_saved_answer"})
                elif stage == "answer":
                    failed = row is not None and row["result"]["status"] == "execution_error"
                    if row is not None and selection.mode in ("continue", "fork"):
                        result.reuse.append(
                            {
                                **item,
                                "status": row["result"]["status"],
                                "source": str(path),
                                "source_branch": source_branch,
                            }
                        )
                    elif selection.mode == "retry_failed" and not failed:
                        result.reuse.append({**item, "status": "not_selected_for_retry"})
                    else:
                        result.execute.append(item)
                elif (
                    stage in ("score", "check", "review")
                    and row is None
                    and "answer" not in selection.stages
                ):
                    result.missing.append(
                        {
                            **item,
                            "requires": "saved answer",
                            "suggestion": "select answer explicitly",
                        }
                    )
                else:
                    result.execute.append(item)
    return result


class ControlSignal(RuntimeError):
    """Durable human pause, stop, or restart at an execution boundary."""

    continuation_signal = True

    def __init__(self, state):
        self.state = dict(state)
        super().__init__("Execution " + state.get("status", "paused"))


def load_controls(root, *, branch="main"):
    """Reduce only the selected branch's human events without opening an empty store."""
    root = Path(root)
    state = {
        "branch": branch,
        "sequence": 0,
        "run_config": {},
        "connection_config": {},
        "policy": {},
        "evaluation": {},
        "objective": None,
        "paused": False,
        "stopped": False,
        "code_sequence": 0,
        "corrections": [],
    }
    if not (root / "workspace/workspace.sqlite3").exists():
        return state
    workspace = Workspace(root / "workspace")
    for event in workspace.events(kind="human_intervention"):
        payload = event["payload"]
        if payload.get("branch", "main") != branch:
            continue
        draft = workspace.read_json(payload["draft_ref"])
        kind = draft.get("kind", payload.get("kind", "configuration"))
        state["sequence"] = event["seq"]
        if kind in ("configuration", "config", "model", "connection"):
            state["run_config"].update(draft.get("run_config", {}))
            state["connection_config"].update(
                {
                    k: v
                    for k, v in draft.get("connection_config", {}).items()
                    if not k.endswith("_changed")
                }
            )
        elif kind in ("policy", "selection_policy"):
            state["policy"].update(draft.get("policy", {}))
        elif kind in ("evaluation", "evaluation_policy"):
            state["evaluation"].update(draft.get("evaluation_config", draft.get("evaluation", {})))
        elif kind in ("objective", "goal"):
            state["objective"] = draft.get("objective", draft.get("goal", draft.get("direction")))
        elif kind == "pause":
            state["paused"] = True
        elif kind == "stop":
            state["stopped"] = True
        elif kind in ("resume", "continue"):
            state["paused"] = state["stopped"] = False
        elif kind == "code":
            state["code_sequence"] = event["seq"]
        elif kind in ("wiki", "wiki_correction", "correction"):
            state["corrections"].append({"sequence": event["seq"], "draft": draft})
    return state


def apply_wiki_controls(root, state):
    """Apply corrections once; invalid drafts remain explicit evidence, not global failures."""
    if not state["corrections"]:
        return
    from .wiki_service import WikiService

    workspace = Workspace(Path(root) / "workspace")
    processed = {
        event["payload"]["sequence"]
        for event in workspace.events()
        if event["kind"] in ("wiki_control_applied", "wiki_control_rejected")
        and event["payload"].get("branch", "main") == state["branch"]
    }
    service = WikiService(root)
    for correction in state["corrections"]:
        sequence, draft = correction["sequence"], correction["draft"]
        if sequence in processed:
            continue
        try:
            text = draft.get("text", draft.get("correction"))
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Wiki correction requires nonempty text")
            refs = draft.get("source_refs", ())
            target = draft.get("target_ref")
            if target:
                result = service.correct(target, text, source_refs=refs)
            else:
                result = service.register(
                    {"correction": text},
                    scope=draft.get("scope", {}),
                    source_kind="correction",
                    source_refs=refs,
                )
        except (ValueError, KeyError, FileNotFoundError, PermissionError) as exc:
            workspace.append_event(
                "wiki_control_rejected",
                {
                    "sequence": sequence,
                    "branch": state["branch"],
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        else:
            workspace.append_event(
                "wiki_control_applied",
                {
                    "sequence": sequence,
                    "branch": state["branch"],
                    "result": result,
                },
            )
