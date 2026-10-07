"""收口归因器：把失败题分成评测内（须举证）与评测外（系统侧），对北极星指标计数。

判定规则（docs/DESIGN-closeout.md）：
- 评测内：gold 不可达（译文没有）/ 判分争议 / gold 策展语义——每题必须给出一句话证据
- 评测外：抽取漏 / 检索 miss / 日期错 / 主体错 / 误拒答 / 措辞与推理错 / 执行错误
- 模糊一律算评测外（保守，不给系统放水）
- 归因提示词冻结（诊断用途，非优化对象）；评测器三件套零改动

用法：
  uv run python -m datasets.locomo.pipeline.closeout conv-44 full
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .config import load_locomo_config
from .data import load_conversation
from darwinagent.llm.client import LLMClient

CLOSEOUT_DIR_NAME = "closeout"
THRESHOLD_PER_100 = 6.0  # 北极星：评测外 ≤ 6 题 / 100 题

# 冻结的归因判定提示词（诊断用，不参与任何优化）
ATTR_SYSTEM = "你是评测归因审计员，只输出合法 JSON。你的判定用于工程收口，必须保守。"

ATTR_TEMPLATE = """判定下面这道错题的失败归属：是【评测外】（系统自己的问题）还是【评测内】（评测基准的问题）。

【评测外】（系统侧，算系统的错）：
- extraction_miss 抽取漏：支撑答案的对话内容没被抽成事实
- retrieval_miss 检索 miss：事实在图里但作答没收集到
- date_error 日期错：日期推算/粒度错
- subject_error 主体错：把别人的事答成被问人的
- bad_refusal 误拒答：证据其实够，却拒答了
- phrasing_or_reasoning 措辞与推理错：证据都在，答案组织/要素完整性/推断强度错
- execution 执行错误：调用失败/空输出等技术故障

【评测内】（评测侧，须给证据）：
- gold_not_in_transcript gold 不可达：标准答案的要素在对话原文里根本不存在（翻译丢失/英文 gold 直译）
- judge_dispute 判分争议：答案与 gold 实质等价但被判错（同义/粒度同等/包含关系）
- gold_curation gold 策展：多要素列举的"哪些算数"由英文标注者 arbitrary 决定，语料无法区分

【题目】{question}
【标准答案 gold】{gold}
【系统作答】{pred}
【判分理由】{judge_reason}
【管线预归因】{attribution}
【相关对话原文（gold 可达性检查用）】
{transcript}

判定规则：
1. gold 的每个要素是否在上面的原文中出现？任一关键要素不存在 → 优先考虑 gold_not_in_transcript
2. 答案与 gold 是否实质等价（同义表述/同等粒度）？是 → judge_dispute
3. 列举题要素集合的取舍是否语料无法判定 → gold_curation
4. 拿不准 → 评测外（保守）
只输出 JSON：{{"verdict": "评测外|评测内", "kind": "extraction_miss|retrieval_miss|date_error|subject_error|bad_refusal|phrasing_or_reasoning|execution|gold_not_in_transcript|judge_dispute|gold_curation", "evidence": "一句话证据"}}"""


def _transcript_slice(conv, dia_ids: list[str], max_chars: int = 3500) -> str:
    """按 evidence dia_id 取原文切片（含邻近轮），供 gold 可达性检查。"""
    if not dia_ids:
        # 无 evidence 引用时取问题相关兜底：全部 session 的首轮（保底可见度）
        turns = [t for s in conv.sessions[:6] for t in s.turns[:6]]
    else:
        turns = conv.turns_by_dia(dia_ids)
        seen: set[str] = set()
        expanded = []
        for t in turns:
            if t.dia_id in seen:
                continue
            seen.add(t.dia_id)
            expanded.append(t)
        turns = expanded
    out = "\n".join(f"[{t.dia_id}] {t.speaker}: {t.text}" for t in turns)
    return out[:max_chars]


async def classify_one(client: LLMClient, conv, f: dict, ns: str) -> dict:
    """单题归因：确定性规则优先，模糊交 LLM（冻结提示词）。"""
    # 确定性：执行故障一定是系统侧
    if f.get("attribution", "").startswith("执行故障"):
        return {
            "idx": f["idx"],
            "category": f.get("category"),
            "verdict": "评测外",
            "kind": "execution",
            "evidence": f.get("attribution", ""),
        }
    dia_ids = f.get("evidence") or []
    if isinstance(dia_ids, str):
        dia_ids = [dia_ids]
    prompt = ATTR_TEMPLATE.format(
        question=f.get("question", ""),
        gold=f.get("gold", ""),
        pred=(f.get("pred") or "")[:600],
        judge_reason=(f.get("judge_reason") or "")[:400],
        attribution=f.get("attribution", ""),
        transcript=_transcript_slice(conv, dia_ids),
    )
    r = await client.chat(
        role="locomo_judge",
        temperature=0.0,
        max_tokens=1024,
        json_mode=True,
        namespace=ns,
        messages=[{"role": "system", "content": ATTR_SYSTEM}, {"role": "user", "content": prompt}],
    )
    import re

    try:
        m = re.search(r"\{.*\}", r.content, re.S)
        o = json.loads(m.group(0)) if m else {}
    except Exception:
        o = {}
    if o.get("verdict") not in ("评测内", "评测外") or not o.get("kind"):
        # 容错：evidence 值里未转义引号会破坏整体 JSON，逐字段正则抽取
        c = r.content or ""
        mv = re.search(r'"verdict"\s*:\s*"([^"]+)"', c)
        mk = re.search(r'"kind"\s*:\s*"([^"]+)"', c)
        me = re.search(r'"evidence"\s*:\s*"([^"]*)"', c)
        if mv and mk and mv.group(1) in ("评测内", "评测外") and mk.group(1):
            o = {"verdict": mv.group(1), "kind": mk.group(1), "evidence": me.group(1) if me else ""}
        else:
            o = {}
    verdict = o.get("verdict")
    kind = o.get("kind", "")
    if verdict not in ("评测内", "评测外") or not kind:
        # 解析失败 → 保守算评测外
        return {
            "idx": f["idx"],
            "category": f.get("category"),
            "verdict": "评测外",
            "kind": "phrasing_or_reasoning",
            "evidence": "归因解析失败，保守计系统侧: " + (r.content or "")[:120],
        }
    return {
        "idx": f["idx"],
        "category": f.get("category"),
        "verdict": verdict,
        "kind": kind,
        "evidence": (o.get("evidence") or "")[:240],
    }


async def run(conv_id: str, tag: str) -> dict:
    lc = load_locomo_config()
    conv = load_conversation(lc.dataset_path, conv_id)
    out_dir = lc.runs_dir / conv_id / tag
    failures = [
        json.loads(l) for l in (out_dir / "failures.jsonl").read_text().splitlines() if l.strip()
    ]
    report = json.loads((out_dir / "report.json").read_text())
    client = LLMClient(lc.cfg)
    ns = f"{conv_id.replace('-', '')[:4]}_closeout_attr"
    sem = asyncio.Semaphore(4)

    async def _one(f):
        async with sem:
            return await classify_one(client, conv, f, ns)

    results = await asyncio.gather(*[_one(f) for f in failures])
    system_side = [r for r in results if r["verdict"] == "评测外"]
    eval_side = [r for r in results if r["verdict"] == "评测内"]
    n = report.get("n", 0)
    threshold = round(THRESHOLD_PER_100 * n / 100)
    out = {
        "conv": conv_id,
        "tag": tag,
        "n": n,
        "exact": report.get("exact"),
        "system_side_count": len(system_side),
        "threshold": threshold,
        "met": len(system_side) <= threshold,
        "system_side_by_kind": _count_by(system_side),
        "eval_side_by_kind": _count_by(eval_side),
        "system_side": system_side,
        "eval_side": eval_side,
    }
    dest = lc.runs_dir / CLOSEOUT_DIR_NAME
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"closeout_{conv_id}_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "conv",
                    "tag",
                    "n",
                    "exact",
                    "system_side_count",
                    "threshold",
                    "met",
                    "system_side_by_kind",
                    "eval_side_by_kind",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return out


def _count_by(items: list[dict]) -> dict:
    from collections import Counter

    return dict(Counter(i["kind"] for i in items))


def main() -> None:
    conv_id = sys.argv[1] if len(sys.argv) > 1 else "conv-44"
    tag = sys.argv[2] if len(sys.argv) > 2 else "full"
    asyncio.run(run(conv_id, tag))


if __name__ == "__main__":
    main()
