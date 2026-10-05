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


async def evaluate_conversation(case_id, adapter, task, bundle, config, root,
                                graph_builder=None):
    case = adapter.generation_input(case_id)
    client = LLMClient(connection(root / case_id))
    try:
        pipeline = Pipeline(client, root / case_id / 'generation',
                            frozen_snapshot=SNAPSHOTS / case_id,
                            graph_builder=graph_builder)
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
    # 逐对话正确率（评审④）：答对数/总题数，故障留在分母；宽松率一并给出
    for m in METRICS:
        if isinstance(row.get(m), (int, float)) and row['total']:
            row[m + '_rate'] = round(100 * row[m] / row['total'], 2)
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


def preflight(bundle, config, task, cases, snap_root=None, embedder_factory=None,
              graph_builder=None):
    """外测与冷启动/候选修订同一套能力准入（评审①③）：AST 底线＋逐对话快照真实试跑
    （图挂冻结向量索引）＋完整图 C 检查否决即拒。锁定资产不因换了入口而豁免。
    graph_builder 给定时（新图重建模式）试跑图＝按锁定 S 从固定事实重建——与答题
    Pipeline 同一派生规则，不得拿冻结旧图过检当新模式证据。"""
    import tempfile
    from oak.kernel.checks import CheckRegistry, enforce_opinions
    from oak.kernel.functions import FunctionRegistry
    from oak.kernel.validation import capability_floor_errors, capability_names, trial_capability_floor_errors
    from oak.operators.sandbox import Limits
    from oak.operators.data import DataCapabilities
    from oak.experiments.snapshots import attach_vector, load_frozen_graph
    snap_root = Path(snap_root) if snap_root is not None else SNAPSHOTS
    required = capability_names(task.retrieval_floor)
    problems = capability_floor_errors(bundle.assets, required)
    if problems:
        raise SystemExit('外测预检失败（静态能力底线）: ' + str(problems))
    limits = Limits(config.function_steps, config.function_timeout_s, config.result_bytes)
    schema = None
    if graph_builder is not None:
        from oak.kernel.validation import validate_bundle
        schema = validate_bundle(bundle)
    for case_id, case in cases.items():
        if graph_builder is not None:
            graph = graph_builder(snap_root / case_id, schema, case.corpus)
        else:
            graph = load_frozen_graph(snap_root / case_id, case.corpus)
            attach_vector(graph, snap_root / case_id, embedder_factory=embedder_factory)
        with tempfile.TemporaryDirectory() as td:
            exported = bundle.assets.export(Path(td) / 'b')
            caps = DataCapabilities(graph)
            try:
                enforce_opinions(CheckRegistry(exported, limits).run(
                    'graph', {'nodes': list(caps.rows.values()), 'stage': 'graph'}), f'外测[{case_id}]')
                records = FunctionRegistry(exported, limits).trial(graph,
                    {a.id: list(a.trial_inputs) for a in exported.assets.assets if a.kind == 'F'})
            except SystemExit:
                raise
            except ValueError as exc:   # 图 C 否决/能力缺失等准入错误：转为入口级失败
                raise SystemExit(f'外测预检失败（{case_id}）: {exc}') from exc
            except Exception as exc:
                raise SystemExit(f'外测预检失败（{case_id}）: {type(exc).__name__}: {exc}') from exc
            missing = trial_capability_floor_errors(records, required)
            if missing:
                raise SystemExit(f'外测预检失败（{case_id} 试跑未触发能力）: ' + str(missing))


def baseline_compatibility(mine, base):
    """共同实验条件核对（评审五）：模型路由、作答/审查配置、判题器锁、完整快照身份。
    两臂预期的检索方式（retrieval_mode/vector_k/tool_steps）与资产版本差异不算不兼容。"""
    if not base:
        return 'incompatible: baseline 缺少身份记录（旧版报告）'
    diffs = []
    if mine.get('transport') != base.get('transport'):
        diffs.append('模型路由不同')
    if mine.get('judge_lock') != base.get('judge_lock'):
        diffs.append('判题器锁不同')
    ANSWER_FIELDS = ('protocol_attempts', 'answer_attempts', 'temperature', 'max_tokens',
                     'calls_per_question')
    a, b = mine.get('run_config') or {}, base.get('run_config') or {}
    unequal = [k for k in ANSWER_FIELDS if a.get(k) != b.get(k)]
    if unequal:
        diffs.append('作答/审查配置不同: ' + ','.join(unequal))
    if mine.get('snapshots') != base.get('snapshots'):
        diffs.append('快照身份不同')
    return 'compatible' if not diffs else 'incompatible: ' + '；'.join(diffs)


def experiment_identity(bundle, config, conn, cases, snap_root=None,
                        graph_builder=None):
    """实验条件身份（评审④）：资产版本、模型路由、运行配置、各对话快照指纹、
    冻结判题器文件锁——基线比对时核对共同条件，缺失/不兼容明确报出。
    新图重建模式（graph_builder 给定）加记：投影规则源码摘要、锁定 S 摘要、
    各对话实际重建图摘要——旧图缓存/不同 S 不得冒充同身份（CONTINUE.md 阶段六）。"""
    from oak.experiments.spec import precheck_identity
    from datasets.locomo.evaluator import AUDITED, LOCK_PATH
    from oak.runtime.artifacts import digest
    from oak.runtime.identity import snapshot_files
    snap_root = Path(snap_root) if snap_root is not None else SNAPSHOTS
    identity = {'asset_version': bundle.version,
                'transport': precheck_identity(conn, config)['transport'],
                'run_config': config.to_dict(),
                'snapshots': {c: json.loads((snap_root / c / 'manifest.json').read_text())['snapshot_digest']
                              for c in cases},
                'judge_lock': digest(snapshot_files([AUDITED, LOCK_PATH]))}
    if graph_builder is not None:
        import inspect
        from oak.kernel.validation import validate_bundle
        from oak.operators.data import DataCapabilities
        try:
            builder_src = inspect.getsource(graph_builder)
        except (OSError, TypeError):
            builder_src = repr(graph_builder)
        identity['graph_mode'] = 'rebuild'
        identity['graph_rules'] = digest(builder_src)
        schema = validate_bundle(bundle)
        identity['schema'] = digest(schema.to_yaml())
        identity['rebuilt_graphs'] = {}
        adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
        for c in cases:
            graph = graph_builder(snap_root / c, schema, adapter.generation_input(c).corpus)
            # 与准入报告同口径的图身份：排序后数据行摘要（admission._graph_identity）
            identity['rebuilt_graphs'][c] = digest(
                sorted(DataCapabilities(graph).rows.values(), key=lambda r: r['node_id']))
    return identity


async def main(args):
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
    task = TaskSpec.load(TASK_DIR / 'task.yaml')
    bundle = KernelBundle(Path(args.assets))
    config = arm_config(args.arm, args.vector_k)
    # 新图重建模式（CONTINUE.md 阶段六）：默认关闭＝冻结图旧行为逐字节不变；
    # 开启后预检与答题 Pipeline 同用 rebuild_snapshot_graph（同一派生规则）。
    graph_builder = None
    if getattr(args, 'graph_rebuild', False):
        from datasets.locomo.graph_rules import rebuild_snapshot_graph
        graph_builder = rebuild_snapshot_graph
    cases = [c.strip() for c in args.cases.split(',') if c.strip()]
    for c in cases:
        if not (SNAPSHOTS / c / 'manifest.json').exists():
            raise SystemExit(f'冻结快照缺失: {c}')
    case_inputs = {c: adapter.generation_input(c) for c in cases}
    preflight(bundle, config, task, case_inputs, graph_builder=graph_builder)
    rows = []
    for c in cases:
        row = await evaluate_conversation(c, adapter, task, bundle, config, root,
                                          graph_builder=graph_builder)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True
              )
    baseline_rows = None
    compatibility = {}
    delta_valid = None
    if args.baseline:
        bp = Path(args.baseline) / 'report.json'
        if not bp.exists():
            compatibility['baseline'] = 'missing: report.json 不存在，无法比对'
            delta_valid = False
        else:
            base_report = json.loads(bp.read_text())
            baseline_rows = base_report['per_conversation']
            mine = experiment_identity(bundle, config, connection(root), cases,
                                       graph_builder=graph_builder)
            compatibility['baseline'] = baseline_compatibility(mine, base_report.get('identity') or {})
            delta_valid = compatibility['baseline'] == 'compatible'
    report = aggregate_report(rows, baseline_rows)
    report['identity'] = experiment_identity(bundle, config, connection(root), cases,
                                             graph_builder=graph_builder)
    report['baseline_compatibility'] = compatibility
    if delta_valid is not None:
        # 不兼容时差值保留作参考但标记无效，不得当作有效实验提升（评审五）
        report['delta_valid'] = delta_valid
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
    p.add_argument('--graph-rebuild', action='store_true',
                   help='新图重建模式：图按锁定 S 从固定事实重建（记忆/向量仍冻结）；'
                        '默认关闭＝冻结图。身份加记投影规则/S/实际重建图摘要。')
    asyncio.run(main(p.parse_args()))
