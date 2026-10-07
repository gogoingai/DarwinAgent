"""Explicit public phases; missing prerequisites never trigger adjacent model work."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import networkx as nx

from darwinagent.contracts import GraphResult
from darwinagent.kernel import KernelBundle
from darwinagent.kernel.revision import training_id
from darwinagent.kernel.validation import capability_names
from darwinagent.kg.graph import load_graph
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.workspace import Workspace

from .admission import admit_candidate
from .proposal import ProposalGenerator
from .wiki import WikiMaintainer

CORE = frozenset({"facts", "graph", "retrieval", "answer", "check", "review", "score"})
PUBLIC = frozenset({"vector", "proposal", "candidate_check", "wiki", "report"})


def _bundle(runner, spec, selection, workspace):
    candidate = selection.candidate
    if not candidate:
        try:
            candidate = workspace.branch(selection.branch)["working"]
        except KeyError:
            pass
    if candidate:
        if not isinstance(candidate, str):
            raise ValueError("Selected candidate must be a saved version or bundle path")
        direct = Path(candidate)
        paths = [
            direct,
            runner.root / "versions" / candidate,
            runner.root / "published/versions" / candidate,
        ]
        reference = (
            workspace.root / "bundles" / (candidate + ".json") if "/" not in candidate else None
        )
        if reference is not None and reference.exists():
            paths.insert(0, Path(json.loads(reference.read_text())["path"]))
        for path in paths:
            if (path / "manifest.json").exists():
                bundle = KernelBundle(path)
                bundle.verify()
                return bundle
        raise ValueError("Selected candidate content is missing: " + candidate)
    if getattr(spec, "bundle", None) is not None:
        spec.bundle.verify()
        return spec.bundle
    raise ValueError("Registered asset bundle is required; bootstrap is not selected")


def _report(runner, selection, workspace):
    paths = sorted(runner.root.glob("R*/decision.json")) + sorted(runner.root.glob("*/stage.json"))
    source = []
    for path in [runner.root / "summary.json", *paths]:
        if path.exists():
            source.append(
                {
                    "path": str(path.relative_to(runner.root)),
                    "content": json.loads(path.read_text()),
                }
            )
    report = {
        "selection": selection.to_dict(),
        "records": source,
        "events": workspace.events(),
        "pending_wiki_tasks": [
            str(p.relative_to(runner.root))
            for p in runner.root.glob("R*/wiki-outbox/*.json")
            if json.loads(p.read_text()).get("state") == "pending"
        ],
    }
    path = runner.root / "reports" / (digest(selection.to_dict()) + ".json")
    atomic_json(path, report)
    ref = workspace.put_json(report)
    workspace.add_provenance(
        ref, {"kind": "report-rebuild", "generation_calls": 0, "scoring_calls": 0}
    )
    return {
        "status": "complete",
        "path": str(path),
        "content_ref": ref,
        "coverage": "existing durable records only",
    }


def _vector(runner, cases, workspace):
    records, gaps = [], []
    for case in cases:
        source = None if runner.snapshot_root is None else runner.snapshot_root / case.id
        if (
            source is None
            or not (source / "vector/index.jsonl").exists()
            or not (source / "manifest.json").exists()
        ):
            gaps.append(
                {"case_id": case.id, "requires": "existing frozen vector index and manifest"}
            )
            continue
        manifest = json.loads((source / "manifest.json").read_text())
        raw = (source / "vector/index.jsonl").read_bytes()
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        if digest(rows) != manifest.get("vector_digest"):
            raise ValueError("Frozen vector checksum mismatch: " + case.id)
        if len(rows) != manifest.get("n_vector_records", len(rows)):
            raise ValueError("Frozen vector row count mismatch: " + case.id)
        ref = workspace.put_bytes(raw, format="frozen-vector-jsonl")
        metadata = source / "vector/meta.json"
        space = json.loads(metadata.read_text()) if metadata.exists() else {"status": "unknown"}
        workspace.add_provenance(
            ref,
            {
                "kind": "frozen-vector-registration",
                "case_id": case.id,
                "embedding_space": space,
                "source_manifest": manifest,
            },
        )
        records.append(
            {"case_id": case.id, "content_ref": ref, "rows": len(rows), "embedding_space": space}
        )
    return {
        "status": "missing_inputs" if gaps else "complete",
        "records": records,
        "missing": gaps,
        "embedding_calls": 0,
    }


def _graphs(runner, cases, branch):
    graphs, gaps = {}, []
    for case in cases:
        answer_root = runner.root / "B0/generation" / case.id / "branches" / branch / "answers"
        bound_graphs = set()
        for question in case.questions:
            answer_path = answer_root / (digest(question.to_dict()) + ".json")
            if answer_path.exists():
                row = json.loads(answer_path.read_text())
                if row.get("case_path"):
                    original_case = json.loads(Path(row["case_path"]).read_text())
                    if original_case.get("corpus") != [block.to_dict() for block in case.corpus]:
                        gaps.append(
                            {"case_id": case.id, "requires": "graph for changed training corpus"}
                        )
                        continue
                if row.get("graph_path"):
                    graph_path = Path(row["graph_path"])
                    if digest(json.loads(graph_path.read_text())) != row["graph_fingerprint"]:
                        raise ValueError("Selected answer graph checksum mismatch")
                    bound_graphs.add(graph_path)
        if any(gap.get("case_id") == case.id for gap in gaps):
            continue
        if len(bound_graphs) > 1:
            gaps.append(
                {
                    "case_id": case.id,
                    "requires": "explicit graph choice; selected answers use different graphs",
                }
            )
            continue
        if bound_graphs:
            graph_path = bound_graphs.pop()
            graphs[case.id] = GraphResult(
                nx.freeze(load_graph(graph_path)),
                MappingProxyType({b.source.id: b for b in case.corpus}),
                (),
                (),
            )
            continue
        completion = list(
            (runner.root / "B0/generation" / case.id).glob("preparations/*/graph.complete.json")
        )
        flat = runner.root / "B0/generation" / case.id / "graph.complete.json"
        if flat.exists():
            completion.append(flat)
        if len(completion) > 1:
            gaps.append(
                {
                    "case_id": case.id,
                    "requires": "explicit graph choice; several preparation graphs exist",
                }
            )
            continue
        if not completion:
            if runner.bootstrap_trial_graph is not None:
                graphs[case.id] = runner.bootstrap_trial_graph
                continue
            gaps.append(
                {
                    "case_id": case.id,
                    "requires": "saved training graph; graph stage is not selected",
                }
            )
            continue
        complete = completion[-1]
        path = complete.with_name("graph.json")
        if digest(json.loads(path.read_text())) != json.loads(complete.read_text())["digest"]:
            raise ValueError("Saved training graph checksum mismatch")
        graphs[case.id] = GraphResult(
            nx.freeze(load_graph(path)),
            MappingProxyType({b.source.id: b for b in case.corpus}),
            (),
            (),
        )
    return graphs, gaps


async def run_selected_operations(runner, cases, spec, selection, scope=()):
    """Run only requested operations, retaining partial drafts and explicit gaps."""
    scoped, missing_scope = [], []
    for case in cases:
        if not selection.includes(case.id):
            continue
        questions = tuple(q for q in case.questions if selection.includes(case.id, q.id))
        if not questions:
            missing_scope.append(
                {
                    "case_id": case.id,
                    "question_ids": list(selection.question_ids),
                    "requires": "matching training questions",
                }
            )
            continue
        scoped.append(replace(case, questions=questions))
    cases = tuple(scoped)
    selected = set(selection.stages)
    if selected <= CORE:
        if not cases:
            return {
                "status": "missing_inputs",
                "missing_scope": missing_scope,
                "rounds": [],
                "stage_results": [],
                "scores": None,
            }
        workspace = Workspace(runner.root / "workspace")
        try:
            selected_bundle = _bundle(runner, spec, selection, workspace)
        except (ValueError, OSError) as exc:
            return {
                "status": "missing_inputs",
                "missing": [{"requires": str(exc)}],
                "rounds": [],
                "stage_results": [],
                "scores": None,
            }
        results, scores = await runner._stage("B0", cases, spec.with_bundle(selected_bundle))
        unhealthy = any(a.status == "execution_error" for r in results for a in r.answers)
        unhealthy = unhealthy or (
            scores is not None and (scores.generation_faults or scores.evaluation_faults)
        )
        return {
            "status": "failed" if unhealthy else "complete",
            "rounds": [],
            "stage_results": [r.to_dict() for r in results],
            "scores": None if scores is None else scores.to_dict(),
            "selection": selection.to_dict(),
        }
    if selected - CORE - PUBLIC:
        raise ValueError("Unsupported selected stages: " + str(sorted(selected - CORE - PUBLIC)))
    workspace = Workspace(runner.root / "workspace")
    output = {
        "selection": selection.to_dict(),
        "operations": {},
        "status": "complete",
        "rounds": [],
        "stage_results": [],
        "scores": None,
    }
    output["missing_scope"] = missing_scope
    if missing_scope:
        output["status"] = "partial"
    target = runner.root / "selected-operations" / digest(selection.to_dict())
    if selection.mode in ("rerun", "rerun_all") and "proposal" in selected:
        from uuid import uuid4

        target = target / "attempts" / uuid4().hex
    bundle, patches, wiki = None, None, None
    if selected & CORE:
        core_selection = replace(selection, stages=tuple(s for s in selection.stages if s in CORE))
        from .stages import selected_stage

        selected_bundle = _bundle(runner, spec, selection, workspace)
        results, scores = await selected_stage(
            "B0",
            cases,
            spec.with_bundle(selected_bundle),
            root=runner.root,
            client_factory=runner._client,
            evaluator_factory=runner.evaluator_factory,
            config=runner.config,
            execution=core_selection,
            snapshot_root=runner.snapshot_root,
            graph_builder=runner.graph_builder,
        )
        output["stage_results"] = [r.to_dict() for r in results]
        output["scores"] = None if scores is None else scores.to_dict()
        output["operations"]["pipeline"] = {
            "results": [r.to_dict() for r in results],
            "scores": None if scores is None else scores.to_dict(),
        }
    for phase in ("vector", "proposal", "candidate_check", "wiki", "report"):
        if phase not in selected:
            continue
        try:
            if not cases and phase in ("proposal", "candidate_check", "vector"):
                output["operations"][phase] = {
                    "status": "missing_inputs",
                    "missing": missing_scope or [{"requires": "selected training cases"}],
                }
                output["status"] = "partial"
                continue
            if phase == "vector":
                value = _vector(runner, cases, workspace)
            elif phase == "report":
                value = _report(runner, selection, workspace)
            elif phase == "proposal":
                if not any(case.questions for case in cases):
                    raise ValueError("Selected training questions are required for proposal")
                bundle = _bundle(runner, spec, selection, workspace)
                wiki = WikiMaintainer(
                    runner.root,
                    "selected:" + bundle.version,
                    runner._client,
                    runner.config,
                    runner.wiki_call_limit,
                )
                for case in cases:
                    wiki.service.register(
                        {
                            "corpus": [b.to_dict() for b in case.corpus],
                            "questions": [q.to_dict() for q in case.questions],
                        },
                        scope={
                            "case_ids": [case.id],
                            "question_ids": [q.id for q in case.questions],
                        },
                    )
                from .optimization import _human_objective

                context = wiki.context()
                manual_goal = _human_objective(workspace, selection.branch)
                if manual_goal is not None:
                    context["objective"] = {"direction": manual_goal, "source": "human"}
                transport = runner._client("selected-proposal")
                try:
                    patches = await ProposalGenerator().propose(
                        bundle,
                        cases,
                        None,
                        transport,
                        runner.config,
                        target / "proposal-call.json",
                        allowed_kinds=scope,
                        wiki_context=context,
                        wiki_service=wiki.service,
                        call_limit=getattr(runner.config, "proposal_call_limit", None),
                    )
                finally:
                    await transport.aclose()
                atomic_json(target / "patches.json", {"patches": [p.to_dict() for p in patches]})
                value = {
                    "status": "complete",
                    "path": str(target / "proposal-call.json"),
                    "patch_count": len(patches),
                    "base_version": bundle.version,
                }
            elif phase == "candidate_check":
                bundle = bundle or _bundle(runner, spec, selection, workspace)
                if patches:
                    candidate_path = target / "draft-candidate"
                    if not (candidate_path / "bundle/manifest.json").exists():
                        bundle = runner.revisions.propose(
                            bundle,
                            patches,
                            candidate_path,
                            [training_id(c.id, q.id) for c in cases for q in c.questions],
                            [q.text for c in cases for q in c.questions],
                            allowed_kinds=scope,
                            required_capabilities=capability_names(spec.retrieval_floor or {}),
                        )
                    else:
                        bundle = KernelBundle(candidate_path / "bundle")
                graphs, gaps = _graphs(runner, cases, selection.branch)
                required = capability_names(spec.retrieval_floor or {})
                if "semantic_search" in required:
                    gaps.append(
                        {
                            "requires": "explicitly prepared vector capability; no embedding connection is inferred"
                        }
                    )
                if gaps:
                    value = {
                        "status": "missing_inputs",
                        "missing": gaps,
                        "candidate_version": bundle.version,
                    }
                else:
                    report = admit_candidate(
                        bundle,
                        cases,
                        graphs,
                        runner.config,
                        required,
                        target / "admission.json",
                        answer_contract=spec.answer_contract,
                    )
                    value = {
                        "status": "complete",
                        "report": report,
                        "path": str(target / "admission.json"),
                    }
            else:
                if not (runner.root / "optimization/wiki.json").exists():
                    value = {
                        "status": "missing_inputs",
                        "missing": [{"requires": "existing Wiki records"}],
                    }
                else:
                    existing = json.loads((runner.root / "optimization/wiki.json").read_text())
                    wiki = wiki or WikiMaintainer(
                        runner.root,
                        existing["identity"],
                        runner._client,
                        runner.config,
                        runner.wiki_call_limit,
                    )
                    wiki.reconcile()
                    from .rounds import _wiki_record_outbox

                    replayed = []
                    allowed = {training_id(c.id, q.id) for c in cases for q in c.questions}
                    for path in sorted(runner.root.glob("R*/wiki-outbox/*.json")):
                        task = json.loads(path.read_text())
                        tids = set(task.get("kwargs", {}).get("training_ids", []))
                        if (
                            task.get("state") == "pending"
                            and "args" in task
                            and (not tids or tids <= allowed)
                        ):
                            await _wiki_record_outbox(
                                wiki, path.parent.parent, *task["args"], **task["kwargs"]
                            )
                            replayed.append(str(path))
                    pending_attribution = [
                        entry["id"]
                        for entry in wiki._wiki()["entries"]
                        if entry.get("pending_attribution")
                    ]
                    pending_outbox = [
                        str(path)
                        for path in runner.root.glob("R*/wiki-outbox/*.json")
                        if json.loads(path.read_text()).get("state") == "pending"
                    ]
                    value = {
                        "status": "partial"
                        if pending_attribution or pending_outbox
                        else "complete",
                        "replayed": replayed,
                        "pending_attribution": pending_attribution,
                        "pending_outbox": pending_outbox,
                        "wiki_version": wiki.context()["version"],
                    }
            output["operations"][phase] = value
            if value["status"] != "complete":
                output["status"] = "partial"
        except (ValueError, OSError, KeyError) as exc:
            output["operations"][phase] = {
                "status": "missing_inputs"
                if "missing" in str(exc).lower() or "required" in str(exc).lower()
                else "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            output["status"] = "partial"
    atomic_json(target / "result.json", output)
    return output
