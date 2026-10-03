"""Frozen three-set experiment protocol: fixed splits, open-ended rounds and selection policy."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .policy import AdoptionPolicy


@dataclass(frozen=True)
class SelectionPolicy:
    """Validation selection: strictly better than B0 on the primary original-gold metric,
    not lower on the floor metric, no faults; rank by (primary, floor), exact ties to the earlier version."""

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


@dataclass(frozen=True)
class ExperimentSpec:
    """Complete-session splits; validation and test references never feed proposals."""

    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    rounds: int | None = None              # None: unbounded training rounds, operator stop
    adoption: AdoptionPolicy = field(default_factory=lambda: AdoptionPolicy(
        'repaired_precise', ('original_lenient', 'original_precise', 'repaired_lenient')))
    selection: SelectionPolicy = field(default_factory=lambda: SelectionPolicy(
        'original_precise', 'original_lenient'))
    max_question_runs: int | None = None   # safety cap over all question runs; None: ledger only

    def __post_init__(self):
        for name in ('train', 'validation', 'test'):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value or len(value) != 1:
                raise ValueError(f'{name} must name exactly one complete conversation in this protocol')
        if len({self.train[0], self.validation[0], self.test[0]}) != 3:
            raise ValueError('Splits must be disjoint conversations')
        if self.rounds is not None and (type(self.rounds) is not int or self.rounds < 0):
            raise ValueError('rounds must be a nonnegative integer or None')
        if self.max_question_runs is not None and (type(self.max_question_runs) is not int or self.max_question_runs < 1):
            raise ValueError('max_question_runs must be a positive integer or None')

    def declaration(self):
        return {'train': list(self.train), 'validation': list(self.validation), 'test': list(self.test),
                'rounds': self.rounds, 'adoption': asdict(self.adoption),
                'selection': {'primary': self.selection.primary, 'floor': self.selection.floor},
                'max_question_runs': self.max_question_runs}
