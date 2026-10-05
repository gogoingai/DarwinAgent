"""Bounded bootstrap -> proposal -> validation -> full evaluation -> adoption controller.

Multi-case training: every case of the split runs fully on the same candidate bundle each
round; scores aggregate by the frozen sum rule. Feedback for proposals is dataset-generic:
diagnostic rows travel as the evaluator produced them (opt-out flag `passed: true`),
bounded by a size budget, and only training feedback ever reaches a proposal."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from collections.abc import Mapping

from oak.contracts import EvaluationResult, plain
from oak.agents.protocol import ProtocolError
from oak.engine.pipeline import Pipeline
from oak.kernel import KernelBundle
from oak.kernel.revision import AssetRevisionService, training_id
from oak.kernel.validation import capability_names
from oak.llm.client import LLMClient
from oak.runtime.artifacts import atomic_json, digest
from oak.runtime.identity import assert_files, snapshot_files, transport_identity
from .bootstrap import AssetBootstrapper
from .proposal import ProposalGenerator
from .spec import aggregate_scores

FEEDBACK_BUDGET_CHARS = 35000
_DIAG_ROW_CHARS = 2200        # 未识别结构的诊断行截断上限（locomo 判分行会被结构化压缩）
_TRACE_CHARS = 600            # 单题检索轨迹序列化上限


def _clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:max(0, limit - 1)] + '…'


def _diagnostic_failure(row):
    """失败优先：answered 且判分 precise 通过（或显式 passed）才算成功，其余进反馈。"""
    if not isinstance(row, dict):
        return True
    if row.get('passed') is True:
        return False
    if row.get('status') == 'answered':
        original = row.get('original') if isinstance(row.get('original'), dict) else {}
        if original.get('precise') is True:
            return False
    return True


def _compact_diagnostic(row):
    """压缩判分原始输出：只保留归因所需字段；金标（reference）与判题内部结构不进提案载荷。"""
    original = row.get('original') if isinstance(row, dict) and isinstance(row.get('original'), dict) else None
    if original is None:
        blob = json.dumps(row, ensure_ascii=False, default=str)
        if len(blob) <= _DIAG_ROW_CHARS:
            return row
        return {'_row_truncated': blob[:_DIAG_ROW_CHARS]}
    keep = {'question_id': row.get('question_id'), 'question': _clip(row.get('question'), 120),
            'status': row.get('status'), 'precise': original.get('precise'),
            'lenient': original.get('lenient')}
    if row.get('answer'):
        keep['answer'] = _clip(row.get('answer'), 200)
    if row.get('error'):
        keep['error'] = _clip(row.get('error'), 200)
    for src in ('missing_elements', 'wrong_elements', 'precision_issues'):
        items = original.get(src) or []
        if items:
            keep[src] = [_clip(i, 60) for i in items[:5]]
    return keep


def _retrieval_trace(answer):
    """单题执行轨迹摘要（评审②）：从结构化 AnswerResult.trace 提取。参数优先取工具事件里
    执行点记录的 parameters（协议重试中被拒动作不会错配）；旧记录回退按 asset_id 顺序配对
    成功调用。含证据摘录与来源标识、空结果、截断标记（tools_truncated）、拒绝理由。"""
    events = plain([ev for ev in (getattr(answer, 'trace', ()) or ())])  # mappingproxy 全解包
    raw_outputs = getattr(answer, 'raw_outputs', ()) or ()
    fallback_calls = []
    has_ready = False
    for raw in raw_outputs:
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get('action') == 'call':
            fallback_calls.append(obj)
        elif isinstance(obj, dict) and obj.get('action') == 'ready':
            has_ready = True
    fb_index = 0
    tools = []
    returned_rows = 0
    empty_results = 0
    seen_ids = set()
    tool_keys = set()
    for ev in events:
        stage = ev.get('stage')
        if stage == 'retrieval':
            rows = ev.get('rows', 0)
            tools.append({'tool': 'vector_once', 'rows': rows, 'k': ev.get('k')})
            returned_rows += rows
            if not rows:
                empty_results += 1
            continue
        if stage != 'tool':
            continue
        params = ev.get('parameters')
        if params is None:
            while fb_index < len(fallback_calls):
                action = fallback_calls[fb_index]; fb_index += 1
                if action.get('asset_id') == ev.get('asset_id'):
                    params = action.get('parameters')
                    break
        # 工具返回类型分流（评审四）：数组取证据行；对象/数值/字符串/布尔是框架允许的
        # 合法返回，保留有界摘要且不算空结果；None 才是空。摘要永不因合法返回类型而失败。
        data = ev.get('data')
        entry = {'tool': ev.get('asset_id'),
                 'capabilities': ev.get('capability_calls') or None}
        if isinstance(data, list):
            rows = len(data)
            if not rows:
                empty_results += 1
            excerpts = []
            for row in data[:2]:
                if not isinstance(row, Mapping):
                    excerpts.append({'value': _clip(row, 60)})
                    continue
                excerpts.append({'node_id': row.get('node_id'),
                                 'statement': _clip(row.get('陈述') or row.get('statement') or row.get('名称'), 60),
                                 'source_ids': list(row.get('source_ids') or ())[:2]})
            if excerpts:
                entry['evidence'] = excerpts
        else:
            rows = 1 if data is not None else 0
            if data is None:
                empty_results += 1
            else:
                entry['returns'] = _clip(data, 90)   # 合法标量/对象返回（0/False 非空）
        entry['rows'] = rows
        returned_rows += rows
        if params is not None:
            entry['params'] = _clip(params, 90)
        # 图新增遥测（专家规格#2，反馈层事后差分——不触碰作答路径）：本调用新召回的
        # node_id（对前一调用集合的差集）与重复调用标记（同工具同参数再现）。
        known=set(seen_ids)
        new_ids=[nid for nid in (ev.get('node_ids') or ()) if nid not in known]
        seen_ids.update(ev.get('node_ids') or ())
        entry['new_node_ids']=new_ids[:8]
        call_key=(ev.get('asset_id'), json.dumps(params,sort_keys=True,ensure_ascii=False) if isinstance(params,Mapping) else None)
        entry['repeat_call']= call_key in tool_keys
        tool_keys.add(call_key)
        tools.append(entry)
    rejections = []
    tool_errors=[]
    for ev in events:
        stage = ev.get('stage')
        if stage=='tool_error':
            tool_errors.append({'tool':ev.get('asset_id'),
                                'input_ref':ev.get('input_ref'),
                                'error_type':ev.get('error_type'),
                                'error':_clip(ev.get('error'),150),
                                'observation':ev.get('observation')})
        if stage == 'review' and not ev.get('accepted'):
            rejections.append({'by': 'review', 'reason': _clip(ev.get('feedback') or ev.get('reason'), 150)})
        elif stage == 'candidate':
            for check in (ev.get('checks') or ())[:6]:
                if isinstance(check, Mapping) and not check.get('ok'):
                    rejections.append({'by': 'check', 'check_id': check.get('check_id'),
                                       'issues': _clip(check.get('issues'), 120)})
    # 停止信号＝工具循环的收尾方式：ready 动作 vs 步数耗尽；执行错误覆盖之
    stopped = 'ready' if has_ready else 'budget'
    if any(ev.get('stage') == 'execution_error' for ev in events):
        stopped = 'execution_error'
    trace = {'question_id': answer.question_id, 'status': answer.status,
             'tool_calls': len(tools), 'model_calls': len(raw_outputs),
             'returned_rows': returned_rows, 'empty_results': empty_results,
             'stopped': stopped, 'tools': tools, 'tool_errors':tool_errors[:2],
             'rejections': rejections}
    if getattr(answer, 'error', None):
        trace['error'] = _clip(answer.error, 150)
    truncated = 0
    while len(tools) > 2 and len(json.dumps(trace, ensure_ascii=False, default=str)) > _TRACE_CHARS:
        tools.pop(); truncated += 1
    if truncated:
        trace['tools_truncated'] = truncated
    return plain(trace)


def pipeline_active_stages(snapshot_root):
    """资产职责图（专家规格#5）：让提案器明确知道每个资产在当前配置下的实际执行情况——
    冻结快照下 P.extract 根本不执行、S 只作用于查询词表层（不重建图）；修改它们不会
    改变本轮计分路径，不得把这类改动算作答题收益。"""
    if snapshot_root is not None:
        return {'P.extract': 'SKIPPED——记忆由冻结快照供给，改它不进本轮计分路径',
                'P.tools': '执行中（检索决策）', 'P.answer': '执行中（作答）',
                'P.review': '执行中（审查）',
                'S': '仅查询词表层——冻结图不因 S 补丁重建，新类型在图中无数据',
                'F': '执行中（检索函数）', 'C': '执行中（结构检查，电池准入）'}
    return {'P.extract': '执行中（语料抽取）', 'P.tools': '执行中', 'P.answer': '执行中',
            'P.review': '执行中', 'S': '全量生效（驱动抽取）', 'F': '执行中', 'C': '执行中'}


def training_feedback(cases, results, case_diagnostics, baseline, active_stages=None,
                      previous_round=None):
    """Failure-first proposal feedback: compressed diagnostics (gold references never enter
    the payload) plus a per-question execution trace. One character budget bounds the COMPLETE
    serialized payload. With several training cases the budget rotates case by case — an early
    case may not crowd the others out. Answer association is keyed per case (评审#3):
    same-named question ids in different cases never share a trace."""
    per_case_rows = []
    rows_total = 0
    for (case_id, diagnostics), result in zip(case_diagnostics, results):
        answers = {a.question_id: a for a in result.answers}  # 会话内索引：同名题号跨对话不串用
        rows = []
        for row in plain(diagnostics):
            if not _diagnostic_failure(row):
                continue
            rows_total += 1
            unit = {'case_id': case_id, 'diagnostic': _compact_diagnostic(row)}
            answer = answers.get(row.get('question_id') if isinstance(row, dict) else None)
            if answer is not None:
                unit['trace'] = _retrieval_trace(answer)
            rows.append(unit)
        per_case_rows.append(rows)
    failures = []
    for case, result in zip(cases, results):
        failures += [{'case_id': case.id, 'question_id': a.question_id, 'error': a.error}
                     for a in result.answers if a.status == 'execution_error']
    graph_rows = []
    for result in results:
        graph_rows += list(plain(result.graph_diagnostics))
    score_data = baseline.to_dict()
    score_data.pop('diagnostics', None)  # 诊断单独装订，载荷不重复计费

    def payload(case_counts, fail_count, graph_count):
        diagnostics = [row for rows, take in zip(per_case_rows, case_counts) for row in rows[:take]]
        return {'scores': score_data,
                'pipeline_active_stages': active_stages,
                'previous_round': previous_round,
                'diagnostics': diagnostics,
                'diagnostic_rows_total': rows_total,
                'diagnostic_rows_in_proposal': len(diagnostics),
                'generation_failures': failures[:fail_count],
                'generation_failures_total': len(failures),
                'generation_failures_truncated': fail_count != len(failures),
                'graph_diagnostics': graph_rows[:graph_count],
                'feedback_budget_chars': FEEDBACK_BUDGET_CHARS}

    def fits(case_counts, fail_count, graph_count):
        return len(json.dumps(payload(case_counts, fail_count, graph_count),
                              ensure_ascii=False, default=str)) <= FEEDBACK_BUDGET_CHARS

    zero_counts = (0,) * len(per_case_rows)
    skeleton = len(json.dumps(payload(zero_counts, 0, 0), ensure_ascii=False, default=str))
    if skeleton > FEEDBACK_BUDGET_CHARS:
        raise ValueError(f'反馈骨架（scores+统计字段）序列化后 {skeleton} 字符，超过预算 '
                         f'{FEEDBACK_BUDGET_CHARS}：评分载荷本身超限，拒绝生成提案')
    # 轮转准入：每步从已入载行数最少的对话取一行——多对话均分预算，谁也不能先占满。
    case_counts = [0] * len(per_case_rows)
    progress = True
    while progress:
        progress = False
        for i in sorted(range(len(per_case_rows)), key=lambda idx: case_counts[idx]):
            if case_counts[i] >= len(per_case_rows[i]):
                continue
            trial = list(case_counts); trial[i] += 1
            if fits(tuple(trial), 0, 0):
                case_counts = trial; progress = True
                break
    fail_count = 0
    while fail_count < len(failures) and fits(tuple(case_counts), fail_count + 1, 0):
        fail_count += 1
    graph_count = 0
    while graph_count < len(graph_rows) and fits(tuple(case_counts), fail_count, graph_count + 1):
        graph_count += 1
    return payload(tuple(case_counts), fail_count, graph_count)


def question_identity(case):
    """Composite training identity: same-named questions in different cases stay distinct,
    and '::' inside either id cannot create collisions (length-prefixed encoding)."""
    return [training_id(case.id, q.id) for q in case.questions]


def _per_case_feedback_facts(root, name, cases):
    """Per-case diagnostics from the stage's evaluation checkpoints: the aggregated baseline
    loses case attribution, the per-case files keep it."""
    rows = []
    for case in cases:
        path = Path(root) / name / 'evaluation' / f'{case.id}.json'
        diagnostics = ()
        if path.exists():
            diagnostics = plain(json.loads(path.read_text())['scores'].get('diagnostics', ()))
        rows.append((case.id, diagnostics))
    return rows


def stress_trial_samples(base_inputs, graph):
    """F 单元测试——穷尽版（用户标准：单测应避免所有故障；历史回归驱动）。
    触发面穷尽：A 行集形态（空/签名穷尽/全量/缺字段）＋B 标量边界（全空最宽＋图内真值）。
    历史回归两大根因修复：①真实 trial_inputs 是冻结映射与元组——按 Mapping 判定并
    plain() 解包，否则样本恒空（电池空转事故）；②自扫描型 F 不收 rows——必须扫标量
    宽值（subject='' → 内部扫描命中全图 → 预算爆/列表字段流过 str() 当场触发）。"""
    from collections.abc import Mapping
    from oak.contracts import plain
    from oak.operators.data import DataCapabilities
    rows = list(DataCapabilities(graph).rows.values())

    def signature(row):
        return tuple(sorted((k, type(v).__name__) for k, v in row.items() if k != 'node_id'))

    shape_rows = []
    seen_sig = set()
    for r in rows:
        sig = signature(r)
        if sig not in seen_sig:
            seen_sig.add(sig); shape_rows.append(r)

    real_scalars = {}
    for field in ('主体', '类型', '主题', '编号'):
        vals = [str(r[field]) for r in rows[:120]
                if isinstance(r.get(field), str) and r[field]]
        if vals:
            real_scalars[field] = list(dict.fromkeys(vals))[:4]

    out = []
    for raw_base in base_inputs[:3]:
        if not isinstance(raw_base, Mapping):
            continue
        base = plain(raw_base)
        takes_rows = isinstance(base.get('rows'), (list, tuple))
        if takes_rows:
            out.append({**base, 'rows': []})
            out.append({**base, 'rows': [dict(r) for r in shape_rows]})
            out.append({**base, 'rows': [dict(r) for r in rows]})
            # 中等规模（历史回归：全量先撞 traverse 100 节点上限报 Invalid traversal，
            # 运行期预算爆真实发生在 30-100 节点档——两档都要扫）
            out.append({**base, 'rows': [dict(r) for r in rows[:60]]})
            out.append({**base, 'rows': [dict(r) for r in rows[:100]]})
            stripped = [{k: x for k, x in r.items()
                         if k not in ('日期', '主题', '类型')} for r in shape_rows]
            out.append({**base, 'rows': stripped})
            out.append({**base, 'rows': [dict(r) for r in rows],
                        **{k: '' for k, v in base.items()
                           if isinstance(v, str) and k != 'rows'}})
        else:
            out.append({k: ('' if isinstance(v, str) else v)
                        for k, v in base.items()})
        for field, vals in real_scalars.items():
            for v in vals[:2]:
                variant = {k: (v if k == field else x) for k, x in base.items()
                           if k != 'rows'}
                if takes_rows:
                    variant['rows'] = [dict(r) for r in shape_rows]
                out.append(variant)
    return out


def pipeline_active_stages(snapshot_root):
    """资产职责图（专家规格#5）：让提案器明确知道每个资产在当前配置下的实际执行情况——
    冻结快照下 P.extract 根本不执行、S 只作用于查询词表层（不重建图）；修改它们不会
    改变本轮计分路径，不得把这类改动算作答题收益。"""
    if snapshot_root is not None:
        return {'P.extract': 'SKIPPED——记忆由冻结快照供给，改它不进本轮计分路径',
                'P.tools': '执行中（检索决策）', 'P.answer': '执行中（作答）',
                'P.review': '执行中（审查）',
                'S': '仅查询词表层——冻结图不因 S 补丁重建，新类型在图中无数据',
                'F': '执行中（检索函数）', 'C': '执行中（结构检查，电池准入）'}
    return {'P.extract': '执行中（语料抽取）', 'P.tools': '执行中', 'P.answer': '执行中',
            'P.review': '执行中', 'S': '全量生效（驱动抽取）', 'F': '执行中', 'C': '执行中'}


def training_feedback(cases, results, case_diagnostics, baseline, active_stages=None,
                      previous_round=None):
    """Failure-first proposal feedback: compressed diagnostics (gold references never enter
    the payload) plus a per-question execution trace. One character budget bounds the COMPLETE
    serialized payload. With several training cases the budget rotates case by case — an early
    case may not crowd the others out. Answer association is keyed per case (评审#3):
    same-named question ids in different cases never share a trace."""
    per_case_rows = []
    rows_total = 0
    for (case_id, diagnostics), result in zip(case_diagnostics, results):
        answers = {a.question_id: a for a in result.answers}  # 会话内索引：同名题号跨对话不串用
        rows = []
        for row in plain(diagnostics):
            if not _diagnostic_failure(row):
                continue
            rows_total += 1
            unit = {'case_id': case_id, 'diagnostic': _compact_diagnostic(row)}
            answer = answers.get(row.get('question_id') if isinstance(row, dict) else None)
            if answer is not None:
                unit['trace'] = _retrieval_trace(answer)
            rows.append(unit)
        per_case_rows.append(rows)
    failures = []
    for case, result in zip(cases, results):
        failures += [{'case_id': case.id, 'question_id': a.question_id, 'error': a.error}
                     for a in result.answers if a.status == 'execution_error']
    graph_rows = []
    for result in results:
        graph_rows += list(plain(result.graph_diagnostics))
    score_data = baseline.to_dict()
    score_data.pop('diagnostics', None)  # 诊断单独装订，载荷不重复计费

    def payload(case_counts, fail_count, graph_count):
        diagnostics = [row for rows, take in zip(per_case_rows, case_counts) for row in rows[:take]]
        return {'scores': score_data,
                'pipeline_active_stages': active_stages,
                'previous_round': previous_round,
                'diagnostics': diagnostics,
                'diagnostic_rows_total': rows_total,
                'diagnostic_rows_in_proposal': len(diagnostics),
                'generation_failures': failures[:fail_count],
                'generation_failures_total': len(failures),
                'generation_failures_truncated': fail_count != len(failures),
                'graph_diagnostics': graph_rows[:graph_count],
                'feedback_budget_chars': FEEDBACK_BUDGET_CHARS}

    def fits(case_counts, fail_count, graph_count):
        return len(json.dumps(payload(case_counts, fail_count, graph_count),
                              ensure_ascii=False, default=str)) <= FEEDBACK_BUDGET_CHARS

    zero_counts = (0,) * len(per_case_rows)
    skeleton = len(json.dumps(payload(zero_counts, 0, 0), ensure_ascii=False, default=str))
    if skeleton > FEEDBACK_BUDGET_CHARS:
        raise ValueError(f'反馈骨架（scores+统计字段）序列化后 {skeleton} 字符，超过预算 '
                         f'{FEEDBACK_BUDGET_CHARS}：评分载荷本身超限，拒绝生成提案')
    # 轮转准入：每步从已入载行数最少的对话取一行——多对话均分预算，谁也不能先占满。
    case_counts = [0] * len(per_case_rows)
    progress = True
    while progress:
        progress = False
        for i in sorted(range(len(per_case_rows)), key=lambda idx: case_counts[idx]):
            if case_counts[i] >= len(per_case_rows[i]):
                continue
            trial = list(case_counts); trial[i] += 1
            if fits(tuple(trial), 0, 0):
                case_counts = trial; progress = True
                break
    fail_count = 0
    while fail_count < len(failures) and fits(tuple(case_counts), fail_count + 1, 0):
        fail_count += 1
    graph_count = 0
    while graph_count < len(graph_rows) and fits(tuple(case_counts), fail_count, graph_count + 1):
        graph_count += 1
    return payload(tuple(case_counts), fail_count, graph_count)


def question_identity(case):
    """Composite training identity: same-named questions in different cases stay distinct,
    and '::' inside either id cannot create collisions (length-prefixed encoding)."""
    return [training_id(case.id, q.id) for q in case.questions]


async def batched_fault_retry(pipeline, case, spec, config, answers_dir, faulted,
                              sleep=asyncio.sleep, batch_size=25, lead_s=150.0, gap_s=60.0):
    """One bounded retry pass for faulted questions: wait out the transient-burst window,
    then delete their answer checkpoints in small batches and rerun the case (healthy
    questions checkpoint-reuse at zero cost). The final fault set is recomputed from the
    LAST complete answer set — never a union of per-batch snapshots: batches not yet retried
    still carry their stale fault checkpoints, and a union would preserve those pre-retry
    states as phantom faults (review #4, offline-reproduced)."""
    await sleep(lead_s)
    from oak.runtime.artifacts import digest as _digest
    result = None
    for start in range(0, len(faulted), batch_size):
        for a in faulted[start:start + batch_size]:
            (Path(answers_dir) / f'{_digest(a.question_id)}.json').unlink(missing_ok=True)
        result = await pipeline.run(case, spec, config)
        if start + batch_size < len(faulted):
            await sleep(gap_s)
    still_faulted = sorted(a.question_id for a in result.answers if a.status == 'execution_error')
    return result, still_faulted




def _per_case_feedback_facts(root, name, cases):
    """Per-case diagnostics from the stage's evaluation checkpoints: the aggregated baseline
    loses case attribution, the per-case files keep it."""
    rows = []
    for case in cases:
        path = Path(root) / name / 'evaluation' / f'{case.id}.json'
        diagnostics = ()
        if path.exists():
            diagnostics = plain(json.loads(path.read_text())['scores'].get('diagnostics', ()))
        rows.append((case.id, diagnostics))
    return rows

_DETERMINISTIC_ERRORS=frozenset({'SandboxError','ValueError','TypeError','KeyError'})
# 服务瞬时族（529/连接/超时）在题级可重试；ProtocolError 仍不可——协议耗尽喂资产反馈，
# 且 529 进传输层退避重试后，拥塞型协议饥饿自然消失（B0/R1 服务拥塞故障，2026-10-05）。
_TRANSIENT_ERRORS=frozenset({'InternalServerError','APIStatusError','APIConnectionError',
                             'APITimeoutError','RateLimitError'})


def _retryable_answer(answer):
    if any(ev.get('stage')=='tool_error' for ev in plain(answer.trace or ())):
        return False
    kind=str(answer.error).split(':',1)[0].strip()
    if kind in _DETERMINISTIC_ERRORS:
        return False
    return (kind in _TRANSIENT_ERRORS
            or str(answer.error).startswith(('TransportExhausted:', 'EmptyCompletion:')))


def _retry_journal(path, identity):
    if path.exists():
        journal=json.loads(path.read_text())
        if journal['identity']!=identity:
            raise ValueError('Retry budget belongs to a different run identity')
        return journal
    return {'identity':identity,'questions':{}}


def _settle_reservations(path, result, consumed):
    if not path.exists():
        return
    journal=_retry_journal(path,result.identity)
    for answer in result.answers:
        previous=journal['questions'].get(answer.question_id)
        if previous is not None and previous['state']=='reserved':
            previous.update(state='done',consumed_after=consumed,
                            recovered_from_reservation=True,
                            final_error_type=str(answer.error).split(':',1)[0]
                            if answer.status=='execution_error' else None)
    atomic_json(path,journal)


def _failed_tool_params(results):
    """Only training failures, with the original executed action when recorded."""
    entries=[]
    for result in results or ():
        for answer in result.answers:
            if answer.status!='execution_error':
                continue
            for event in plain(answer.trace or ()):
                if event.get('stage')=='tool_error':
                    entries.append((result.case_id,event['asset_id'],event['parameters']))
            # Older checkpoints lack tool_error; the final action is usable only
            # when its asset matches a deterministic tool failure.
            if not any(e.get('stage')=='tool_error' for e in plain(answer.trace or ())) \
                    and str(answer.error).startswith(('SandboxError:', 'ValueError:')):
                for raw in reversed(answer.raw_outputs or ()):
                    try:
                        action=json.loads(raw)
                    except (ValueError,TypeError):
                        continue
                    if isinstance(action,dict) and action.get('action')=='call':
                        entries.append((result.case_id,action.get('asset_id'),
                                        action.get('parameters')))
                        break
    return entries


def _prior_failed_tool_params(root):
    """Replay saved failures from earlier stages, including rejected candidates."""
    entries=[]
    seen=set()
    for path in sorted(Path(root).glob('*/generation/*/answers/*.json')):
        answer=json.loads(path.read_text()).get('result',{})
        if answer.get('status')!='execution_error':
            continue
        case_id=path.parent.parent.name
        actions=[(event.get('asset_id'),event.get('parameters'))
                 for event in answer.get('trace',())
                 if event.get('stage')=='tool_error']
        if not actions and str(answer.get('error','')).startswith(
                ('SandboxError:', 'ValueError:')):
            for raw in reversed(answer.get('raw_outputs',())):
                try:
                    action=json.loads(raw)
                except (TypeError,ValueError):
                    continue
                if isinstance(action,dict) and action.get('action')=='call':
                    actions=[(action.get('asset_id'),action.get('parameters'))]
                    break
        for aid,params in actions:
            if aid is None or params is None:
                continue
            key=(case_id,aid,digest(plain(params)))
            if key not in seen:
                seen.add(key)
                entries.append((case_id,aid,params))
    return entries


def stability_metrics(root):
    """Summarize only persisted attempts and evaluated stages from this run."""
    root=Path(root)
    paths=([root/'B0'/'admission.json']
           +list(root.glob('R*/.candidate-attempt-*/admission.json'))
           +list(root.glob('R*/candidate/admission.json')))
    reports=[json.loads(p.read_text()) for p in paths if p.exists()]
    stages=[json.loads(p.read_text()) for p in root.glob('*/stage.json')]
    retry_rows=[]
    for path in root.glob('*/fault-retry/*.json'):
        retry_rows.extend(json.loads(path.read_text()).get('questions',{}).values())
    count=len(reports)
    passed=sum(r.get('verdict')=='passed' for r in reports)
    smoke_passed=sum(r.get('verdict')=='passed' and
                     r.get('smoke',{}).get('status')=='passed' for r in reports)
    return {'admission_submitted':count,'admission_passed':passed,
            'admission_pass_rate':passed/count if count else None,
            'smoke_passed':smoke_passed,
            'admission_elapsed_s':round(sum(r.get('elapsed_s',0) for r in reports),3),
            'smoke_elapsed_s':round(sum(r.get('smoke',{}).get('elapsed_s',0)
                                         for r in reports),3),
            'formal_execution_faults':sum(
                s.get('scores',{}).get('generation_faults',0) or 0 for s in stages),
            'formal_elapsed_s':round(sum(s.get('elapsed_s',0) for s in stages),3),
            'retry_elapsed_s':round(sum(
                sum(r.get('elapsed_s',0) for r in s.get('fault_retries',{}).values())
                for s in stages),3),
            'retry_reserved':len(retry_rows),
            'retry_same_class_failures':sum(
                x.get('final_error_type')==x.get('initial_error_type')
                for x in retry_rows if x.get('initial_error_type')
                and 'final_error_type' in x)}


ADMISSION_ATTEMPTS=50


class ExperimentRunner:
    def __init__(self,adapter,evaluator_factory,connection_config,run_config,policy,work_dir,
                 frozen_files=(),client_factory=None,bootstrap_context=None,snapshot_root=None,
                 bootstrap_trial_graph=None,smoke_judge=None):
        self.adapter,self.evaluator_factory=adapter,evaluator_factory
        self.connection_config,self.config,self.policy=connection_config,run_config,policy
        self.root=Path(work_dir)
        self.frozen=snapshot_files([Path(__file__).resolve().parents[1],*frozen_files])
        self.revisions=AssetRevisionService()
        self._injected_client=client_factory
        # bootstrap_context: 冻结快照结构样本（无标签），随冷启动 bootstrap 载荷进提示词。
        self.bootstrap_context=bootstrap_context
        # snapshot_root: 每对话冻结记忆快照目录（<case_id>/ 子目录）；注入时臂间共享同一记忆面。
        self.snapshot_root=Path(snapshot_root) if snapshot_root is not None else None
        # bootstrap_trial_graph: 冷启动 bootstrap 反馈环内的真图试跑（冻结快照图）。
        self.bootstrap_trial_graph=bootstrap_trial_graph
        # smoke_judge: 任务层注入的子集判题器（冻结判题原语；框架不依赖任务模块）。
        self.smoke_judge=smoke_judge
        self._fault_retried=set()

    def _stage_health(self):
        """Stage-level execution faults from the on-disk stage records. A candidate rejected
        after complete scoring is a normal outcome; a stage that could not finish scoring is
        a fault and must surface in the run status."""
        health={}
        for path in sorted(self.root.glob('*/stage.json')):
            row=json.loads(path.read_text());scores=row.get('scores',{})
            faults={'status':row.get('status'),'completed':scores.get('completed'),
                    'total':scores.get('total'),'generation_faults':scores.get('generation_faults'),
                    'evaluation_faults':scores.get('evaluation_faults')}
            if (row.get('status')!='complete' or faults['completed']!=faults['total']
                    or faults['generation_faults'] or faults['evaluation_faults']):
                health[path.parent.name]=faults
        return health

    def _preflight(self,candidate,spec,sample_question=None,cases=None,replay_inputs=()):
        """An identity-bound candidate report, including actual frozen trial inputs."""
        from types import SimpleNamespace
        from .admission import admit_candidate
        if self.snapshot_root is None and self.bootstrap_trial_graph is None:
            # Legacy dynamic-graph runs execute their actual-data trials in Pipeline.run.
            return None
        if cases is None:
            cases=(SimpleNamespace(id='trial',questions=() if sample_question is None
                                    else (sample_question,)),)
        replay_inputs=tuple(replay_inputs)+tuple(_prior_failed_tool_params(self.root))
        report_path=Path(candidate.root).parent/'admission.json'
        required=capability_names(getattr(spec,'retrieval_floor',{}) or {})
        if self.snapshot_root is not None:
            from .admission_worker import run_isolated
            from .snapshots import snapshot_digest
            try:
                snapshot_digests={
                    c.id:snapshot_digest(self.snapshot_root/c.id) for c in cases}
            except (OSError,ValueError) as exc:
                from .admission import AdmissionError
                report={'schema_version':1,'candidate_version':candidate.version,
                        'asset_fingerprints':{
                            a.id:a.fingerprint for a in candidate.assets.assets},
                        'config_digest':digest(self.config.to_dict()),
                        'scenarios':[{'asset_id':'bundle','scenario_id':'training_graph',
                            'status':'incomplete','required':True,'error':str(exc)}],
                        'verdict':'failed'}
                atomic_json(report_path,report)
                raise AdmissionError(report_path,report) from exc
            request={'bundle_path':str(candidate.root),'bundle_version':candidate.version,
                     'snapshot_root':str(self.snapshot_root),
                     'snapshot_digests':snapshot_digests,
                     'asset_fingerprints':{
                         a.id:a.fingerprint for a in candidate.assets.assets},
                     'cases':[c.to_dict() for c in cases],
                     'config':self.config.to_dict(),'required_caps':sorted(required),
                     'report_path':str(report_path),
                     'replay_inputs':plain(replay_inputs)}
            request_path=report_path.with_name('admission-input.json')
            atomic_json(request_path,request)
            if not run_isolated(request_path,report_path,180):
                from .admission import AdmissionError
                raise AdmissionError(report_path,json.loads(report_path.read_text()))
            return json.loads(report_path.read_text())
        graphs={}
        for case in cases:
            graphs[case.id]=self.bootstrap_trial_graph
        return admit_candidate(candidate,cases,graphs,self.config,
            required,report_path,replay_inputs=replay_inputs)

    def _client(self,stage):
        if self._injected_client is not None:
            return self._injected_client(stage)
        cfg=copy.deepcopy(self.connection_config)
        cfg.work_dir=self.root/stage/'runtime'
        return LLMClient(cfg)

    def verify(self):
        assert_files(self.frozen)

    @staticmethod
    def _record_smoke(bundle,error,elapsed_s=0):
        path=Path(bundle.root).parent/'admission.json'
        if not path.exists():
            return
        report=json.loads(path.read_text())
        report['smoke']={'status':'failed' if error else 'passed',
                         'error':error,'elapsed_s':round(elapsed_s,3)}
        if error:
            report['verdict']='failed'
        atomic_json(path,report)

    async def _stage(self,name,cases,spec):
        """Run every case of the split on the same bundle; aggregate by the frozen sum rule.

        Faulted questions get ONE bounded retry pass: their checkpoints are removed and the
        pipeline reruns (healthy answers checkpoint-reuse at zero cost). A question that
        fails twice is a real fault and stays; the retry is recorded in the stage summary."""
        self.verify();started=time.time();stage=self.root/name
        client=self._client(name)
        try:
            results=[];scores=[];identities=[];retries={}
            for case in cases:
                pipeline=Pipeline(client,stage/'generation',
                                  frozen_snapshot=None if self.snapshot_root is None else self.snapshot_root/case.id)
                result=await pipeline.run(case,spec,self.config)
                journal_path=stage/'fault-retry'/f'{case.id}.json'
                _settle_reservations(journal_path,result,client.ledger_summary())
                faulted=[a for a in result.answers if a.status=='execution_error']
                graph_failure=[d for d in (result.graph_diagnostics or ())
                               if isinstance(d,Mapping) and d.get('status')=='execution_error']
                if faulted and graph_failure:
                    # 图阶段全局确定性失败（评审①）：删答案检查点救不回图阶段产物，
                    # 分批等待重试毫无意义——如实记录，不重试。
                    retries[case.id]={'questions':len(faulted),'recovered':0,
                                      'still_faulted':sorted(a.question_id for a in faulted),
                                      'skipped_retry':'graph_stage_failure',
                                      'graph_error':str(graph_failure[0].get('error'))[:200]}
                    print(json.dumps({'stage':name,'case':case.id,'fault_retry':retries[case.id]},ensure_ascii=False),flush=True)
                elif faulted and not any(_retryable_answer(a) for a in faulted):
                    # 确定性工具错误（评审：接口/参数错误重试不会变好）：不整题重检索，
                    # 如实入统计与反馈，由资产修订解决（scope F）。
                    retries[case.id]={'questions':len(faulted),'recovered':0,
                                      'still_faulted':sorted(a.question_id for a in faulted),
                                      'skipped_retry':'deterministic_tool_error',
                                      'sample_errors':[str(a.error)[:150] for a in faulted[:3]]}
                    print(json.dumps({'stage':name,'case':case.id,'fault_retry':retries[case.id]},ensure_ascii=False),flush=True)
                elif faulted:
                    journal=_retry_journal(journal_path,result.identity)
                    eligible=[]
                    for answer in faulted:
                        if not _retryable_answer(answer):
                            continue
                        previous=journal['questions'].get(answer.question_id)
                        if previous is not None:
                            continue
                        journal['questions'][answer.question_id]={
                            'state':'reserved','attempts':1,
                            'initial_digest':digest(answer.to_dict()),
                            'initial_error_type':str(answer.error).split(':',1)[0],
                            'consumed_before':client.ledger_summary()}
                        eligible.append(answer)
                        atomic_json(journal_path,journal)
                    if not eligible:
                        retries[case.id]={'questions':len(faulted),'recovered':0,
                                          'still_faulted':sorted(a.question_id for a in faulted),
                                          'skipped_retry':'already_reserved_or_deterministic'}
                    else:
                    # 先歇再重试：EmptyCompletion 类故障多为瞬时突发，隔窗后分批小跑；
                    # 统计口径见 batched_fault_retry（末份答案集重算，不做批次并集）。
                        retry_started=time.monotonic()
                        result,still_faulted=await batched_fault_retry(
                            pipeline,case,spec,self.config,
                            stage/'generation'/case.id/'answers',eligible)
                        latest={a.question_id:a for a in result.answers}
                        for answer in eligible:
                            journal['questions'][answer.question_id].update(
                                state='done',consumed_after=client.ledger_summary(),
                                final_error_type=str(latest[answer.question_id].error).split(':',1)[0]
                                if latest[answer.question_id].status=='execution_error' else None)
                        atomic_json(journal_path,journal)
                        retries[case.id]={'questions':len(faulted),
                                          'retried':len(eligible),
                                          'recovered':sum(a.question_id not in still_faulted for a in eligible),
                                          'still_faulted':still_faulted,
                                          'elapsed_s':round(time.monotonic()-retry_started,3),
                                          'skipped_deterministic':len(faulted)-len(eligible)}
                    print(json.dumps({'stage':name,'case':case.id,'fault_retry':retries[case.id]},ensure_ascii=False),flush=True)
                scores_path=stage/'evaluation'/f'{case.id}.json'
                if case.id in retries:
                    scores_path.unlink(missing_ok=True)  # 重试跑过＝答案集可能已变：旧评测检查点一律作废重评
                if scores_path.exists():
                    saved=json.loads(scores_path.read_text())
                    if saved['run_identity']!=result.identity or saved['asset_version']!=spec.bundle.version:
                        raise ValueError('Evaluation checkpoint identity mismatch')
                    case_scores=EvaluationResult(**saved['scores'])
                else:
                    evaluator=self.evaluator_factory(client,stage/'evaluation'/case.id)
                    # 完整性按本轮实际出题集核对（训练集瘦身后是前缀子集；全量时等价旧检查）
                    case_scores=await evaluator.evaluate(result,
                        asked=tuple(q.id for q in case.questions))
                    atomic_json(scores_path,{'run_identity':result.identity,'asset_version':spec.bundle.version,
                                             'scores':case_scores.to_dict()})
                results.append(result);scores.append(case_scores);identities.append(result.identity)
            aggregated=aggregate_scores(scores)
            self.verify()
            summary={'stage':name,'cases':[c.id for c in cases],
                     'status':'complete' if aggregated.completed==aggregated.total and not aggregated.evaluation_faults else 'failed',
                     'run_identities':identities,'asset_version':spec.bundle.version,'scores':aggregated.to_dict(),
                     'fault_retries':retries,
                     'calls':client.ledger_summary(),'elapsed_s':round(time.time()-started,2)}
            atomic_json(stage/'stage.json',summary)
            print(json.dumps({k:summary[k] for k in ('stage','status','asset_version','elapsed_s')},ensure_ascii=False),flush=True)
            return results,aggregated
        finally: await client.aclose()

    async def _smoke_gate(self,cases,spec,questions_per_case=6,candidate=False):
        """B0 全量提交前的冒烟门（用户拍板：先保证能答对，再启动跑；v10 追加：6 题对 2）：
        每训练对话抽前 6 题走完整真实管线＋冻结判题（临时目录、真模型、~5 分钟）。
        过门条件＝执行错误 <2/3、判题完整、至少 2/6 precise 答对；不满足分钟级中止换根
        ——确定性全灭（v1/v3/v6 事故类）与「能跑但全答错」的弱冷启动都不再烧全量预算。
        门槛与 B0 冷门成比例：单题低概率故障（如 F 输出契约被个别调用绊倒）由冷门吸收，
        只有系统性破绽（≥2/3）才在此拦下。"""
        import dataclasses as _dc
        import tempfile
        client=self._client('B0-smoke')
        try:
            for case in cases:
                questions=case.questions
                if candidate:
                    risk=('时间|日期|哪天|何时|上周|昨天',
                          '过滤|全部|哪些|多少|类型|主题',
                          '关系|相关|属于|关联|遍历')
                    import re
                    selected=[]
                    for pattern in risk:
                        first=next((q for q in questions if q not in selected
                                    and re.search(pattern,q.text)),None)
                        if first is not None:
                            selected.append(first)
                    selected.extend(q for q in questions if q not in selected)
                    questions=tuple(selected)
                sampled=_dc.replace(case,questions=tuple(questions[:questions_per_case]))
                with tempfile.TemporaryDirectory() as td:
                    pipeline=Pipeline(client,Path(td),
                                      frozen_snapshot=None if self.snapshot_root is None else self.snapshot_root/case.id)
                    result=await pipeline.run(sampled,spec,self.config)
                    faults=[a for a in result.answers if a.status=='execution_error']
                    recoverable=[a for a in faults if _retryable_answer(a)]
                    if candidate and recoverable and len(recoverable)==len(faults):
                        result,_=await batched_fault_retry(
                            pipeline,sampled,spec,self.config,
                            Path(td)/case.id/'answers',recoverable)
                faults=[a for a in result.answers if a.status=='execution_error']
                if candidate and faults:
                    return (f'候选冒烟执行故障 {len(faults)}/{len(result.answers)}: '
                            + str(faults[0].error)[:200])
                if len(faults)*3>=len(result.answers)*2:
                    return f'冒烟执行错误达 {len(faults)}/{len(result.answers)}（≥2/3，系统性破绽）: '+str(faults[0].error)[:200]
                if not any(a.status in ('answered','abstained') for a in result.answers):
                    return '冒烟题无任何有效作答'
                # 冒烟判题（任务层注入的冻结判题原语，训练集金标对机械门合法可见）：
                # 保证能答对，至少 1/3 precise。无注入时退化为「存在有效作答」检查。
                if self.smoke_judge is not None:
                    verdict=await self.smoke_judge(client,case,result.answers)
                    if verdict['completed']<verdict['total']:
                        return f'冒烟判题未完成: {verdict}'
                    if verdict['precise']<2:
                        return f"冒烟 {verdict['total']} 题对 {verdict['precise']}（需≥2）——质量门拒绝"
                elif not any(a.status in ('answered','abstained') for a in result.answers):
                    return '冒烟题无任何有效作答'
            return None
        finally: await client.aclose()

    async def run(self,case_ids,spec,rounds=2,resume=False,stop_file=None,b0_gate=None,stage_gate=None,
                  scope=()):
        """case_ids: one conversation id or a tuple; every case runs fully each round on the
        same candidate bundle. rounds=None iterates until stop_file appears. scope limits
        which asset kinds a round may patch (P first; F/S open by attribution later)."""
        if isinstance(case_ids,str): case_ids=(case_ids,)
        if not case_ids:
            raise ValueError('Training split needs at least one case')
        if rounds is not None and (type(rounds) is not int or rounds<0):
            raise ValueError('Rounds must be a nonnegative integer or None for unbounded iteration')
        self.verify();cases=[self.adapter.generation_input(c) for c in case_ids]
        self.root.mkdir(parents=True,exist_ok=True)
        declaration={'cases':list(case_ids),'case_fingerprint':digest([c.to_dict() for c in cases]),
                     'aggregation':'sum',
                     'task':spec.declaration(),'config':self.config.to_dict(),
                     'connection':transport_identity(type('Connection',(),{'cfg':self.connection_config})()),
                     'policy':asdict(self.policy),'frozen_files':self.frozen,'rounds':rounds,'seed_assets':[],
                     'scope':list(scope or ()),
                     'source_layers':sorted({b.source.kind for case in cases for b in case.corpus}),
                     'snapshots':({c.id:(self.snapshot_root/c.id/'manifest.json').read_text()
                                   for c in cases} if self.snapshot_root is not None else {})}
        declaration=json.loads(json.dumps(declaration,ensure_ascii=False))
        experiment_path=self.root/'experiment.json'
        if experiment_path.exists():
            if not resume or json.loads(experiment_path.read_text())!=declaration:
                raise ValueError('Existing experiment requires explicit resume with exactly the same identity')
        else: atomic_json(experiment_path,declaration)
        try:
            bundle_path=self.root/'B0'/'assets'
            if (bundle_path/'manifest.json').exists(): bundle=KernelBundle(bundle_path)
            else:
                client=self._client('B0')
                try: bundle=await AssetBootstrapper().initialize(cases,spec,client,self.config,bundle_path,
                                                                 structure_sample=self.bootstrap_context,
                                                                 trial_graph=self.bootstrap_trial_graph,
                                                                 snapshot_root=self.snapshot_root)
                finally: await client.aclose()
            if self.snapshot_root is not None or self.bootstrap_trial_graph is not None:
                self._preflight(bundle,spec,cases[0].questions[0],cases)
            if stage_gate is not None: stage_gate('B0')
            if self.snapshot_root is not None:
                smoke_started=time.monotonic()
                smoke_error=await self._smoke_gate(cases,spec.with_bundle(bundle))
                self._record_smoke(bundle,smoke_error,time.monotonic()-smoke_started)
                if smoke_error:
                    summary={'status':'blocked_b0','reason':'smoke gate: 3 题全灭（确定性缺陷）',
                             'smoke_error':smoke_error,'rounds':[],'adopted_version':None}
                    atomic_json(self.root/'summary.json',summary)
                    raise ValueError('冒烟门拒绝（全量提交前 3 题全灭）: '+smoke_error)
            results,baseline=await self._stage('B0',cases,spec.with_bundle(bundle))
            if b0_gate is not None and not b0_gate(baseline):
                summary={'status':'blocked_b0','reason':'baseline gate rejected the B0 evaluation',
                         'baseline':baseline.to_dict(),'rounds':[],'adopted_version':None}
                atomic_json(self.root/'summary.json',summary)
                print(json.dumps({'stage':'B0','status':'blocked_b0'},ensure_ascii=False),flush=True)
                return summary
            adopted=self.revisions.publish(bundle,self.root/'published',{'accepted':True,'reasons':['initial_validated_baseline']})
            # The stage whose evaluation currently backs `baseline`/`results`: proposals must
            # read diagnostics from THERE, never from the round being proposed (it has not
            # run yet). A rejected candidate leaves it unchanged.
            evidence='B0'
            decisions=[];stopped=False
            n=0
            while True:
                # Recorded decisions are always restored first — a STOP signal (or a rounds
                # cap) must never truncate history that already happened.
                next_decision=self.root/f'R{n+1}'/'decision.json'
                if next_decision.exists():
                    n+=1
                    self.verify();name=f'R{n}';stage=self.root/name
                    decision=json.loads(next_decision.read_text());decisions.append(decision)
                    if decision['accepted']:
                        adopted=KernelBundle(stage/'candidate'/'bundle')
                        baseline=EvaluationResult(**decision['candidate'])
                        evidence=name  # 恢复同样以最后采纳版本的评测为准
                        # The publish pointer must follow the restored adoption (B0 was
                        # re-published above during resume), atomically and idempotently.
                        self.revisions.publish(adopted,self.root/'published',decision)
                        # Needed as feedback for the next round even when restored.
                        if stage_gate is not None: stage_gate(name)
                        results,_=await self._stage(name,cases,spec.with_bundle(adopted))
                    continue
                if stop_file is not None and Path(stop_file).exists(): stopped=True; break
                if rounds is not None and n>=rounds: break
                n+=1
                self.verify();name=f'R{n}';stage=self.root/name
                decision_path=stage/'decision.json'
                candidate_path=stage/'candidate'/'bundle'
                if (candidate_path/'manifest.json').exists():
                    candidate=KernelBundle(candidate_path)
                    # 恢复已有候选同样过预检＋冒烟（评审三）：不能仅凭 manifest 存在就跳过验证
                    self._preflight(candidate,spec,cases[0].questions[0],cases,
                                    _failed_tool_params(results))
                    if self.snapshot_root is not None:
                        smoke_started=time.monotonic()
                        resume_smoke=await self._smoke_gate(cases,spec.with_bundle(candidate),candidate=True)
                        self._record_smoke(candidate,resume_smoke,
                                           time.monotonic()-smoke_started)
                        if resume_smoke: raise ValueError('恢复候选冒烟失败: '+resume_smoke)
                else:
                    client=self._client(name)
                    prev_decision=self.root/f'R{n-1}'/'decision.json'
                    previous_round=None
                    if n>0 and prev_decision.exists():
                        pd=json.loads(prev_decision.read_text())
                        previous_round={'round':f'R{n-1}','status':pd.get('status'),
                                        'accepted':pd.get('accepted'),
                                        'reasons':(pd.get('reasons') or [])[:6]}
                    feedback=training_feedback(cases,results,_per_case_feedback_facts(self.root,evidence,cases),baseline,
                                      active_stages=pipeline_active_stages(self.snapshot_root),
                                      previous_round=previous_round)
                    questions=[]
                    for case in cases:
                        questions+=[{'training_id':tid,'text':q.text} for tid,q in zip(question_identity(case),case.questions)]
                    required_caps=capability_names(getattr(spec,'retrieval_floor',{}) or {})
                    try:
                        # 准入类错误（指纹回显/类型/范围/底线/预检否决）回灌提案模型重试，而非
                        # 整轮作废后重复同类错误；用户拍板 2026-10-04：3 次改 10 次（沙箱
                        # 规矩类死因一轮一坑，轮内多试比跨轮便宜）。仍不过才记 validation_failed。
                        # 每次尝试写独立暂存目录，全部预检通过后才确认为正式候选（评审三）——
                        # 否则首败残留的 candidate 目录让后续尝试报 already exists，掩盖真实错误。
                        admission_error=None
                        for attempt in range(ADMISSION_ATTEMPTS):
                            attempt_path=stage/f'.candidate-attempt-{attempt}'
                            if attempt_path.exists(): shutil.rmtree(attempt_path)
                            try:
                                patches=await ProposalGenerator().propose(adopted,cases,feedback,client,self.config,
                                    stage/'proposal-call.json',questions,
                                    allowed_kinds=tuple(scope or ()),admission_error=admission_error)
                                training_ids=[tid for case in cases for tid in question_identity(case)]
                                forbidden=[q.text for case in cases for q in case.questions]
                                # revisions.propose 的 target 是信封目录（内含 bundle/ 与 proposal.json）；
                                # 暂存信封 → 预检 bundle → 全过后信封整体上位为正式 candidate。
                                self.revisions.propose(adopted,patches,attempt_path,training_ids,forbidden,
                                                       allowed_kinds=tuple(scope or ()),
                                                       required_capabilities=required_caps)
                                staged=KernelBundle(attempt_path/'bundle')
                                self._preflight(staged,spec,cases[0].questions[0],cases,
                                                _failed_tool_params(results))
                                if self.snapshot_root is not None:
                                    # 每轮候选同样先冒烟（用户拍板）：坏补丁在 3 题内暴露并
                                    # 回灌重试，不烧 70 分钟全量
                                    smoke_started=time.monotonic()
                                    round_smoke=await self._smoke_gate(cases,spec.with_bundle(staged),candidate=True)
                                    self._record_smoke(staged,round_smoke,
                                                       time.monotonic()-smoke_started)
                                    if round_smoke: raise ValueError(round_smoke)
                                candidate_path.parent.mkdir(parents=True,exist_ok=True)
                                os.replace(attempt_path,candidate_path.parent)
                                candidate=KernelBundle(candidate_path)
                                break
                            except (ValueError, ProtocolError) as exc:
                                # 失败暂存目录保留审计（.candidate-attempt-N），下一尝试用新目录。
                                # ProtocolError（模型输出 JSON 手误）同为可反馈重试类——一次格式错
                                # 不再整轮作废（R11 事故：50 次预算只用了 1 次）。
                                admission_error=f'[重试 {attempt+1}/{ADMISSION_ATTEMPTS}] {type(exc).__name__}: {exc}'
                        else:
                            raise ValueError(admission_error)
                    except Exception as exc:
                        decision={'accepted':False,'status':'validation_failed','reasons':[f'{type(exc).__name__}: {exc}'],
                                  'base_version':adopted.version,'candidate':None}
                        atomic_json(decision_path,decision);decisions.append(decision)
                        print(json.dumps({'stage':name,**decision},ensure_ascii=False),flush=True)
                        continue
                    finally: await client.aclose()
                if stage_gate is not None: stage_gate(name)
                candidate_results,candidate_scores=await self._stage(name,cases,spec.with_bundle(candidate))
                decision={**self.policy.decide(baseline,candidate_scores),'base_version':adopted.version,'candidate_version':candidate.version}
                atomic_json(decision_path,decision);decisions.append(decision)
                if decision['accepted']:
                    adopted=self.revisions.publish(candidate,self.root/'published',decision)
                    baseline=candidate_scores;results=candidate_results
                    evidence=name  # 后续提案的诊断跟随新采纳版本
                print(json.dumps({'stage':name,'accepted':decision['accepted'],'reasons':decision['reasons']},ensure_ascii=False),flush=True)
            self.verify()
            # 汇总训练阶段执行/评测故障：正常评分后的拒绝可完成，评分未完成必须报失败
            unhealthy=self._stage_health()
            summary={'status':'complete' if not unhealthy and all(d.get('status')!='validation_failed' for d in decisions) else 'failed',
                     'unhealthy_stages':unhealthy,
                     'stopped_by_operator':stopped,'rounds':decisions,'adopted_version':adopted.version,
                     'adopted_scores':baseline.to_dict(),
                     'stability':stability_metrics(self.root)}
            atomic_json(self.root/'summary.json',summary)
            return summary
        except Exception as exc:
            atomic_json(self.root/'failure.json',{'status':'failed','error':f'{type(exc).__name__}: {exc}'})
            raise
