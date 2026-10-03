import asyncio
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from datasets.locomo.pipeline.data import QA
from datasets.locomo.pipeline.dates import answer_equivalent, resolve_relative
from datasets.locomo.pipeline.judge import deterministic_grade, is_clean_refusal, _aggregate, Grade
from datasets.locomo.pipeline.protocol import aggregate, checked_json, validate_verdict, dual_grade, complete_equal, dual_grade_batch


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.cfg = SimpleNamespace(model_for=lambda role: 'test')
    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=next(self.replies))


class ProtocolTests(unittest.TestCase):


    def test_no_substring_shortcuts(self):
        for gold,pred,cat in [(2,'不是2，而是三个。',4),('不是','不是，但实际做过。',5),
                              ('两年前','不是两年前，是昨天。',2)]:
            self.assertIsNone(deterministic_grade(QA(0,'问题',cat,gold),pred))
        self.assertFalse(is_clean_refusal('无法确定，但可能是吉他'))
        self.assertFalse(is_clean_refusal('对话中未提及该信息，但可能是吉他'))
        self.assertTrue(is_clean_refusal('对话中未提及该信息。'))
        self.assertTrue(answer_equivalent('2023年5月7日','2023-05-07'))

    def test_complete_equality_preserves_meaning(self):
        self.assertFalse(complete_equal("1²", "12"))
        self.assertFalse(complete_equal("。", ""))
        self.assertFalse(complete_equal("", ""))
        self.assertFalse(complete_equal("1 2", "12"))
        self.assertFalse(complete_equal("A B", "AB"))
        self.assertIsNone(deterministic_grade(QA(0,"数量",3,"12"),"1 2"))
        self.assertIsNone(deterministic_grade(QA(0,"年份",2,"2022"),"20 22"))
        self.assertFalse(complete_equal("1.5", "15"))
        self.assertFalse(complete_equal("A、BC", "AB、C"))
        self.assertFalse(answer_equivalent("1.5", "15"))
        self.assertFalse(answer_equivalent("A、BC", "AB、C"))
        self.assertFalse(answer_equivalent("2023年5月", "2023年5月7日"))
        self.assertFalse(answer_equivalent("甲、乙", "乙、甲"))

    def test_failed_verdict_retry_budget_is_frozen(self):
        c=FakeClient(['','',''])
        qa=QA(0,'q',4,'gold')
        with TemporaryDirectory() as tmp:
            first=asyncio.run(dual_grade(qa,'answer',c,'source',Path(tmp)))
            second=asyncio.run(dual_grade(qa,'answer',c,'source',Path(tmp)))
        self.assertEqual(first,second)
        self.assertEqual(first['status'],'evaluation_error')
        self.assertEqual(len(c.calls),3)

    def test_relative_weeks(self):
        self.assertEqual(resolve_relative(date(2023,7,17),'两周前')[0],'2023-07-03')

    def test_invalid_verdict_not_wrong(self):
        c=FakeClient(['','not json','{"lenient":true,"precise":false}'])
        r=asyncio.run(checked_json(c,messages=[],namespace='test',validator=validate_verdict))
        self.assertEqual(r['status'],'evaluation_error')
        self.assertIsNone(r['lenient'])
        self.assertEqual(len(c.calls),3)

    def test_metric_consistency(self):
        base={'lenient':True,'precise':True,'missing_elements':[], 'wrong_elements':[],
              'precision_issues':[],'reason':'yes','reference_issue':''}
        self.assertEqual(validate_verdict(base),base)
        for changes in [{'lenient':False},{'missing_elements':['游泳']},{'wrong_elements':['错误主体']},
                        {'precision_issues':['日期不够精确']}]:
            with self.assertRaises(ValueError):validate_verdict({**base,**changes})

    def test_error_bounds(self):
        r=aggregate([{'idx':0,'status':'ok','lenient':True,'precise':False},
                     {'idx':1,'status':'evaluation_error','lenient':None,'precise':None}],{0})
        self.assertIsNone(r['overall']['lenient']['rate'])
        self.assertEqual(r['overall']['lenient']['lower_bound'],0.5)
        self.assertEqual(r['overall']['lenient']['upper_bound'],1)
        self.assertEqual(r['undisputed']['n'],1)
        legacy=_aggregate([QA(0,'q',4,'g')],[Grade(0,'evaluation_error','llm')],'conv')
        self.assertIsNone(legacy['exact_rate'])





    def test_batch_uses_complete_dialogue_and_keeps_ids(self):
        import json
        from datasets.locomo.pipeline.experiment import context_for
        conv=SimpleNamespace(sessions=[SimpleNamespace(no=i,date_raw='2023',turns=[
            SimpleNamespace(dia_id=f'D{i}:1',speaker='甲',text=f'会话{i}的事实')]) for i in [1,2]])
        source=context_for(conv,0)
        self.assertIn('D1:1',source)
        self.assertIn('D2:1',source)
        class BatchClient(FakeClient):
            async def chat(self,**kwargs):
                self.calls.append(kwargs)
                payload=json.loads(kwargs['messages'][1]['content'])
                self.last=payload
                verdicts=[{'id':r['id'],'lenient':True,'precise':True,
                    'missing_elements':[],'wrong_elements':[],'precision_issues':[],
                    'reference_issue':'','reason':'核验全部原文'} for r in payload['requests']]
                return SimpleNamespace(content=json.dumps({'results':verdicts}))
        c=BatchClient([])
        with TemporaryDirectory() as tmp:
            items=[(QA(i,'问题'+str(i),4,'标准'),'不同措辞','ok') for i in range(4)]
            r=asyncio.run(dual_grade_batch(items,c,source,Path(tmp)))
            self.assertEqual([x['idx'] for x in r],[0,1,2,3])
            self.assertEqual(len(c.calls),1)
            self.assertEqual(c.last['source_context'],source)
            again=asyncio.run(dual_grade_batch(items,c,source,Path(tmp)))
            self.assertEqual(len(c.calls),1)
            self.assertEqual(r,again)

    def test_source_preserves_caption_but_excludes_search_intent(self):
        from datasets.locomo.pipeline.experiment import transcript
        from datasets.locomo.pipeline.data import load_conversation
        conv=load_conversation(Path('datasets/locomo/data/locomo10.json'),'conv-26')
        source=transcript(conv)
        self.assertIn('trans lives matter',source.lower())
        self.assertIn('原始机器图片说明',source)
        self.assertNotIn('搜图词，仅意图不是事实',source)
        self.assertIn('搜图词，仅意图不是事实',transcript(conv,True))

    def test_answer_error_not_refusal(self):
        with TemporaryDirectory() as tmp:
            c=FakeClient([])
            r=asyncio.run(dual_grade(QA(0,'q',5,None),'',c,'',Path(tmp),'answer_error'))
            self.assertEqual(r['status'],'answer_error')
            self.assertEqual(len(c.calls),0)


if __name__=='__main__':unittest.main()
