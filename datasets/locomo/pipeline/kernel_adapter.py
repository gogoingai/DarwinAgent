"""locomo 的内核适配器：字段访问器 + 资产登记（框架 oak/kernel 的数据集侧注入）。

框架（oak/kernel）不含任何 locomo 字段名；本文件是唯一允许出现
"别名/主体/出处" 等 locomo 表示法的地方——这就是"一套框架 + 薄适配"的接缝。
"""
from __future__ import annotations

from pathlib import Path

from oak.kernel.checks import GraphAccessors
from oak.kernel import KernelAssets
from oak.kg.graph import node_view

LOCOMO_ACCESSORS = GraphAccessors(
    node_name=lambda n: str(node_view(n).get("姓名") or node_view(n).get("名称") or node_view(n).get("序号", "")),
    node_aliases=lambda n: [a for a in str(n.get("别名", "")).split(";") if a],
    speakers=lambda g: {
        str(node_view(n).get("姓名", ""))
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
    refusal_text="对话中未提及该信息",
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


def kernel_assets(schema_path: Path, harness_path: Path) -> KernelAssets:
    pipe = Path(__file__).resolve().parent
    import oak.llm.client, oak.operators.library, oak.kg.graph, oak.kernel.harness
    from oak_domains.conversation_memory import harness as domain_harness
    return KernelAssets(
        schema_path=schema_path,
        prompt_paths={"extract": pipe / "prompts/extract.py", "answer": pipe / "prompts/answer.py"},
        function_paths=[pipe / "funcs_compile.py"],
        check_ids=["alias-not-speaker", "fact-has-source", "answer-evidence-integrity", "refusal-cleanliness"],
        check_paths=[Path(__import__("oak.kernel.checks", fromlist=["__file__"]).__file__)],
        harness_path=harness_path,
        dependency_paths=[pipe / "build.py", pipe / "entity_resolve.py", pipe / "tools.py", pipe / "agent.py", pipe / "dates.py",
                          pipe / "config.py", pipe / "runner.py", pipe / "kernel_adapter.py", pipe / "prompts/lexicon.py",
                          Path(domain_harness.__file__), Path(oak.llm.client.__file__), Path(oak.operators.library.__file__),
                          Path(oak.kg.graph.__file__), Path(oak.kernel.harness.__file__)],
        version="portable-v1", scope="domain",
    )
