import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from oak.config import Config,RunConfig
from oak.contracts import EvaluationResult
from oak.experiments import AdoptionPolicy,ExperimentRunner
from oak.kernel import KernelBundle,TaskSpec
from oak.kernel.registration import load_assets
from oak.llm.recorded import RecordedClient
from tests.fixtures import TASK,case,client


class LedgerRecordedClient(RecordedClient):
    async def aclose(self): pass
    def ledger_summary(self): return {'total_calls':len(self.calls)}


class FixtureEvaluator:
    def __init__(self,stage):
        stage=Path(stage)
        name=stage.parent.name
        self.stage=name if name=='B0' or name.startswith('R') else stage.parent.parent.name
    async def evaluate(self,result):
        assert all(a.status=='answered' for a in result.answers)
        return EvaluationResult({'precise':0 if self.stage=='B0' else 1},1,1,0,0)


class RecordedExperiment(ExperimentRunner):
    """Transport fixture only; the actual controller, agents and asset runtime execute."""
    def __init__(self,root,stale=False):
        self.case=case();self.created=[];self.stage_clients=Counter();self.stale=stale
        super().__init__(type('Adapter',(),{'generation_input':lambda _,ident:self.case})(),
            lambda transport,path:FixtureEvaluator(path),Config(),
            RunConfig(protocol_attempts=1),AdoptionPolicy('precise',()),root)

    def _client(self,stage):
        self.stage_clients[stage]+=1
        if self.stage_clients[stage]==1:
            if stage=='B0':
                replies={'bootstrap':[{'assets':[a.to_dict() for a in load_assets(TASK).assets]}]}
            else:
                pointer=json.loads((self.root/'published/current.json').read_text())
                base=KernelBundle(self.root/'published'/pointer['path'])
                asset=next(a for a in base.assets.assets if a.role=='answer')
                updated=asset.to_dict();updated['content']+='\nUse the current source records carefully. '+stage
                replies={'proposal':[{'patches':[{'asset':updated,
                    'base_fingerprint':'f'*64 if self.stale and stage=='R2' else asset.fingerprint,
                    'reason':'General task instruction refined from this training run',
                    'training_evidence':[f'{self.case.id}::{self.case.questions[0].id}']}]}]}
        else:
            replies={role:list(values) for role,values in client(self.case).replies.items()}
        transport=LedgerRecordedClient(replies);self.created.append(transport)
        return transport


class FullExperimentControl(unittest.TestCase):
    def execute(self,stale=False):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup);root=Path(td.name)
        runner=RecordedExperiment(root,stale)
        spec=TaskSpec.load(TASK/'task.yaml')
        with contextlib.redirect_stdout(io.StringIO()):
            summary=asyncio.run(runner.run(runner.case.id,spec))
        return root,runner,summary

    def test_full_bootstrap_proposal_score_adopt_then_reject_tie(self):
        root,runner,summary=self.execute()
        self.assertEqual(summary['status'],'complete')
        self.assertEqual([d['accepted'] for d in summary['rounds']],[True,False])
        self.assertIn('primary_not_strictly_improved',summary['rounds'][1]['reasons'])
        published=json.loads((root/'published/current.json').read_text())
        self.assertEqual(published['version'],summary['rounds'][0]['candidate_version'])
        for stage in ['B0','R1','R2']:
            result=json.loads((root/stage/'generation'/runner.case.id/'result.json').read_text())
            self.assertEqual(result['answers'][0]['status'],'answered')
            self.assertTrue((root/stage/'evaluation'/f'{runner.case.id}.json').exists())
        second=json.loads((root/'R2/proposal-call.json').read_text())
        self.assertEqual(second['input']['base_version'],published['version'])
        self.assertEqual(second['input']['task_training_feedback']['scores']['metrics']['precise'],1)

    def test_stale_candidate_rejected_without_generation_or_publication(self):
        root,runner,summary=self.execute(stale=True)
        self.assertEqual(summary['status'],'failed')
        rejected=summary['rounds'][1]
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['status'],'validation_failed')
        self.assertFalse((root/'R2/generation').exists())
        published=json.loads((root/'published/current.json').read_text())
        self.assertEqual(published['version'],summary['rounds'][0]['candidate_version'])
