"""checks 槽位的可执行校验器（C 层，框架级，数据集无关）。

设计契约：**不变量逻辑属于框架，字段访问属于适配器。**
适配器提供 GraphAccessors（如何从该数据集的图/事实/答案表示里取
节点名、别名、说话人、事实主体/出处、答案拒答位等），本模块用访问器
执行通用断言。禁止在本文件出现任何数据集字段名。

性质：只读校验，不修改产物；违反即报告（收口指标的系统侧证据）。
与管线内部硬门互补：那侧"写入前掐掉"，本侧"写出后可审计"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class GraphAccessors:
    """数据集适配器需提供的字段访问器（全部为纯函数）。"""
    node_name: Callable[[dict], str]
    node_aliases: Callable[[dict], list[str]]
    speakers: Callable[[dict], set[str]]                  # graph -> 全体说话人名
    fact_id: Callable[[dict], str]
    fact_subject: Callable[[dict], str]
    fact_source: Callable[[dict], str]
    answer_idx: Callable[[dict], str]
    answer_refused: Callable[[dict], bool]
    answer_text: Callable[[dict], str]
    answer_evidence: Callable[[dict], list[str]]
    answer_status_ok: Callable[[dict], bool]


@dataclass
class Violation:
    check: str
    subject: str
    detail: str = ""


@dataclass
class Check:
    id: str
    statement: str
    verify: object    # callable(graph, answers, facts, acc: GraphAccessors) -> list[Violation]


CHECKS: list[Check] = []


def register(cid: str, statement: str):
    def deco(fn):
        CHECKS.append(Check(id=cid, statement=statement, verify=fn))
        return fn
    return deco


# ---------------------------------------------------------------- 图级

@register("alias-not-speaker", "别名不得等于任何说话人名或其他实体的规范名")
def _alias_not_speaker(graph, answers, facts, acc):
    speakers = acc.speakers(graph)
    names = {acc.node_name(n) for n in graph.get("nodes", [])}
    out = []
    for n in graph.get("nodes", []):
        nm = acc.node_name(n)
        for a in acc.node_aliases(n) or []:
            if (a in speakers or (a in names and a != nm)) and a:
                out.append(Violation("alias-not-speaker", nm, f"别名'{a}'越界"))
    return out


@register("fact-has-source", "每条事实必须带出处")
def _fact_has_source(graph, answers, facts, acc):
    return [Violation("fact-has-source", acc.fact_id(f), "无出处")
            for f in facts if not acc.fact_source(f)]


# ---------------------------------------------------------------- 答案级

@register("answer-subject-consistency", "非拒答答案须能从其证据解析出主体（零主体=硬违规）")
def _answer_subject(graph, answers, facts, acc):
    by_id = {acc.fact_id(f): f for f in facts}
    out = []
    for a in answers:
        if acc.answer_refused(a) or not acc.answer_status_ok(a):
            continue
        evs = acc.answer_evidence(a) or []
        subjects = {acc.fact_subject(by_id[e]) for e in evs if e in by_id} - {""}
        if evs and not subjects:
            out.append(Violation("answer-subject-consistency", acc.answer_idx(a),
                                 f"有证据但零主体: {evs[:3]}"))
    return out


@register("refusal-cleanliness", "拒答必须是干净固定句，不得附带猜测或他人事实")
def _refusal_clean(graph, answers, facts, acc):
    out = []
    for a in answers:
        if not acc.answer_refused(a):
            continue
        txt = acc.answer_text(a).strip()
        if txt and txt != "对话中未提及该信息":
            out.append(Violation("refusal-cleanliness", acc.answer_idx(a), f"拒答不干净: {txt[:60]}"))
    return out


def run_all(graph_path: Path, answers_path: Path, facts_path: Path | None,
            acc: GraphAccessors) -> dict:
    """对一组产物跑全部检查（框架入口；acc 由数据集适配器提供）。"""
    graph = json.loads(graph_path.read_text())
    answers = ([json.loads(l) for l in answers_path.read_text().splitlines() if l.strip()]
               if answers_path.exists() else [])
    facts = ([json.loads(l) for l in facts_path.read_text().splitlines() if l.strip()]
             if facts_path and facts_path.exists() else [])
    report = {}
    for c in CHECKS:
        try:
            report[c.id] = [v.__dict__ for v in c.verify(graph, answers, facts, acc)]
        except Exception as e:  # noqa: BLE001
            report[c.id] = [{"check": c.id, "subject": "ERROR", "detail": repr(e)[:160]}]
    return report
