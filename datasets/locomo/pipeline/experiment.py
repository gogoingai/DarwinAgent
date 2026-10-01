"""Conv-26 fixed-graph experiment: blind audit, unified history, one answer run, diagnosis.

python -m datasets.locomo.pipeline.experiment [--stage all|audit|history|answer|diagnose|review]
No source dataset, historical run or frozen artifact is modified.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import json
import inspect
from pathlib import Path
import random
import re
import subprocess

from oak.kg.graph import load_graph
from oak.llm.client import LLMClient
from .agent import run_qa
from .config import LOCOMO_TASK_DIR, load_locomo_config
from .data import load_conversation
from .judge import load_repairs
from .protocol import VERSION, RULES, aggregate, checked_json, digest, dual_grade, dual_grade_batch
from .tools import ToolBox

ROOT = LOCOMO_TASK_DIR
EXP = ROOT / 'runs/experiments/conv26_dual_v4'
ANSWER_RUN = ROOT / 'runs/experiments/conv26_dual_v2'
GRAPH = ROOT / 'runs/conv-26/graph_d0953122/graph.json'
AUDIT_RULES = '''你是独立的标准答案审计员。禁止读取、推测或迎合任何系统回答。
只根据问题、中文原始语料与英文对照审查候选修复。
必须保持原问题的对象、时间和任务语义。缺失图片细节不能用另一个物品/更粗答案替代；
无法从文本回答的题可以改成不可回答，但必须确实没有文本证据。
纯等义措辞无需改 gold，由评测器处理。不得因标准答案措辞不在译文逐字出现就宣称不可达。
候选依据与系统有关时忽略该依据，重新独立判断。信息不足或歧义标 disputed。
decision=keep|revoke|disputed；keep 时 answer 必须为修复后的完整答案或 null（不可回答）。
输出 JSON：{"decision":"keep|revoke|disputed","answer":字符串或null,
"reason":字符串,"sources":[dia_id字符串]}。不改问题，不展示系统输出。'''
AUDIT_RULES += '''\n核查全部来源层，不能只读消息正文就说整个语料没有某个图片细节。
图片说明是原始机器标注，可辅助核对；搜索词只是意图，不作发生事实。明确正文优先，相关来源冲突标争议。
合理推断题不能因为标准里的某个词未逐字出现在正文就宣称原gold错误。
教育目标与职业方向、画面细节与灵感主题、标志内容与观感、活动与拍照不能互换。
原答案与提议答案完全等义或只是数字类型不同，无需修复，应revoke。
原中文有翻译歧义时核对英文原句；不能让错误翻译改变答案对象。'''


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    temp.replace(path)


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def transcript(conv, audit_metadata=False):
    lines=[]
    for s in conv.sessions:
        lines.append(f'会话{s.no} 日期{s.date_raw}')
        for t in s.turns:
            lines.append(f'[{t.dia_id}] {t.speaker}: {t.text}')
            if getattr(t,'image_caption',''):
                lines.append(f'[{t.dia_id} 原始机器图片说明，非说话人台词] {t.image_caption}')
            if audit_metadata and getattr(t,'image_query',''):
                lines.append(f'[{t.dia_id} 搜图词，仅意图不是事实] {t.image_query}')
    lines.append('【数据集派生标注，不能覆盖明确原文】')
    lines.extend(f'[{dia}] {subject}: {body}' for dia,subject,body in getattr(conv,'observations',[]))
    lines.extend(f'[事件标注 {day}] {subject}: {body}' for day,subject,body in getattr(conv,'events',[]))
    return '\n'.join(lines)


def context_for(conv, idx):
    # The evaluator must see all sessions: absence in QA evidence sessions is not absence in the corpus.
    return transcript(conv)


class Experiment:
    def __init__(self):
        self.lc = load_locomo_config()
        # Explicitly pin repository dataset; do not follow LOCOMO_DATA to another checkout.
        self.lc.dataset_path = ROOT / 'data/locomo10_zh.json'
        self.conv = load_conversation(self.lc.dataset_path, 'conv-26')
        self.en = load_conversation(ROOT / 'data/locomo10.json', 'conv-26')
        # Keep the effective models/endpoints of the already frozen generation.
        self.lc.cfg.model_fast='glm-5.3-flash'
        self.lc.cfg.fast_base_url=self.lc.cfg.api_base_url
        self.lc.cfg.fast_api_key=self.lc.cfg.api_key
        self.lc.cfg.work_dir = EXP / 'runtime'
        self.client = LLMClient(self.lc.cfg)
        self.sem = asyncio.Semaphore(4)
        self.audit = []
        self.disputed = set()
        self.audited = list(self.conv.qas)
        self.source = transcript(self.conv) + '\n【完整英文原句对照；翻译歧义以原句为准】\n' + transcript(self.en)
        binding=json.loads((ANSWER_RUN/'generation_binding.json').read_text())
        parent=json.loads((ANSWER_RUN/'manifest.json').read_text())
        if (binding['answers_hash']!=digest((ANSWER_RUN/'optimized/answers.jsonl').read_text())
            or binding['checked_json_source']!=digest(inspect.getsource(checked_json))
            or any(digest((ROOT.parent.parent/p).read_text())!=h for p,h in binding['generation_code'].items())
            or binding['dataset']!=digest(self.lc.dataset_path.read_text())
            or binding['graph']!=digest(GRAPH.read_text())
            or binding['models']!={'strong':self.lc.cfg.model_strong,'fast':self.lc.cfg.model_fast}):
            raise RuntimeError('Frozen generation contract changed; refusing answer reuse')
        code = {str(p.relative_to(ROOT.parent.parent)): digest(p.read_text())
                for folder in [ROOT/'pipeline', ROOT.parent.parent/'oak'] for p in folder.rglob('*.py')}
        self.manifest = {'protocol': VERSION, 'rules': digest(RULES),
            'gold_audit_source': digest((EXP/'gold_audit.json').read_text()) if (EXP/'gold_audit.json').exists() else None, 'audit_rules': digest(AUDIT_RULES),
            'conv': 'conv-26', 'n': len(self.conv.qas), 'dataset': digest(self.lc.dataset_path.read_text()),
            'english_dataset': digest((ROOT/'data/locomo10.json').read_text()),
            'graph': digest(GRAPH.read_text()), 'schema': digest((ROOT/'runs/frozen/schema.yaml').read_text()),
            'topics': digest((ROOT/'runs/frozen/topics.json').read_text()),
            'candidate_repairs': digest((ROOT/'data/gold_repairs.jsonl').read_text()),
            'source_review': digest((EXP/'manual_gold_review.json').read_text()) if (EXP/'manual_gold_review.json').exists() else None,
            'models': {'strong': self.lc.cfg.model_strong, 'fast': self.lc.cfg.model_fast},
            'parameters': {'react_steps': 10, 'temperatures': [0.3,0.7,1.0], 'concurrency': 4, 'evaluation_batch_size': 4, 'evaluation_source': 'bilingual dialogue, image descriptions, supplied annotation layers'},
            'frozen_generation_reference':{'directory':str(ANSWER_RUN),'manifest':digest(parent),'binding':binding},
            'source_context':digest(self.source),
            'inputs': 'frozen graph includes dialogue extraction, observation and event_summary', 'code': code, 'git_head': subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()}
        manifest = EXP/'manifest.json'
        if manifest.exists() and json.loads(manifest.read_text()) != self.manifest:
            raise RuntimeError('Experiment fingerprint changed: choose a new EXP directory; refusing stale resume')
        write(manifest, self.manifest)

    async def audit_gold(self):
        path = EXP/'gold_audit.json'
        if path.exists():
            self.audit = json.loads(path.read_text())
        else:
            async def one(idx, repair):
                # Only proposed answer, never repair narrative referencing a system answer.
                payload = {'question': self.conv.qas[idx].question,
                           'original_gold': self.conv.qas[idx].answer,
                           'proposed_gold': None if repair.get('adversarial') else repair.get('answer'),
                           'english_question':self.en.qas[idx].question,
                           'chinese': transcript(self.conv,True), 'english': transcript(self.en,True)}
                def validate(o):
                    if o.get('decision') not in ('keep','revoke','disputed'):
                        raise ValueError('invalid decision')
                    if 'answer' not in o or (o['answer'] is not None and not isinstance(o['answer'],str)):
                        raise ValueError('invalid answer')
                    if not isinstance(o.get('reason'),str) or not o['reason'].strip():
                        raise ValueError('missing audit reason')
                    if not isinstance(o.get('sources'),list) or not all(isinstance(v,str) for v in o['sources']):
                        raise ValueError('invalid sources')
                    valid = {t.dia_id for s in self.conv.sessions for t in s.turns}
                    if not set(o['sources']) <= valid:
                        raise ValueError('nonexistent source')
                    if o['decision']=='keep' and not o['sources']:
                        raise ValueError('keep requires source citation')
                    return o
                async with self.sem:
                    r = await checked_json(self.client, namespace='lc26_audit_v3',validator=validate,max_tokens=4096,
                        messages=[{'role':'system','content':AUDIT_RULES},
                                  {'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
                if r['status']!='ok':
                    r.update(decision='disputed',answer=self.conv.qas[idx].answer,
                             reason='Audit execution unresolved',sources=[])
                print('audit',idx,r['decision'],flush=True)
                return {'idx':idx,**r}
            self.audit = await asyncio.gather(*(one(i,r) for i,r in load_repairs('conv-26').items()))
            write(path,self.audit)
        self.manifest['gold_audit_source']=digest(path.read_text())
        write(EXP/'manifest.json',self.manifest)
        review_path = EXP/'manual_gold_review.json'
        if review_path.exists():
            overrides = {r['idx']:r for r in json.loads(review_path.read_text())}
            self.audit = [{**row, 'model_decision':row['decision'],
                           **overrides.get(row['idx'], {})} for row in self.audit]
        write(EXP/'gold_audit_reviewed.json',self.audit)
        for row in self.audit:
            if row['decision']=='keep':
                self.audited[row['idx']] = replace(self.conv.qas[row['idx']], answer=row['answer'])
            if row['decision']=='disputed':
                self.disputed.add(row['idx'])
        write(EXP/'gold_original.json',[{'idx':q.idx,'question':q.question,'answer':q.answer} for q in self.conv.qas])
        write(EXP/'gold_audited.json',[{'idx':q.idx,'question':q.question,'answer':q.answer,
              'disputed':q.idx in self.disputed} for q in self.audited])

    async def evaluate(self, answers, label):
        if len(answers)!=199 or set(answers)!=set(range(199)):
            raise ValueError('Complete conv-26 answer set required')
        reports={}
        for version,qas in [('original',self.conv.qas),('audited',self.audited)]:
            blocks=[qas[n:n+4] for n in range(0,len(qas),4)]
            progress=0
            async def block(qblock):
                nonlocal progress
                items=[]
                for q in qblock:
                    a=answers[q.idx]
                    status=a.get('status','answer_error' if a.get('trajectory',{}).get('error') else 'ok')
                    items.append((q,a['answer'],status))
                async with self.sem:
                    rows=await dual_grade_batch(items,self.client,self.source,EXP/'verdict_cache')
                progress += len(rows)
                if progress%20==0 or progress==199:
                    print('grading',label,version,progress,'/199',flush=True)
                return rows
            grouped=await asyncio.gather(*(block(b) for b in blocks))
            reports[version]=aggregate([r for b in grouped for r in b],self.disputed)
        write(EXP/'evaluations'/f'{label}.json',reports)
        print('evaluated',label,{v:r['overall'] for v,r in reports.items()},flush=True)
        return reports

    async def history(self):
        table=[]
        previous=None
        for path in sorted((ROOT/'runs/conv-26').glob('iter*/answers.jsonl'),
                           key=lambda p:(int(re.search(r'\d+',p.parent.name)[0]),p.parent.name)):
            answers={r['idx']:r for r in jsonl(path)}
            if set(answers)!=set(range(199)):
                continue
            label=path.parent.name
            reports=await self.evaluate(answers,label)
            legacy=json.loads((path.parent/'report.json').read_text())
            row={'round':label,'answers_hash':digest(path.read_text()),'legacy_exact':legacy['exact'],
                 'legacy_original_exact':legacy.get('orig_exact'),
                 'input_note':'dialogue-only before iter12; adds observation/event_summary from iter12',
                 'graph_binding':'unknown: historical checkpoint did not record graph hash',
                 'metrics':{v:r['overall'] for v,r in reports.items()}}
            if previous:
                same=[i for i in answers if answers[i]['answer']==previous[0][i]['answer']]
                row['identical_answers_to_previous']=len(same)
                row['legacy_score_flips_for_identical_answers']=[i for i in same
                    if (previous[1][i]['grade'] == 'exact') != next(g['grade'] == 'exact' for g in legacy['grades'] if g['idx']==i)]
            previous=(answers,{g['idx']:g for g in legacy['grades']})
            table.append(row)
            write(EXP/'history.json',table)

    async def answer(self):
        # Evaluation upgrade: the single answer optimization run is already frozen.
        answers={r['idx']:r for r in jsonl(ANSWER_RUN/'optimized/answers.jsonl')}
        await self.evaluate(answers,'optimized')

    async def diagnose(self):
        tb=ToolBox(load_graph(GRAPH))
        answers={r['idx']:r for r in jsonl(ANSWER_RUN/'optimized/answers.jsonl')}
        path=EXP/'diagnostics.json'
        existing={r['idx']:r for r in json.loads(path.read_text())} if path.exists() else {}
        lock=asyncio.Lock()
        async def one(qa):
            if qa.idx in existing:
                return
            a=answers[qa.idx]
            alias={r['__id__']:fid for fid,r in tb.facts.items()}
            alias.update({e['__id__']:f"{e['etype']}:{name}" for name,e in tb.entities.items()})
            for n,props in tb.g.nodes(data=True):
                alias.setdefault(n,f'node:{n}')
            payload={'question':qa.question,'gold':qa.answer,'source':self.source,
                'all_graph_facts':list(tb.facts.values()),
                'graph_entities':tb.entities,'graph_session_dates':tb.session_dates,
                'graph_fact_mentions':tb.fact_entities,
                'graph_all_relations':[(alias[u],alias[v],d.get('relation')) for u,v,d in tb.g.edges(data=True)],
                'actual_final_context':a.get('trajectory',{}).get('final_input',a.get('context_text','')),
                'actual_final_fact_context':a.get('context_text',''),
                'final_context_ids':a.get('context_fids',[]),'prediction':a['answer']}
            def validate(o):
                elements=o.get('elements')
                if not isinstance(elements,list) or not elements:
                    raise ValueError('element audit required')
                for e in elements:
                    if not isinstance(e.get('element'),str):raise ValueError('missing element')
                    for k in ['in_graph','in_context','used_correctly']:
                        if e.get(k) not in ['yes','no','unknown']:raise ValueError('invalid state')
                    if not isinstance(e.get('support_ids'),list) or not set(e['support_ids'])<=set(tb.facts):
                        raise ValueError('invalid support ids')
                    if e['in_graph']=='no' and e['in_context']=='yes':raise ValueError('invalid hierarchy')
                    if e['in_context']=='yes' and not set(e['support_ids'])&set(a.get('context_fids',[])):
                        raise ValueError('context support required')
                if not isinstance(o.get('reason'),str):raise ValueError('missing reason')
                return o
            async with self.sem:
                r=await checked_json(self.client,namespace='lc26_diagnosis_v1',validator=validate,
                    max_tokens=3072,messages=[{'role':'system','content':
                    '这是独立诊断，绝不用于作答。分解gold必要要素，逐项核查图中是否有足够证据、'
                    '最终上下文是否包含、回答是否正确使用。相同出处不等于答案要素覆盖。'
                    '主体必须一致或有明确关系链；推断允许多条事实组合。缺图片信息或歧义标unknown。'
                    'in_graph只能依据graph字段，不得用source原文补足图缺失；in_context只能依据actual_final_context，'
                    '不能因事实编号出现就假设其全部属性也展示了。source只用于核对gold与原文。'
                    '不可回答题的要素为正确识别信息不可得，无法证实图中不存在则unknown。'
                    '输出JSON {"elements":[{"element":字符串,"in_graph":"yes|no|unknown",'
                    '"in_context":"yes|no|unknown","used_correctly":"yes|no|unknown",'
                    '"support_ids":[事实编号]}],"reason":字符串}。'},
                    {'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
            async with lock:
                existing[qa.idx]={'idx':qa.idx,**r}
                write(path,[existing[i] for i in sorted(existing)])
                if len(existing)%10==0:print('diagnosis',len(existing),flush=True)
        await asyncio.gather(*(one(q) for q in self.audited))

    async def review(self):
        baseline=json.loads((EXP/'evaluations/iter22.json').read_text())
        optimized=json.loads((EXP/'evaluations/optimized.json').read_text())
        old={r['idx']:r for r in jsonl(ROOT/'runs/conv-26/iter22/answers.jsonl')}
        new={r['idx']:r for r in jsonl(ANSWER_RUN/'optimized/answers.jsonl')}
        required={r['idx'] for r in self.audit}
        stable=[]
        diffs=[]
        for i in range(199):
            changes=[]
            all_correct=True
            for version in ['original','audited']:
                x,y=baseline[version]['grades'][i],optimized[version]['grades'][i]
                for metric in ['lenient','precise']:
                    if x.get(metric)!=y.get(metric):changes.append(f'{version}/{metric}')
                    all_correct &= x.get(metric) is True and y.get(metric) is True
                if x.get('lenient')!=x.get('precise') or y.get('lenient')!=y.get('precise'):
                    required.add(i)
                if x.get('reference_issue') or y.get('reference_issue'):
                    required.add(i)
            if changes:required.add(i)
            if all_correct and not changes:stable.append(i)
            diffs.append({'idx':i,'changed_metrics':changes})
        required.update(random.Random(26).sample(stable,min(20,len(stable))))
        jobs=[]
        for i in sorted(required):
            for label,source in [('baseline',old),('optimized',new)]:
                for version,qas in [('original',self.conv.qas),('audited',self.audited)]:
                    qa=qas[i]
                    rid=digest({'i':i,'answer':source[i]['answer'],'gold':qa.answer})
                    jobs.append((rid,label,version,qa,source[i]))
        random.Random(26).shuffle(jobs)
        write(EXP/'blind_review_map.json',[{'review_id':r,'source':l,'version':v,'idx':q.idx} for r,l,v,q,a in jobs])
        async def one(job):
            rid,label,version,qa,a=job
            path=EXP/'blind_reviews'/f'{rid}.json'
            if path.exists():return json.loads(path.read_text())
            async with self.sem:
                # Independent namespace/cache and no original verdict/round/system in judge input.
                r=await dual_grade(qa,a['answer'],self.client,self.source,
                                   EXP/'blind_verdict_cache',a.get('status','ok'),reviewer='blind')
            write(path,{'review_id':rid,**r})
            return r
        unique_jobs = {j[0]: j for j in jobs}
        unique_results = await asyncio.gather(*(one(j) for j in unique_jobs.values()))
        review_map = dict(zip(unique_jobs, unique_results))
        reviews = [review_map[j[0]] for j in jobs]
        disagreements=[]
        for (rid,label,version,qa,a),review in zip(jobs,reviews):
            primary=(baseline if label=='baseline' else optimized)[version]['grades'][qa.idx]
            if any(primary.get(k)!=review.get(k) for k in ['lenient','precise','status']):
                disagreements.append({'review_id':rid,'source':label,'version':version,'idx':qa.idx,
                    'primary':{k:primary.get(k) for k in ['lenient','precise','status']},
                    'blind':{k:review.get(k) for k in ['lenient','precise','status']}})
        write(EXP/'blind_disagreements.json',disagreements)
        write(EXP/'comparison.json' ,{'diffs':diffs,'blind_review_indices':sorted(required),
              'baseline':{v:r['overall'] for v,r in baseline.items()},
              'optimized':{v:r['overall'] for v,r in optimized.items()},
              'blind_disagreements':len(disagreements),
              'review_status':'needs_adjudication' if disagreements else 'consistent',
              'ledger':self.client.ledger_summary()})

    async def adjudicate(self):
        from .protocol import validate_verdict
        discrepancies=json.loads((EXP/'blind_disagreements.json').read_text())
        if not discrepancies:
            return
        old={r['idx']:r for r in jsonl(ROOT/'runs/conv-26/iter22/answers.jsonl')}
        new={r['idx']:r for r in jsonl(ANSWER_RUN/'optimized/answers.jsonl')}
        unique={r['review_id']:r for r in discrepancies}
        async def one(item):
            rid=item['review_id'];i=item['idx'];version=item['version'];label=item['source']
            path=EXP/'adjudications'/f'{rid}.json'
            primary=json.loads((EXP/'evaluations'/f"{'iter22' if label=='baseline' else 'optimized'}.json").read_text())[version]['grades'][i]
            if path.exists():
                return primary['key'],json.loads(path.read_text())
            blind=json.loads((EXP/'blind_reviews'/f'{rid}.json').read_text())
            qa=(self.conv.qas if version=='original' else self.audited)[i]
            prediction=(old if label=='baseline' else new)[i]['answer']
            reasons=[{k:r.get(k) for k in ['lenient','precise','missing_elements','wrong_elements',
                                          'precision_issues','reference_issue','reason']} for r in [primary,blind]]
            random.Random(rid).shuffle(reasons)
            payload={'question':qa.question,'gold':qa.answer,'answer':prediction,
                     'source_context':self.source,'anonymous_reviews':reasons}
            async with self.sem:
                verdict=await checked_json(self.client,namespace='lc26_referee_v1',validator=validate_verdict,
                    messages=[{'role':'system','content':RULES+'\n你是盲复核分歧的裁决员。独立逐项查原文，'
                        '两份匿名意见都可能错。不能投票、修改gold或以另一版本标准替代。输出相同JSON。'},
                        {'role':'user','content':json.dumps(payload,ensure_ascii=False)}])
            result={**verdict,'idx':i,'key':primary['key'],'review_id':rid,
                    'adjudication':{'primary':primary,'blind':blind}}
            write(path,result)
            return primary['key'],result
        resolved=dict(await asyncio.gather(*(one(x) for x in unique.values())))
        for key,result in resolved.items():
            write(EXP/'verdict_cache'/f'{key}.json',result)
        for path in (EXP/'evaluations').glob('*.json'):
            report=json.loads(path.read_text())
            write(EXP/'evaluation_snapshots'/path.name,report)
            revised={}
            for version,r in report.items():
                revised[version]=aggregate([resolved.get(g['key'],g) for g in r['grades']],self.disputed)
            write(path,revised)
        history_path=EXP/'history.json'
        if history_path.exists():
            table=json.loads(history_path.read_text())
            for row in table:
                r=json.loads((EXP/'evaluations'/f"{row['round']}.json").read_text())
                row['metrics']={v:report['overall'] for v,report in r.items()}
            write(history_path,table)
        comparison=json.loads((EXP/'comparison.json').read_text())
        baseline=json.loads((EXP/'evaluations/iter22.json').read_text())
        optimized=json.loads((EXP/'evaluations/optimized.json').read_text())
        comparison['baseline']={v:r['overall'] for v,r in baseline.items()}
        comparison['optimized']={v:r['overall'] for v,r in optimized.items()}
        comparison['diffs']=[{'idx':i,'changed_metrics':[f'{v}/{k}' for v in ['original','audited']
            for k in ['lenient','precise'] if baseline[v]['grades'][i].get(k)!=optimized[v]['grades'][i].get(k)]}
            for i in range(199)]
        comparison['review_status']='adjudicated'
        comparison['adjudication_errors']=sum(r['status']!='ok' for r in resolved.values())
        comparison['ledger']=self.client.ledger_summary()
        write(EXP/'comparison.json',comparison)


async def main(stage):
    exp=Experiment()
    await exp.audit_gold()
    if stage in ['all','history']:await exp.history()
    if stage in ['all','answer']:await exp.answer()
    if stage in ['all','diagnose']:await exp.diagnose()
    if stage in ['all','review']:
        await exp.review()
        await exp.adjudicate()
    print('stage complete',stage,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',choices=['all','audit','history','answer','diagnose','review'],default='all')
    asyncio.run(main(parser.parse_args().stage))
