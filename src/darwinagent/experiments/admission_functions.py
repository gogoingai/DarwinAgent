"""Independent function replay and actual-graph capability trials."""

from __future__ import annotations

from collections.abc import Mapping

from darwinagent.contracts import plain
from darwinagent.kernel.spec import validate_value
from darwinagent.runtime.artifacts import digest

from .admission_reporting import TrialBatch, _row
from .admission_rules import loop_carried_capability_errors
from .admission_samples import (
    _graph_identity,
    _pressure_graph,
    _samples,
    _traversal_directions,
    _traversal_shape,
)


def _returned_rows(data):
    rows = data.get("rows") if isinstance(data, Mapping) else data
    if isinstance(rows, (list, tuple)) and rows and all(isinstance(x, Mapping) for x in rows):
        return plain(rows)
    return ()


def function_replay_trials(case, graph, registry, replay_inputs, remote_vector_error):
    scenarios = []
    for entry in replay_inputs:
        if len(entry) == 3:
            replay_case, aid, params = entry
            if replay_case != case.id:
                continue
        else:
            aid, params = entry
        asset = registry.functions.get(aid, (None, None))[0]
        if asset is None:
            scenarios.append(
                _row(
                    None,
                    "replay",
                    "incomplete",
                    ref=f"{case.id}:{aid}",
                    error="Historical asset not registered",
                )
            )
            continue
        ref = f"{case.id}:replay:{digest(plain(params))}"
        if remote_vector_error and "semantic_search" in asset.content:
            scenarios.append(
                _row(
                    asset,
                    "replay",
                    "incomplete",
                    ref=ref,
                    error="Remote vector dependency unavailable: " + remote_vector_error,
                )
            )
            continue
        try:
            validate_value(params, asset.input_contract, "tool.params")
        except ValueError as exc:
            scenarios.append(
                _row(
                    asset,
                    "replay",
                    "incomplete",
                    ref=ref,
                    error="Historical parameters need a validated contract migration: " + str(exc),
                )
            )
            continue
        observation = {}
        try:
            outcome = registry.call(aid, params, graph, _observation=observation)
            scenarios.append(
                _row(
                    asset,
                    "replay",
                    "passed",
                    ref=ref,
                    data_digest=digest(plain(outcome["data"])),
                    node_ids=outcome["node_ids"],
                    read_node_ids=outcome["read_node_ids"],
                    source_ids=outcome["source_ids"],
                    **observation,
                )
            )
        except Exception as exc:
            scenarios.append(
                _row(
                    asset,
                    "replay",
                    "failed",
                    ref=ref,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    **observation,
                )
            )
        yield TrialBatch(scenarios)
    yield TrialBatch(scenarios, persist=False)


def function_trials(
    case, graph, registry, remote_vector_error, prior_scenarios=(), prior_counts=None
):
    scenarios = []
    counts_by_case = {case.id: dict(prior_counts or {})}
    base_records = []
    composable = {}
    for aid, (asset, _) in sorted(registry.functions.items()):
        if remote_vector_error and "semantic_search" in asset.content:
            counts_by_case.setdefault(case.id, {}).setdefault(
                aid,
                {
                    "base": len(asset.trial_inputs),
                    "stress_generated": 0,
                    "stress_legal": 0,
                    "executed": 0,
                    "by_scenario": {},
                },
            )
            scenarios.append(
                _row(
                    asset,
                    "remote_vector",
                    "incomplete",
                    ref=case.id,
                    error="Required semantic search trials not executed: " + remote_vector_error,
                )
            )
            continue
        try:
            registry.call(aid, None, graph)
        except ValueError as exc:
            scenarios.append(
                _row(
                    asset,
                    "invalid_params_rejected",
                    "passed" if str(exc).startswith("tool.params:") else "failed",
                    ref=f"{case.id}:invalid:null",
                    error=None if str(exc).startswith("tool.params:") else str(exc),
                )
            )
        except Exception as exc:
            scenarios.append(
                _row(
                    asset,
                    "invalid_params_rejected",
                    "failed",
                    ref=f"{case.id}:invalid:null",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            )
        else:
            scenarios.append(
                _row(
                    asset,
                    "invalid_params_rejected",
                    "failed",
                    ref=f"{case.id}:invalid:null",
                    error="Non-object parameters were accepted",
                )
            )
        counts = counts_by_case.setdefault(case.id, {}).setdefault(
            aid,
            {
                "base": 0,
                "stress_generated": 0,
                "stress_legal": 0,
                "executed": 0,
                "by_scenario": {},
            },
        )
        static_errors = loop_carried_capability_errors(asset.content)
        if static_errors:
            scenarios.append(
                _row(asset, "loop_carried", "failed", ref=case.id, error="; ".join(static_errors))
            )
        else:
            # 通过也落行：场景行集完整，verified_fix 的「最初失败场景复现通过」
            # 绑定才能机器判定（2026-10-05 审查 P2）。
            scenarios.append(_row(asset, "loop_carried", "passed", ref=case.id))
        required = [("base", plain(v)) for v in asset.trial_inputs]
        generated = list(_samples(asset, graph))
        counts["base"] = len(required)
        counts["stress_generated"] = len(generated)
        if not generated:
            scenarios.append(
                _row(
                    asset,
                    "stress_coverage",
                    "incomplete",
                    ref=case.id,
                    error="No contract-valid stress sample",
                )
            )
        # Reserve a representative of each shape before optional variations.
        representatives = {}
        for tag, params in generated:
            representatives.setdefault(tag, params)
        selected_stress = list(representatives.items())
        if len(selected_stress) < 32:
            selected_stress.extend((t, p) for t, p in generated if (t, p) not in selected_stress)
        selected_stress = selected_stress[:32]
        selected_tags = {tag for tag, _ in selected_stress}
        expected_tags = set()
        if "relative_date" in asset.content:
            expected_tags.add("relative_date_object")
        if "traverse" in asset.content:
            _, direction_keys, fixed_directions, _ = _traversal_shape(asset)
            enabled = _traversal_directions(asset)
            for direction in ("in", "out"):
                tag = f"high_degree_{direction}"
                if direction not in enabled:
                    scenarios.append(
                        _row(
                            asset,
                            tag,
                            "skipped",
                            required=False,
                            ref=case.id,
                            reason="Traversal code does not expose this direction",
                        )
                    )
                    continue
                degree = graph.graph.in_degree if direction == "in" else graph.graph.out_degree
                if any(degree(node) > 0 for node in graph.graph.nodes):
                    expected_tags.add(tag)
                else:
                    scenarios.append(
                        _row(
                            asset,
                            tag,
                            "skipped",
                            required=False,
                            ref=case.id,
                            reason=f"Training graph has no {direction} edges",
                        )
                    )
        for tag in sorted(expected_tags - selected_tags):
            scenarios.append(
                _row(
                    asset,
                    tag,
                    "incomplete",
                    ref=case.id,
                    error="No contract-valid input triggered the required capability path",
                )
            )
        counts["stress_legal"] = len(selected_stress)
        selected = required + selected_stress
        for index, (tag, params) in enumerate(selected):
            ref = f"{case.id}:{tag}:{index}:{digest(params)}"
            observation = {}
            try:
                trial_graph = _pressure_graph(asset, params, graph, tag)
                if trial_graph is not graph:
                    observation["pressure_kind"] = "graph_row_order"
                    observation["pressure_graph_digest"] = _graph_identity(trial_graph)
                outcome = registry.call(aid, params, trial_graph, _observation=observation)
                status, error_type, error = "passed", None, None
            except Exception as exc:
                outcome = None
                status, error_type, error = "failed", type(exc).__name__, str(exc)
            expected_capability = (
                "relative_date"
                if tag == "relative_date_object"
                else "traverse"
                if tag.startswith("high_degree_")
                else None
            )
            if (
                outcome is not None
                and expected_capability
                and not outcome["capability_calls"].get(expected_capability)
            ):
                status, error_type, error = (
                    "incomplete",
                    "CoverageGap",
                    (f"{tag} did not invoke {expected_capability}"),
                )
            if (
                outcome is not None
                and tag.startswith("high_degree_")
                and tag.removeprefix("high_degree_") not in outcome["traverse_directions"]
            ):
                status, error_type, error = (
                    "incomplete",
                    "CoverageGap",
                    (f"{tag} did not traverse in the requested direction"),
                )
            if (
                outcome is not None
                and tag.startswith("high_degree_")
                and not any(
                    call["direction"] == tag.removeprefix("high_degree_")
                    and call["matched_edges"] > 0
                    for call in observation.get("traverse_observations", ())
                )
            ):
                status, error_type, error = (
                    "incomplete",
                    "CoverageGap",
                    (f"{tag} did not traverse any matching relation edge"),
                )
            counts["executed"] += 1
            covered = counts["by_scenario"].setdefault(tag, {"executed": 0, "passed": 0})
            covered["executed"] += 1
            covered["passed"] += status == "passed"
            scenarios.append(
                _row(
                    asset,
                    tag,
                    status,
                    ref=ref,
                    error_type=error_type,
                    error=error,
                    parameters=plain(params),
                    **observation,
                )
            )
            if outcome is not None:
                scenarios[-1]["capability_calls"] = outcome["capability_calls"]
                scenarios[-1]["read_node_ids"] = outcome["read_node_ids"]
                scenarios[-1]["node_ids"] = outcome["node_ids"]
                scenarios[-1]["source_ids"] = outcome["source_ids"]
                if tag == "base":
                    base_records.append(outcome)
                if status == "passed" and trial_graph is graph and _returned_rows(outcome["data"]):
                    composable.setdefault(aid, (outcome, _returned_rows(outcome["data"])))
        # Some legal selectors return no relation hits. They are discovery trials,
        # not execution failures. Require an actually covered pressure path per
        # direction across the legal selectors; never waive runtime failures.
        for tag in sorted(expected_tags):
            attempts = [
                r
                for r in (*prior_scenarios, *scenarios)
                if r.get("asset_id") == aid
                and r.get("scenario_id") == tag
                and str(r.get("input_ref", "")).startswith(case.id + ":")
            ]
            gaps = [r for r in attempts if r.get("error_type") == "CoverageGap"]
            if any(r["status"] == "passed" for r in attempts):
                for row in gaps:
                    row["required"] = False
                    row["reason"] = "Other legal selector actually exercised this pressure path"
        if counts["executed"] < counts["base"] + counts["stress_legal"]:
            scenarios.append(
                _row(
                    asset,
                    "stress_coverage",
                    "incomplete",
                    ref=case.id,
                    error="Not all generated legal samples executed",
                )
            )
        yield TrialBatch(scenarios, counts_by_case, base_records, composable)
    yield TrialBatch(scenarios, counts_by_case, base_records, composable, persist=False)
