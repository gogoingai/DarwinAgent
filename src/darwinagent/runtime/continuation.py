"""Safe registration of known flat checkpoints and branch progress references.

Unlike generic legacy archival, this adapter knows Pipeline checkpoint shapes.
It never guesses a question version from its ID: legacy data must carry a saved
case or the caller must supply the original source case explicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from darwinagent.contracts import AnswerResult

from .artifacts import atomic_json, digest
from .workspace import Workspace


def _branch_name(value):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or any(part in value for part in ("/", "\\", "\x00"))
    ):
        raise ValueError("Invalid progress branch name")


def _immutable_copy(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".retained-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            Workspace._sync_directory(path.parent)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Retained checkpoint content conflict") from None
    finally:
        os.unlink(temporary)


def _question_versions(source_case):
    if hasattr(source_case, "questions"):
        return {q.id: digest(q.to_dict()) for q in source_case.questions}
    return {q["id"]: digest(q) for q in source_case["questions"]}


def _pin_graph(case_root, row, source_path):
    graph_path = Path(row.get("graph_path") or source_path / "graph.json")
    if not graph_path.is_absolute():
        graph_path = source_path / graph_path
    if not graph_path.exists():
        if row["result"]["status"] == "execution_error":
            return row
        raise ValueError("Original answer graph is missing")
    data = graph_path.read_bytes()
    fingerprint = digest(json.loads(data))
    expected = row.get("graph_fingerprint")
    completion_path = source_path / "graph.complete.json"
    if expected is None and completion_path.exists():
        expected = json.loads(completion_path.read_text())["digest"]
    if expected != fingerprint:
        raise ValueError("Original answer graph checksum mismatch or missing binding")
    destination = case_root / "retained-graphs" / (fingerprint + ".json")
    _immutable_copy(destination, data)
    return {**row, "graph_path": str(destination), "graph_fingerprint": fingerprint}


def _register(case_root, row, question, source_path, workspace, branch):
    if digest(row["result"]) != row["digest"]:
        raise ValueError("Answer checkpoint checksum mismatch")
    answer = AnswerResult.from_dict(row["result"])
    if answer.question_id != question.id:
        raise ValueError("Answer belongs to a different question")
    version = digest(question.to_dict())
    if row.get("question_version", version) != version:
        raise ValueError("Question body/version changed")
    row = _pin_graph(case_root, row, source_path)
    original_ref = workspace.put_bytes(
        json.dumps(row, ensure_ascii=False).encode(), format="checkpoint-reference"
    )
    workspace.add_provenance(
        original_ref,
        {
            "kind": "checkpoint-registration",
            "question_version": version,
            "producer_identity": row.get("identity"),
        },
    )
    row = {**row, "question_version": version, "registered_checkpoint_ref": original_ref}
    destination = case_root / "branches" / branch / "answers" / (version + ".json")
    if destination.exists():
        existing = json.loads(destination.read_text())
        if existing["digest"] != row["digest"]:
            raise ValueError("Existing branch answer preserved; imported answer conflicts")
        return str(destination)
    atomic_json(destination, row)
    workspace.add_reference(f"answer:{branch}:{question.id}:{version}", "checkpoint", original_ref)
    return str(destination)


def register_legacy_case(
    case_root, current_case, *, source_case=None, branch="main", workspace=None
):
    """Register validated flat answers without deleting or rewriting originals.

    ``source_case`` is the actual original generation input, not a guessed current
    case. With no explicit input, ``case.json`` must exist in the legacy directory.
    Missing bindings produce a report and zero registered answers.
    """
    _branch_name(branch)
    case_root = Path(case_root)
    workspace = workspace or Workspace(case_root / "workspace")
    report = {"registered": [], "not_registered": []}
    if source_case is None:
        snapshot = case_root / "case.json"
        if not snapshot.exists():
            report["not_registered"].append({"reason": "Original question versions unavailable"})
            return report
        source_case = json.loads(snapshot.read_text())
    versions = _question_versions(source_case)
    for question in current_case.questions:
        path = case_root / "answers" / (digest(question.id) + ".json")
        if not path.exists():
            continue
        try:
            if versions.get(question.id) != digest(question.to_dict()):
                raise ValueError("Original question version differs from current question")
            original_ref = workspace.put_bytes(path.read_bytes(), format="legacy-answer-checkpoint")
            workspace.add_provenance(
                original_ref, {"kind": "legacy-pipeline", "source": "original"}
            )
            row = json.loads(path.read_text())
            report["registered"].append(
                _register(case_root, row, question, case_root, workspace, branch)
            )
        except (OSError, ValueError, KeyError) as error:
            report["not_registered"].append({"question_id": question.id, "reason": str(error)})
    return report


def fork_case_progress(case_root, source_branch, target_branch, questions, *, workspace=None):
    """Create target checkpoint references; source files and selections stay intact."""
    _branch_name(source_branch)
    _branch_name(target_branch)
    case_root = Path(case_root)
    workspace = workspace or Workspace(case_root / "workspace")
    report = {"registered": [], "not_registered": []}
    for question in questions:
        path = (
            case_root
            / "branches"
            / source_branch
            / "answers"
            / (digest(question.to_dict()) + ".json")
        )
        seen = {source_branch}
        inherited_from = source_branch
        while not path.exists():
            try:
                parent = workspace.branch(inherited_from).get("parent")
            except KeyError:
                break
            if not parent or parent in seen:
                break
            _branch_name(parent)
            seen.add(parent)
            inherited_from = parent
            path = (
                case_root / "branches" / parent / "answers" / (digest(question.to_dict()) + ".json")
            )
        if not path.exists():
            continue
        try:
            row = json.loads(path.read_text())
            if row.get("question_version") != digest(question.to_dict()):
                raise ValueError("Fork source lacks a reliable question version")
            report["registered"].append(
                _register(case_root, row, question, case_root, workspace, target_branch)
            )
        except (OSError, ValueError, KeyError) as error:
            report["not_registered"].append({"question_id": question.id, "reason": str(error)})
    return report


def reuse_preparation(source, destination, *, include_graph=False):
    """Copy known verified preparation bytes; schema changes can reuse facts alone.

    All selected artifacts are verified before any destination write. Existing
    unequal files are preserved. This function never executes missing stages.
    """
    source, destination = Path(source), Path(destination)
    names = ("memory", "graph") if include_graph else ("memory",)
    staged, missing = [], []
    for name in names:
        data_path, completion_path = source / (name + ".json"), source / (name + ".complete.json")
        if not data_path.exists() or not completion_path.exists():
            missing.append(name)
            continue
        data = data_path.read_bytes()
        completion = completion_path.read_bytes()
        if digest(json.loads(data)) != json.loads(completion)["digest"]:
            raise ValueError("Preparation checksum mismatch: " + name)
        staged.extend(((data_path.name, data), (completion_path.name, completion)))
    for name, data in staged:
        target = destination / name
        if target.exists() and target.read_bytes() != data:
            raise ValueError("Existing preparation preserved: " + name)
    for name, data in staged:
        _immutable_copy(destination / name, data)
    return {"reused": [name for name, _ in staged], "missing": missing}
