"""锁定资产多对话外测（评审#3）：同一套 S/F/P 在多个独立对话的冻结快照上逐段评测。

不冷启动、不迭代、不按对话改策略；纯向量臂与融合臂共用记忆快照、模型与评测协议。
输出逐对话 精准/宽松/故障/成本 ＋按对话宏平均与按题汇总；--baseline 给另一臂的外测
目录时附逐对话差值。资产目录＝campaign published 下带 manifest.json 的 bundle 目录。
"""
import argparse
import asyncio
import json
from pathlib import Path

from oak.config import RunConfig
from oak.engine import Pipeline
from oak.kernel import KernelBundle, TaskSpec
from oak.llm.client import LLMClient
from oak.runtime.artifacts import atomic_json

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.evaluator import LocomoEvaluator
from datasets.locomo.run import ROOT, SNAPSHOTS, TASK_DIR, arm_config, connection

METRICS = ('original_precise', 'original_lenient', 'repaired_precise', 'repaired_lenient')


async def evaluate_conversation(case_id, adapter, task, bundle, config, root):
    case = adapter.generation_input(case_id)
    client = LLMClient(connection(root / case_id))
    try:
        pipeline = Pipeline(client, root / case_id / 'generation', frozen_snapshot=SNAPSHOTS / case_id)
        result = await pipeline.run(case, task.with_bundle(bundle), config)
        scores = await LocomoEvaluator(client, root / case_id / 'evaluation').evaluate(result)
        cost = client.ledger_summary()
    finally:
        await client.aclose()
    row = {'case_id': case_id, 'completed': scores.completed, 'total': scores.total,
           'generation_faults': scores.generation_faults,
           'evaluation_faults': scores.evaluation_faults,
           **{m: scores.metrics.get(m) for m in METRICS if m in scores.metrics},
           'llm_calls': cost.get('total_calls'), 'prompt_tokens': cost.get('prompt_tokens'),
           'completion_tokens': cost.get('completion_tokens')}
    atomic_json(root / case_id / 'scores.json', row)
    return row


def _rate(row, metric):
    """逐对话正确率＝答对题数／总题数（故障题留在分母，不得借删除故障抬高正确率）。"""
    value, total = row.get(metric), row.get('total')
    if isinstance(value, (int, float)) and isinstance(total, (int, float)) and total:
        return value / total
    return None


def aggregate_report(rows, baseline_rows=None):
    """宏平均＝逐对话正确率的等权平均；按题汇总＝答对和／总题和（评审#1：不同对话题量
    不同，答对题数的直接平均是错误统计）。差值一律用正确率，单位百分点（pp）。
    原始答对题数、总题数、完成数与故障数逐对话保留，故障另计不折算。"""
    report = {'per_conversation': rows}
    macro = {}
    micro = {'total_sum': sum(r.get('total', 0) for r in rows)}
    for k in ('generation_faults', 'evaluation_faults'):
        micro[k] = sum(r.get(k, 0) for r in rows)
    for m in METRICS:
        rates = [_rate(r, m) for r in rows]
        rates = [x for x in rates if x is not None]
        if rates:
            macro[m + '_rate'] = round(100 * sum(rates) / len(rates), 2)     # 宏平均：等权
            correct = sum(r.get(m, 0) for r in rows)
            micro[m + '_correct'] = correct
            if micro['total_sum']:
                micro[m + '_rate'] = round(100 * correct / micro['total_sum'], 2)  # 按题汇总
    report['macro'] = macro
    report['micro'] = micro
    if baseline_rows:
        base = {r['case_id']: r for r in baseline_rows}
        deltas = []
        for r in rows:
            b = base.get(r['case_id'])
            if b is None:
                continue
            d = {'case_id': r['case_id']}
            for m in METRICS:
                mine, yours = _rate(r, m), _rate(b, m)
                if mine is not None and yours is not None:
                    d[m + '_delta_pp'] = round(100 * (mine - yours), 2)      # 百分点
            deltas.append(d)
        report['delta_vs_baseline'] = deltas
        if deltas:
            report['macro']['delta_vs_baseline_pp'] = {
                m: round(sum(d[m + '_delta_pp'] for d in deltas) / len(deltas), 2)
                for m in METRICS if all(m + '_delta_pp' in d for d in deltas)}
    return report


async def main(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
    task = TaskSpec.load(TASK_DIR / 'task.yaml')
    bundle = KernelBundle(Path(args.assets))
    config = arm_config(args.arm, args.vector_k)
    cases = [c.strip() for c in args.cases.split(',') if c.strip()]
    for c in cases:
        if not (SNAPSHOTS / c / 'manifest.json').exists():
            raise SystemExit(f'冻结快照缺失: {c}')
    rows = []
    for c in cases:
        row = await evaluate_conversation(c, adapter, task, bundle, config, root)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    baseline_rows = None
    if args.baseline:
        bp = Path(args.baseline) / 'report.json'
        if bp.exists():
            baseline_rows = json.loads(bp.read_text())['per_conversation']
    report = aggregate_report(rows, baseline_rows)
    atomic_json(root / 'report.json', report)
    print(json.dumps(report['macro'], ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--arm', choices=('v0', 'g1'), required=True)
    p.add_argument('--assets', required=True, help='锁定的 bundle 目录（含 manifest.json）')
    p.add_argument('--cases', required=True, help='逗号分隔对话 id，如 conv-30,conv-41,conv-48')
    p.add_argument('--output', required=True)
    p.add_argument('--vector-k', type=int, default=60)
    p.add_argument('--baseline', help='另一臂外测输出目录（含 report.json），报告逐对话差值')
    asyncio.run(main(p.parse_args()))
