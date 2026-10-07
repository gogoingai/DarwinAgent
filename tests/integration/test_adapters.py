import ast
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from darwinagent.contracts import DatasetAdapter, Evaluator, QuestionInput
from darwinagent.config import RunConfig
from darwinagent.engine import Pipeline
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.recorded import RecordedClient
from darwinagent.runtime.artifacts import verify_files
from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.evaluator import LocomoEvaluator, LOCK_PATH
from datasets.travelplanner.adapter import TravelPlannerAdapter
from datasets.travelplanner.evaluator import TravelPlannerEvaluator
from tests.fixtures import ROOT,case,client,spec


class DatasetBoundary(unittest.TestCase):
    def test_both_implement_contract(self):
        self.assertIsInstance(LocomoAdapter(Path('unused')),DatasetAdapter)
        self.assertIsInstance(TravelPlannerAdapter(),DatasetAdapter)
        self.assertTrue(hasattr(LocomoEvaluator,'evaluate'))
        self.assertTrue(hasattr(TravelPlannerEvaluator,'evaluate'))

    def test_locomo_reference_change_cannot_change_generation_input(self):
        path=ROOT/'datasets/locomo/data/locomo10_zh.json'
        raw=next(c for c in json.loads(path.read_text()) if c['sample_id']=='conv-26')
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'data.json';p.write_text(json.dumps([raw],ensure_ascii=False))
            adapter=LocomoAdapter(p);before=adapter.generation_input('conv-26')
            for q in raw['qa']:
                q['answer']='FORBIDDEN GOLD';q['category']=987;q['evidence']=['FORBIDDEN EVIDENCE'];q['adversarial_answer']='FORBIDDEN TRAP'
            p.write_text(json.dumps([raw],ensure_ascii=False))
            self.assertEqual(before.to_dict(),adapter.generation_input('conv-26').to_dict())
            self.assertEqual(len(before.questions),199)
            # 生成只见原始对话文本、说话人与会话日期；标注层与评测字段一律不进生成。
            self.assertEqual({b.source.kind for b in before.corpus},{'message_text'})
            self.assertTrue(all(set(b.metadata)=={'speaker','date'} for b in before.corpus))
            self.assertTrue(any(b.metadata['date'] for b in before.corpus))

    def test_travel_level_cannot_change_generation_input(self):
        adapter=TravelPlannerAdapter(tp_root=ROOT/'tests/fixtures/travel_environment');before=adapter.generation_input('0')
        rows=[json.loads(s) for s in (adapter.data_dir/'train.queries.jsonl').read_text().splitlines()]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rows[0]['level']='FORBIDDEN GOLD CATEGORY'
            (root/'train.queries.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            after=TravelPlannerAdapter(data_dir=root,tp_root=adapter.tp_root).generation_input('0')
            self.assertEqual(before.to_dict(),after.to_dict())
            self.assertNotIn('level',before.questions[0].parameters)
            self.assertEqual({b.source.kind for b in before.corpus},{'reference_table','allowed_environment'})
            self.assertTrue(any(b.metadata['table']=='cities' for b in before.corpus))

    def test_adapters_do_not_call_model_or_modify_answers(self):
        for name in ['locomo','travelplanner']:
            source=(ROOT/'datasets'/name/'adapter.py').read_text();tree=ast.parse(source)
            self.assertFalse(any(isinstance(n,ast.Attribute) and n.attr in {'chat','run','answer','repair','publish'} for n in ast.walk(tree)))
            self.assertNotIn('darwinagent.llm',source)

    def test_locked_evaluator_unchanged(self):
        verify_files(ROOT,json.loads(LOCK_PATH.read_text()))

    def test_no_preseeded_conversation_assets(self):
        files=[p for p in (ROOT/'tasks/conversation_memory').rglob('*') if p.is_file()]
        # Only the task declaration and its fixed seed schema; no generated F/C/P seeds.
        self.assertEqual(sorted(p.name for p in files),['schema.yaml','task.yaml'])

    def test_generation_contract_rejects_evaluation_metadata(self):
        from darwinagent.contracts import CaseInput,CorpusBlock,SourceRef
        from darwinagent.kernel.validation import validate_case
        c=case()
        bad=CaseInput(c.id,(CorpusBlock(c.corpus[0].source,c.corpus[0].text,{'gold':'x'}),),c.questions)
        s=TaskSpec.load(ROOT/'tasks/device_maintenance/task.yaml')
        with self.assertRaises(ValueError): validate_case(bad,s)

    def test_exports_preserve_published_content(self):
        from darwinagent.contracts import AnswerResult,RunResult,SourceRef
        from datasets.locomo.exports import legacy_rows
        answer=AnswerResult('0','answered','约翰住在北京。',(SourceRef('text','doc','row'),),node_ids=('n0',))
        row=legacy_rows(RunResult('c','identity','assets',(answer,),1))[0]
        self.assertEqual(row['answer'],'约翰住在北京。')
        from datasets.travelplanner.exports import plan
        text='[{"hotel":"original","unfilled":[]}]'
        a=AnswerResult('0','answered',text,answer.evidence,node_ids=('n0',))
        self.assertEqual(plan(a),json.loads(text))

    def test_dataset_entries_use_same_pipeline(self):
        for name in ['locomo','travelplanner']:
            source=(ROOT/'datasets'/name/'run.py').read_text()
            self.assertIn('from darwinagent.engine import Pipeline',source)
            self.assertNotIn('class ',source)
