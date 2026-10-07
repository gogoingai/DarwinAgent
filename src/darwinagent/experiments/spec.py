"""Frozen experiment protocol: multi-case splits, generic metrics and selection policy.

The framework never hardcodes dataset metric names: the experiment declaration supplies
the adoption policy (metric names come from the independent Evaluator), the aggregation
rule is frozen (counts and faults sum across cases), and splits may hold any number of
complete conversations as long as the three splits stay disjoint."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from darwinagent.contracts import EvaluationResult, plain
from .policy import AdoptionPolicy


@dataclass(frozen=True)
class SelectionPolicy:
    """Selection on aggregated validation scores: strictly better than the baseline on the
    primary metric, not lower on the floor metric, no faults; rank by (primary, floor),
    exact ties to the earlier version."""

    primary: str
    floor: str

    def decide(self, baseline, candidates):
        qualified = []
        for candidate in candidates:
            scores = candidate['scores']
            if scores.total != baseline.total or scores.completed != scores.total:
                continue
            if scores.evaluation_faults or scores.generation_faults:
                continue
            if set(scores.metrics) != set(baseline.metrics):
                continue
            if (scores.metrics.get(self.primary, -1) > baseline.metrics.get(self.primary, -1)
                    and scores.metrics.get(self.floor, -1) >= baseline.metrics.get(self.floor, -1)):
                qualified.append(candidate)
        ranked = sorted(qualified, key=lambda c: (-c['scores'].metrics[self.primary],
                                                  -c['scores'].metrics[self.floor], c['order']))
        if not ranked:
            return {'selected': None, 'reason': 'no_qualified_candidate',
                    'baseline_metrics': dict(baseline.metrics)}
        return {'selected': ranked[0]['version'], 'reason': 'primary_then_floor_then_earliest',
                'ranking': [{'version': c['version'], 'order': c['order'],
                             self.primary: c['scores'].metrics[self.primary],
                             self.floor: c['scores'].metrics[self.floor]} for c in ranked]}


def aggregate_scores(results):
    """The frozen multi-case aggregation: metric counts, totals, completion and faults all
    sum; diagnostics concatenate in case order. Metric key sets must agree across cases."""
    results = list(results)
    if not results:
        raise ValueError('Nothing to aggregate')
    key_sets = {tuple(sorted(r.metrics)) for r in results}
    if len(key_sets) > 1:
        raise ValueError(f'评价器指标口径不一致，无法汇总: {[list(k) for k in key_sets]}')
    metrics = {key: sum(r.metrics.get(key, 0) for r in results) for key in results[0].metrics}
    diagnostics = ()
    for r in results:
        diagnostics += tuple(r.diagnostics)
    return EvaluationResult(metrics, sum(r.total for r in results), sum(r.completed for r in results),
                            sum(r.generation_faults for r in results),
                            sum(r.evaluation_faults for r in results), diagnostics)


@dataclass(frozen=True)
class ExperimentSpec:
    """Complete-conversation splits (any number of cases each, mutually disjoint); the same
    candidate asset bundle is evaluated on every case of a round — no per-case picking."""

    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    adoption: AdoptionPolicy
    selection: SelectionPolicy
    rounds: int | None = None              # None: unbounded training rounds, operator stop
    max_question_runs: int | None = None   # safety cap over all question runs; None: ledger only

    def __post_init__(self):
        for name in ('train', 'validation', 'test'):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value or len(set(value)) != len(value):
                raise ValueError(f'{name} must be a nonempty tuple of distinct conversations')
        seen = [c for split in (self.train, self.validation, self.test) for c in split]
        if len(seen) != len(set(seen)):
            raise ValueError('Splits must be disjoint conversations')
        if not isinstance(self.adoption, AdoptionPolicy) or not isinstance(self.selection, SelectionPolicy):
            raise ValueError('Experiment requires explicit adoption and selection policies '
                             '(metric names belong to the dataset evaluator)')
        if self.rounds is not None and (type(self.rounds) is not int or self.rounds < 0):
            raise ValueError('rounds must be a nonnegative integer or None')
        if self.max_question_runs is not None and (type(self.max_question_runs) is not int or self.max_question_runs < 1):
            raise ValueError('max_question_runs must be a positive integer or None')

    def declaration(self):
        return {'train': list(self.train), 'validation': list(self.validation), 'test': list(self.test),
                'rounds': self.rounds, 'aggregation': 'sum',
                'adoption': asdict(self.adoption),
                'selection': {'primary': self.selection.primary, 'floor': self.selection.floor},
                'max_question_runs': self.max_question_runs}


def precheck_identity(connection_config, run_config):
    """Identity a passing precheck is bound to: model routing, frozen run config and
    framework code. A record from any other identity cannot authorize a campaign."""
    from darwinagent.runtime.identity import snapshot_files, transport_identity
    from darwinagent.runtime.artifacts import digest
    from pathlib import Path
    return {'transport': transport_identity(type('Connection', (), {'cfg': connection_config})()),
            'config': digest(run_config.to_dict()),
            'framework': digest(snapshot_files([Path(__file__).resolve().parents[1]]))}
