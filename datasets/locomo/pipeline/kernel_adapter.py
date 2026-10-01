"""locomo 的内核适配器：字段访问器 + 资产登记（框架 oak/kernel 的数据集侧注入）。

框架（oak/kernel）不含任何 locomo 字段名；本文件是唯一允许出现
"别名/主体/出处" 等 locomo 表示法的地方——这就是"一套框架 + 薄适配"的接缝。
"""
from __future__ import annotations

from pathlib import Path

from oak.kernel.checks import GraphAccessors

LOCOMO_ACCESSORS = GraphAccessors(
    node_name=lambda n: str(n.get("id", n.get("__key__", ""))).split("=")[-1].rstrip("}"),
    node_aliases=lambda n: ([n["别名"]] if n.get("别名") else []),
    speakers=lambda g: {
        str(n.get("id", "")).split("=")[-1].rstrip("}")
        for n in g.get("nodes", [])
        if n.get("身份") in ("说话人甲", "说话人乙")
    },
    fact_id=lambda f: str(f.get("fid", "")),
    fact_subject=lambda f: str(f.get("subject", "")),
    fact_source=lambda f: ";".join(f.get("sources", []) or []),
    answer_idx=lambda a: str(a.get("idx", "")),
    answer_refused=lambda a: bool(a.get("refused")),
    answer_text=lambda a: str(a.get("answer", "")),
    answer_evidence=lambda a: list(a.get("evidence", []) or []),
    answer_status_ok=lambda a: a.get("status", "ok") == "ok",
)


def graph_paths(conv_id: str, tag: str | None = None) -> dict[str, Path]:
    """定位一次运行的图/答案/事实产物（locomo 目录约定）。"""
    root = Path(__file__).resolve().parents[3]
    conv_dir = root / "datasets" / "locomo" / "runs" / conv_id
    graph_dir = max(conv_dir.glob("graph_*"), key=lambda p: p.stat().st_mtime)
    answers = conv_dir / (tag or "full") / "answers.jsonl"
    return {
        "graph": graph_dir / "graph.json",
        "facts": graph_dir / "facts.jsonl",
        "answers": answers,
    }
