"""历史故障回归（用户标准：单测必须发现所有问题）。

扫描全部历史实验根的故障轮：取该轮真实候选 bundle，用穷尽单测电池重放——
每个「当时在全量炸过的错误类」都必须被电池在准入层复现（拦下）。
用法：uv run python -m datasets.locomo.scripts.regress_history
"""
import json
import sys
import tempfile
from pathlib import Path

REPO = Path('/Users/xu/git/oak')
RUNS = REPO / 'datasets/locomo/runs'


def fault_classes(round_dir):
    """从该轮答案检查点提取真实炸过的错误类（前 80 字符去重）。"""
    answers = round_dir / 'generation/conv-26/answers'
    classes = set()
    if not answers.exists():
        return classes
    for f in answers.glob('*.json'):
        r = json.loads(f.read_text())['result']
        if r.get('status') == 'execution_error':
            classes.add(str(r.get('error'))[:80])
    return classes


def battery_errors(bundle_dir, graph, sample_cap=14):
    """穷尽电池重放：返回每个 F 触发的错误（asset, 错误类）。"""
    from oak.experiments.runner import stress_trial_samples
    from oak.kernel.functions import FunctionRegistry
    from oak.kernel import KernelBundle
    from oak.operators.sandbox import Limits
    bundle = FunctionRegistry(KernelBundle(bundle_dir), Limits(30000, 15.0, 180000))
    from oak.kernel.validation import loop_carried_capability_errors
    hits = []
    for aid, (a, _fn) in bundle.functions.items():
        if loop_carried_capability_errors(a.content):
            hits.append((aid, 'budget exhausted(static:loop-carried)'))
        samples = stress_trial_samples(list(a.trial_inputs), graph)[:sample_cap]
        for params in samples:
            try:
                bundle.call(aid, params, graph)
            except ValueError as exc:
                hits.append((aid, str(exc)[:80]))   # 全样本全错误类（不做首错截断）
    return hits


NOT_F_TESTABLE = ('ProtocolError', 'Feedback retries', 'Invalid isoformat')


def normalize(err):
    for key in NOT_F_TESTABLE:
        if key in err:
            return None              # 协议/模型层故障不属 F 单测职责（冒烟层兜）
    for key in ('Container-to-string', 'budget exhausted', 'expected', 'Unexpected',
                'Unknown graph node', 'Result byte limit'):
        if key in err:
            return 'budget exhausted' if key == 'budget exhausted' and '(static' in err \
                else ('budget exhausted' if key == 'budget exhausted' else key)
    return err[:40]


def main():
    from datasets.locomo.run import bootstrap_trial_graph, LocomoAdapter
    adapter = LocomoAdapter(REPO / 'datasets/locomo/data/locomo10_zh.json')
    graph = bootstrap_trial_graph(adapter)
    report, missed = [], 0
    for root in sorted(RUNS.glob('agentic_v*')):
        g1 = root / 'g1'
        if not (g1 / 'train').is_dir():
            continue
        for rnd in sorted((g1 / 'train').glob('R*')):
            cand = rnd / 'candidate/bundle'
            if not (cand / 'manifest.json').exists():
                continue
            real = fault_classes(rnd)
            if not real:
                continue                      # 无故障轮不作数
            hits = battery_errors(cand, graph)
            caught = {normalize(e) for _, e in hits} - {None}
            needed = {normalize(e) for e in real} - {None}
            gap = needed - caught
            status = 'PASS' if not gap else f'MISS {sorted(gap)}'
            if gap:
                missed += len(gap)
            report.append((root.name, rnd.name, sorted(needed), sorted(caught), status))
    for r in report:
        print(r)
    print(f'\n结论: {"全部历史故障类均被电池拦截" if not missed else f"漏拦 {missed} 类——电池需补"}')
    return 1 if missed else 0


if __name__ == '__main__':
    sys.exit(main())
