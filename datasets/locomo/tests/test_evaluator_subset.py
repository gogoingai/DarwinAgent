import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datasets.locomo.evaluator import LocomoEvaluator

class SubsetEvaluation(unittest.IsolatedAsyncioTestCase):
    async def test_sparse_ids_and_shuffled_grades(self):
        async def grade(items,*args,**kwargs):
            return [{'idx':q.idx,'status':'ok','lenient':True,'precise':True} for q,_,_ in reversed(items)]
        answers=tuple(SimpleNamespace(question_id=str(i),status='answered',answer='fixture',error=None) for i in (32,57,180))
        with tempfile.TemporaryDirectory() as td,patch('datasets.locomo.evaluator.dual_grade_batch',grade):
            result=await LocomoEvaluator(None,Path(td)).evaluate(SimpleNamespace(case_id='conv-26',answers=answers),asked=(32,57,180))
        self.assertEqual(result.total,3);self.assertEqual(result.completed,3)
        self.assertEqual(result.metrics['original_precise'],3)
        for row in result.diagnostics:
            self.assertEqual(row['original']['idx'],int(row['question_id']))
            self.assertEqual(row['repaired']['idx'],int(row['question_id']))

    async def test_incomplete_grade_identity_rejected(self):
        async def grade(items,*args,**kwargs):
            return [{'idx':0,'status':'ok','lenient':True,'precise':True}]
        a=SimpleNamespace(question_id='32',status='answered',answer='fixture',error=None)
        with tempfile.TemporaryDirectory() as td,patch('datasets.locomo.evaluator.dual_grade_batch',grade):
            with self.assertRaisesRegex(ValueError,'grade set'):
                await LocomoEvaluator(None,Path(td)).evaluate(SimpleNamespace(case_id='conv-26',answers=(a,)),asked=(32,))
