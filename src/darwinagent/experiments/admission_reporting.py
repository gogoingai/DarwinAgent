"""Ordered trial batches and admission report checkpoint aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field

from darwinagent.runtime.artifacts import atomic_json


def _row(asset, scenario, status, *, ref="", required=True, **extra):
    return {
        "asset_id": asset.id if asset else "bundle",
        "asset_fingerprint": asset.fingerprint if asset else None,
        "scenario_id": scenario,
        "input_ref": ref,
        "required": required,
        "status": status,
        **extra,
    }


@dataclass
class TrialBatch:
    """Cumulative routine results; persist marks an original report checkpoint.

    Row/count objects retain identity so later coverage classification updates
    the same records already aggregated by the entrance.
    """

    scenarios: list
    counts: dict = field(default_factory=dict)
    base_records: list = field(default_factory=list)
    composable: dict = field(default_factory=dict)
    persist: bool = True


def collect_trials(trials, report, report_path, base_records):
    """Append each ordered row once and write only requested checkpoints."""
    seen_rows = seen_base = 0
    composable = {}
    for batch in trials:
        report["scenarios"].extend(batch.scenarios[seen_rows:])
        seen_rows = len(batch.scenarios)
        base_records.extend(batch.base_records[seen_base:])
        seen_base = len(batch.base_records)
        for case_id, counts in batch.counts.items():
            if counts:
                report["counts"].setdefault(case_id, {}).update(counts)
        composable = batch.composable
        if batch.persist:
            atomic_json(report_path, report)
    return composable
