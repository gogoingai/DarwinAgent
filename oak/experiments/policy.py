"""Metric names are supplied by the independent evaluator; policy is frozen engineering code."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AdoptionPolicy:
    primary: str
    non_decreasing: tuple[str, ...]

    def decide(self,baseline,candidate):
        failures=[]
        if candidate.total!=baseline.total or candidate.completed!=candidate.total:
            failures.append('incomplete_evaluation')
        if candidate.evaluation_faults: failures.append('evaluation_fault')
        if candidate.generation_faults>baseline.generation_faults: failures.append('more_generation_faults')
        if set(candidate.metrics)!=set(baseline.metrics): failures.append('metric_contract_changed')
        if candidate.metrics.get(self.primary,-1)<=baseline.metrics.get(self.primary,-1): failures.append('primary_not_strictly_improved')
        for key in self.non_decreasing:
            if candidate.metrics.get(key,-1)<baseline.metrics.get(key,-1): failures.append('metric_decreased:'+key)
        return {'accepted':not failures,'reasons':failures or ['primary_strictly_improved'],
                'baseline':baseline.to_dict(),'candidate':candidate.to_dict()}
