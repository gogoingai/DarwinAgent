"""历史故障回归（用户标准：单测必须发现所有问题）。

扫描全部历史实验根的故障轮：取该轮真实候选 bundle，用穷尽单测电池重放——
每个「当时在全量炸过的错误类」都必须被电池在准入层复现（拦下）。
用法：uv run python -m datasets.locomo.scripts.regress_history
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RUNS = Path(os.environ.get('OAK_HISTORY_ROOT', REPO / 'datasets/locomo/runs'))
REPORT = Path(os.environ.get('OAK_HISTORY_REPORT',
                             REPO / '.cache/diagnostics/history-replay.json'))


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


def failed_calls(round_dir):
    """Replay actual failed actions; legacy traces have only the last model action."""
    answers=round_dir/'generation/conv-26/answers'
    for path in sorted(answers.glob('*.json')):
        answer=json.loads(path.read_text())['result']
        if answer.get('status')!='execution_error':
            continue
        seen=False
        for event in answer.get('trace',()):
            if event.get('stage')=='tool_error':
                seen=True
                yield answer['question_id'],event['asset_id'],event['parameters'],answer['error']
        if not seen and str(answer.get('error','')).startswith(('SandboxError:', 'ValueError:')):
            for raw in reversed(answer.get('raw_outputs',())):
                try:
                    action=json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(action,dict) and action.get('action')=='call':
                    yield answer['question_id'],action['asset_id'],action['parameters'],answer['error']
                    break


def battery_errors(bundle_dir, graph, sample_cap=14):
    """穷尽电池重放：返回每个 F 触发的错误（asset, 错误类）。"""
    from darwinagent.experiments.runner import stress_trial_samples
    from darwinagent.kernel.functions import FunctionRegistry
    from darwinagent.kernel import KernelBundle
    from darwinagent.operators.sandbox import Limits
    bundle = FunctionRegistry(KernelBundle(bundle_dir), Limits(30000, 15.0, 180000))
    from darwinagent.experiments.admission_rules import loop_carried_capability_errors
    hits = []
    not_replayed = []
    for aid, (a, _fn) in bundle.functions.items():
        if 'semantic_search' in a.content:
            not_replayed.append(aid)
            continue
        if loop_carried_capability_errors(a.content):
            hits.append((aid, 'budget exhausted(static:loop-carried)'))
        samples = stress_trial_samples(list(a.trial_inputs), graph)[:sample_cap]
        for params in samples:
            try:
                bundle.call(aid, params, graph)
            except ValueError as exc:
                hits.append((aid, str(exc)[:80]))   # 全样本全错误类（不做首错截断）
    return hits, not_replayed


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
    from datasets.locomo.run import LocomoAdapter, SNAPSHOTS
    from darwinagent.experiments.snapshots import load_frozen_graph
    adapter = LocomoAdapter(REPO / 'datasets/locomo/data/locomo10_zh.json')
    graph = load_frozen_graph(SNAPSHOTS / 'conv-26',
                              adapter.generation_input('conv-26').corpus)
    report, missed, attempted, not_replayed = [], 0, 0, 0
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
            hits, remote_assets = battery_errors(cand, graph)
            from darwinagent.kernel import KernelBundle
            from darwinagent.kernel.functions import FunctionRegistry
            from darwinagent.operators.sandbox import Limits
            registry=FunctionRegistry(KernelBundle(cand),Limits(30000,15.0,180000))
            call_results=[]
            for qid,aid,params,expected in failed_calls(rnd):
                if aid in remote_assets:
                    not_replayed+=1
                    call_results.append({'question_id':qid,'asset_id':aid,
                                         'status':'not_replayed',
                                         'reason':'Remote embedding is unavailable offline'})
                    continue
                attempted+=1
                try:
                    registry.call(aid,params,graph)
                    actual='unexpected_success'
                except Exception as exc:
                    actual=f'{type(exc).__name__}: {exc}'
                matched=actual==expected
                if not matched:
                    missed+=1
                call_results.append({'question_id':qid,'asset_id':aid,'matched':matched,
                                     'expected':expected[:120],'actual':actual[:120]})
            caught = {normalize(e) for _, e in hits} - {None}
            needed = {normalize(e) for e in real} - {None}
            gap = needed - caught
            replay_caught={normalize(c['actual']) for c in call_results
                           if c.get('matched') is True} - {None}
            unresolved=gap-replay_caught
            status = ('PASS' if not gap else
                      f'REPLAY_CAUGHT {sorted(gap)}' if not unresolved else
                      f'MISS {sorted(unresolved)}')
            missed += len(unresolved)
            report.append((root.name, rnd.name, sorted(needed), sorted(caught), status,
                           call_results))
    for r in report:
        print(f'{r[0]}/{r[1]} battery={r[4]} '
              f'actual_matched={sum(c.get("matched") is True for c in r[5])} '
              f'actual_mismatched={sum(c.get("matched") is False for c in r[5])} '
              f'not_replayed={sum(c.get("status") == "not_replayed" for c in r[5])}')
    print(f'\n实际失败参数回放 {attempted} 次，远程调用未回放 {not_replayed} 次；'
          + ("故障路径匹配" if attempted and not missed else f"未匹配/漏拦 {missed} 次"))
    return 1 if missed or not attempted or not_replayed else 0


if __name__ == '__main__':
    if '--worker' in sys.argv:
        sys.exit(main())
    from darwinagent.runtime.artifacts import atomic_json
    try:
        worker = subprocess.run(
            [sys.executable, '-m', 'datasets.locomo.scripts.regress_history',
             '--worker'],
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        atomic_json(REPORT,{'status':'timeout','history_root':str(RUNS),
                            'error':'History replay worker exceeded 180 seconds'})
        print('历史回放工作进程超时，检查未完成', file=sys.stderr)
        sys.exit(2)
    atomic_json(REPORT,{'status':'passed' if worker.returncode==0 else 'failed',
                        'history_root':str(RUNS),'exit_code':worker.returncode,
                        'output':worker.stdout[-12000:],
                        'error':worker.stderr[-2000:]})
    print(worker.stdout, end='')
    if worker.stderr:
        print(worker.stderr, file=sys.stderr, end='')
    sys.exit(worker.returncode)
