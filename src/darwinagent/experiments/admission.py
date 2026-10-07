"""Fail-closed, per-candidate actual-graph admission diagnostics."""

from __future__ import annotations

import time
from pathlib import Path

from darwinagent.kernel.checks import CheckRegistry
from darwinagent.kernel.functions import FunctionRegistry
from darwinagent.kernel.validation import trial_capability_floor_errors, validate_bundle
from darwinagent.operators.sandbox import Limits
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import snapshot_files

from .admission_checks import check_replay_trials, check_trials
from .admission_composition import composition_trials
from .admission_functions import function_replay_trials, function_trials
from .admission_reporting import _row, collect_trials
from .admission_samples import _graph_identity


class AdmissionError(ValueError):
    def __init__(self, report_path, report):
        failures = [
            f"{x['asset_id']}:{x['scenario_id']}:{x.get('error', x['status'])}"
            for x in report["scenarios"]
            if x["required"] and x["status"] != "passed"
        ]
        prefix = (
            "候选能力试跑不合格: "
            if any(
                x["scenario_id"] == "capability_floor" and x["status"] == "failed"
                for x in report["scenarios"]
            )
            else "候选准入失败: "
        )
        super().__init__(prefix + f"({report_path}): " + "; ".join(failures[:12]))
        self.report_path, self.report = report_path, report


def admit_candidate(
    bundle,
    training_cases,
    graph_refs,
    config,
    required_caps,
    report_path,
    replay_inputs=(),
    remote_vector_error=None,
    answer_contract=None,
    replay_checks=(),
    answer_counterexamples=(),
    answer_examples=(),
):
    """Run independent required checks, persist the complete report, then reject on gaps."""
    report_path = Path(report_path)
    limits = Limits(config.function_steps, config.function_timeout_s, config.result_bytes)
    assets = tuple(bundle.assets.assets)
    framework = snapshot_files([Path(__file__).resolve().parents[1]])
    report = {
        "schema_version": 1,
        "candidate_version": bundle.version,
        "asset_fingerprints": {a.id: a.fingerprint for a in assets},
        "config_digest": digest(config.to_dict()),
        "framework_digest": digest(framework),
        "sandbox_digest": framework[
            str(Path(__file__).resolve().parents[1] / "operators" / "sandbox.py")
        ],
        "graph_digests": {},
        "snapshot_digests": {},
        "scenarios": [],
        "counts": {},
        "verdict": "incomplete",
    }
    scenarios = report["scenarios"]
    base_records = []
    started = time.monotonic()
    try:
        validate_bundle(bundle)
    except Exception as exc:
        scenarios.append(
            _row(None, "static", "failed", error_type=type(exc).__name__, error=str(exc))
        )
        atomic_json(report_path, report)
        raise AdmissionError(report_path, report) from exc
    scenarios.append(_row(None, "static", "passed"))
    if remote_vector_error:
        scenarios.append(_row(None, "remote_vector", "incomplete", error=remote_vector_error))
    registry = FunctionRegistry(bundle, limits)
    checks = CheckRegistry(bundle, limits)
    cases = list(training_cases)
    if not cases or not graph_refs:
        scenarios.append(_row(None, "training_graph", "incomplete", error="No training graph"))
    for case in cases:
        graph = graph_refs.get(case.id)
        if graph is None:
            scenarios.append(
                _row(
                    None,
                    "training_graph",
                    "incomplete",
                    ref=case.id,
                    error="Missing training graph",
                )
            )
            continue
        report["graph_digests"][case.id] = _graph_identity(graph)
        manifest = next(
            (d for d in graph.diagnostics if isinstance(d, dict) and d.get("snapshot_digest")), None
        )
        if manifest:
            report["snapshot_digests"][case.id] = manifest["snapshot_digest"]
        collect_trials(
            check_trials(
                case, graph, checks, answer_contract, answer_examples, answer_counterexamples
            ),
            report,
            report_path,
            base_records,
        )
        collect_trials(
            function_replay_trials(case, graph, registry, replay_inputs, remote_vector_error),
            report,
            report_path,
            base_records,
        )
        collect_trials(
            check_replay_trials(case, checks, replay_checks),
            report,
            report_path,
            base_records,
        )
        composable = collect_trials(
            function_trials(
                case,
                graph,
                registry,
                remote_vector_error,
                tuple(scenarios),
                report["counts"].get(case.id),
            ),
            report,
            report_path,
            base_records,
        )
        collect_trials(
            composition_trials(case, graph, registry, checks, composable, answer_contract),
            report,
            report_path,
            base_records,
        )
    if required_caps:
        problems = trial_capability_floor_errors(base_records, required_caps)
        scenarios.append(
            _row(
                None,
                "capability_floor",
                "failed" if problems else "passed",
                error=str(problems) if problems else None,
            )
        )
    report["elapsed_s"] = round(time.monotonic() - started, 3)
    report["verdict"] = (
        "passed"
        if scenarios and all(x["status"] == "passed" or not x["required"] for x in scenarios)
        else "failed"
    )
    atomic_json(report_path, report)
    if report["verdict"] != "passed":
        raise AdmissionError(report_path, report)
    return report
