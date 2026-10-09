"""两层判题：确定性预检（零成本零方差）→ glm-5.3 盲判 exact/partial/wrong（只记 exact）。

附带官方口径参考指标：字符 bigram F1（非对抗题）。
S4 gold 修复支持：data/gold_repairs.jsonl 存在时判分先套修复表（逐题含判据与引证，
永不隐去），report 同时保留原始 gold 口径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from darwinagent.llm.client import LLMClient

from .config import ns
from .data import CATEGORY_MAP, QA, TOPIC_CATEGORIES
from .dates import answer_equivalent as answer_equivalent
from .dates import cn_num as cn_num
from .dates import extract_digits as extract_digits
from .dates import normalize_answer_text
from .prompts.answer import REFUSAL
from .prompts.judge import CATEGORY_RULES, JUDGE_SYSTEM, JUDGE_TEMPLATE


def load_repairs(conv_id: str, path=None) -> dict[int, dict]:
    """gold 修复表：{idx: {answer|None, adversarial, question?, 依据, 引证}}。"""
    if path is None:
        return {}
    from pathlib import Path

    p = Path(path)
    out: dict[int, dict] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("conv") == conv_id and isinstance(o.get("idx"), int):
            out[o["idx"]] = o
    return out


def apply_repair(qa: QA, rep: dict | None) -> QA:
    """把修复套到 QA 副本（不改原对象）。
    可答化修复同时把 category 改为 4（单跳规则判分）——否则 judge 仍按对抗
    rubric 把正确答案判 wrong（iter15 idx161/170/184 实证）。"""
    if not rep:
        return qa
    new = QA(
        idx=qa.idx,
        question=rep.get("question", qa.question),
        category=qa.category,
        answer=qa.answer,
        adversarial_answer=qa.adversarial_answer,
        evidence=qa.evidence,
    )
    if rep.get("adversarial"):
        new.answer = None
    elif "answer" in rep:
        new.answer = rep["answer"]
        if new.category == 5:
            new.category = 4
    return new


REFUSAL_HINTS = (
    "未提及",
    "没有提到",
    "未提到",
    "无法确定",
    "无法回答",
    "不能确定",
    "没有说明",
    "未说明",
    "没有相关信息",
    "对话中没有",
    "未提供",
    "没有记录",
)


@dataclass
class Grade:
    idx: int
    grade: str  # exact / partial / wrong
    source: str  # det / llm
    reason: str = ""
    missing: list[str] = field(default_factory=list)
    f1: float = 0.0


def is_clean_refusal(pred: str) -> bool:
    """干净拒答：以标准拒答句开头，且后半句没有具体实体/数字猜测。"""
    p = normalize_answer_text(pred)
    return p in {normalize_answer_text(REFUSAL), *map(normalize_answer_text, REFUSAL_HINTS)}


def deterministic_grade(qa: QA, pred: str) -> str | None:
    """返回 exact/wrong 或 None（交 LLM）。"""
    if pred is None:
        return "wrong"
    # 对抗题（含 2 个有 gold 的特例——有 gold 走普通路径）
    if qa.answer is None:
        if is_clean_refusal(pred):
            return "exact"
        return None  # 拒答带其他猜测等细节交 LLM 分级
    from .protocol import complete_equal

    if complete_equal(qa.answer, pred):
        return "exact"
    return None


async def llm_grade(qa: QA, pred: str, client: LLMClient, conv_id: str) -> dict:
    # rubric 按可答性而非 category 编号：有 gold 的 cat-5（"不是"型/修复可答化）
    # 用单跳规则——对抗 rubric 会把正确答案判 wrong（iter19 实证）
    cat = "对抗" if qa.answer is None else CATEGORY_MAP.get(qa.category, "单跳")
    if qa.answer is not None and qa.category == 5:
        cat = "单跳"
    gold = qa.gold_text() or "（无标准答案——对话中不存在该信息）"
    user = JUDGE_TEMPLATE.format(
        question=qa.question,
        gold=gold,
        response=pred or "（空）",
        category_rules=CATEGORY_RULES[cat],
    )
    from .protocol import checked_json

    def validate(obj):
        if obj.get("grade") not in ("exact", "partial", "wrong"):
            raise ValueError("invalid grade")
        if not isinstance(obj.get("missing"), list) or not all(
            isinstance(x, str) for x in obj["missing"]
        ):
            raise ValueError("invalid missing elements")
        if not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
            raise ValueError("invalid reason")
        return obj

    result = await checked_json(
        client,
        role="locomo_judge",
        max_tokens=2048,
        namespace=ns(conv_id, "judge_validated_v1"),
        validator=validate,
        messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
    )
    if result["status"] != "ok":
        return {"grade": "evaluation_error", "missing": [], "reason": "判分执行失败，未计为答错"}
    return result


def char_bigram_f1(gold: str, pred: str) -> float:
    def bg(s: str) -> dict[str, int]:
        t = re.sub(r"\s+", "", str(s))
        d: dict[str, int] = {}
        for i in range(len(t) - 1):
            k = t[i : i + 2]
            d[k] = d.get(k, 0) + 1
        return d

    g, p = bg(gold), bg(pred)
    if not g or not p:
        return 1.0 if not g and not p else 0.0
    overlap = sum(min(g[k], p.get(k, 0)) for k in g)
    prec = overlap / sum(p.values())
    rec = overlap / sum(g.values())
    return 2 * prec * rec / (prec + rec) if prec + rec else 0.0


async def grade_all(
    qa_list: list[QA],
    preds: dict[int, str],
    client: LLMClient,
    conv_id: str,
    repairs: dict[int, dict] | None = None,
    answer_statuses: dict[int, str] | None = None,
) -> dict:
    if repairs is None:
        repairs = load_repairs(conv_id)
    grades: list[Grade] = []
    orig_grades: list[Grade] = []
    for qa in qa_list:
        pred = preds.get(qa.idx, "")
        # 主口径：修复后 gold；并算原始 gold 口径（未修复题两次调用同键，判分缓存命中）
        rqa = apply_repair(qa, repairs.get(qa.idx))
        for tgt, out in ((rqa, grades), (qa, orig_grades)):
            if answer_statuses and answer_statuses.get(qa.idx, "ok") != "ok":
                out.append(
                    Grade(tgt.idx, "answer_error", "execution", "作答执行失败，未计为语义答错")
                )
                continue
            det = deterministic_grade(tgt, pred)
            if det == "exact":
                g = Grade(tgt.idx, "exact", "det")
            elif det == "wrong":
                g = Grade(tgt.idx, "wrong", "det")
            else:
                r = await llm_grade(tgt, pred, client, conv_id)
                g = Grade(tgt.idx, r["grade"], "llm", r["reason"], r["missing"])
            g.f1 = (
                0.0
                if tgt.category == 5 and tgt.answer is None
                else char_bigram_f1(tgt.gold_text(), pred)
            )
            out.append(g)
    report = _aggregate(qa_list, grades, conv_id)
    orig_n = sum(g.grade == "exact" for g in orig_grades)
    orig_errors = sum(g.grade in ("evaluation_error", "answer_error") for g in orig_grades)
    report["orig_exact"] = orig_n
    report["orig_exact_rate"] = None if orig_errors else round(orig_n / max(len(orig_grades), 1), 4)
    report["orig_evaluation_errors"] = orig_errors
    report["repairs_applied"] = len(repairs)
    return report


def _aggregate(qa_list: list[QA], grades: list[Grade], conv_id: str) -> dict:
    by_cat: dict[str, dict] = {}
    exact_n = 0
    for qa, g in zip(qa_list, grades):
        c = by_cat.setdefault(
            qa.cat_name,
            {
                "n": 0,
                "exact": 0,
                "partial": 0,
                "wrong": 0,
                "evaluation_error": 0,
                "answer_error": 0,
            },
        )
        c["n"] += 1
        c[g.grade] += 1
        exact_n += g.grade == "exact"
    errors = sum(g.grade in ("evaluation_error", "answer_error") for g in grades)
    topic = [(qa, g) for qa, g in zip(qa_list, grades) if qa.category in TOPIC_CATEGORIES]
    return {
        "conv": conv_id,
        "n": len(grades),
        "exact": exact_n,
        "exact_rate": None if errors else round(exact_n / max(len(grades), 1), 4),
        "evaluation_errors": errors,
        "exact_rate_bounds": [
            exact_n / max(len(grades), 1),
            (exact_n + errors) / max(len(grades), 1),
        ],
        "j_exact_rate": None
        if any(g.grade in ("evaluation_error", "answer_error") for _, g in topic)
        else round(sum(g.grade == "exact" for _, g in topic) / max(len(topic), 1), 4),
        "f1_avg": round(sum(g.f1 for g in grades) / max(len(grades), 1), 4),
        "by_category": {
            k: {
                **v,
                "exact_rate": None
                if v["evaluation_error"] or v["answer_error"]
                else round(v["exact"] / v["n"], 4),
            }
            for k, v in by_cat.items()
        },
        "grades": [
            {
                "idx": g.idx,
                "grade": g.grade,
                "source": g.source,
                "reason": g.reason,
                "missing": g.missing,
                "f1": round(g.f1, 3),
            }
            for g in grades
        ],
    }
