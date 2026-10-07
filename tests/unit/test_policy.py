import unittest
from darwinagent.contracts import EvaluationResult
from darwinagent.experiments import AdoptionPolicy


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.policy=AdoptionPolicy('repaired_precise',('original_lenient','original_precise','repaired_lenient'))
        self.base=EvaluationResult({'repaired_precise':10,'original_lenient':12,'original_precise':11,'repaired_lenient':13},20,20,0,0)

    def score(self,**kw):
        return EvaluationResult(kw.pop('metrics',dict(self.base.metrics,repaired_precise=11)),**{'total':20,'completed':20,'generation_faults':0,'evaluation_faults':0,**kw})

    def test_strict_increase_only(self):
        self.assertTrue(self.policy.decide(self.base,self.score())['accepted'])
        self.assertFalse(self.policy.decide(self.base,self.base)['accepted'])

    def test_any_other_metric_decrease_rejected(self):
        for name in self.policy.non_decreasing:
            self.assertFalse(self.policy.decide(self.base,self.score(metrics=dict(self.base.metrics,repaired_precise=15,**{name:0})))['accepted'])

    def test_fault_or_incomplete_rejected(self):
        for kw in [{'generation_faults':1},{'evaluation_faults':1},{'completed':19},{'total':19}]:
            self.assertFalse(self.policy.decide(self.base,self.score(**kw))['accepted'])
