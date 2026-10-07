"""Mandatory engineering checks; assets cannot edit, override or disable these."""

from __future__ import annotations

import json

from darwinagent.contracts import AnswerResult, AtomicFact, CaseInput, GraphResult, plain
from darwinagent.kg.graph import EntityCandidate, RelationCandidate, build_graph, node_view, node_id
from darwinagent.schema.model import Schema
from .spec import validate_value, contract_errors

FIXED_CHECK_IDS = (
    "fixed.input",
    "fixed.schema",
    "fixed.source",
    "fixed.type",
    "fixed.status",
    "fixed.publish",
)


def atomic_memory_errors(schema):
    """Hard minimum for tasks requiring an atomic-memory kernel: meta.atomic_memory_type
    names a declared node type carrying 编号 (memory id) and 陈述 (statement). Everything
    else about S stays free; this is the only frozen floor."""
    name = schema.meta.get("atomic_memory_type")
    if not name:
        return ["S 必须在 meta.atomic_memory_type 声明原子记忆节点类型"]
    entity = schema.entity(str(name))
    if entity is None:
        return [f"meta.atomic_memory_type 指向未声明类型: {name}"]
    attrs = {a.name for a in entity.attributes}
    missing = {"编号", "陈述"} - attrs
    if missing:
        return [f"原子记忆类型 {name} 缺属性: {sorted(missing)}"]
    return []


def validate_bundle(bundle, forbidden_questions=()):
    from .functions import FunctionRegistry
    from .checks import CheckRegistry
    from darwinagent.kg.assembler import anchoring_errors

    bundle.verify()
    schema = Schema.from_yaml(next(a.content for a in bundle.assets.assets if a.kind == "S"))
    if schema.validate():
        raise ValueError(str(schema.validate()))
    if schema.meta.get("anchoring"):
        # Only tasks that declare the fact-anchoring hook must carry the anchoring vocabulary.
        anchor = anchoring_errors(schema)
        if anchor:
            raise ValueError("Schema 事实锚定声明不完整: " + str(anchor))
    if schema.meta.get("atomic_memory_type"):
        problems = atomic_memory_errors(schema)
        if problems:
            raise ValueError("Schema 原子记忆声明不完整: " + str(problems))
    from darwinagent.schema.owlcheck import static_checks

    findings = static_checks(schema)
    if findings:
        raise ValueError(str([f.render() for f in findings]))
    reserved = {"node_id", "entity_type", "source_ids", "claims", "etype"}
    if any(
        a.name in reserved or a.name.startswith("__") for e in schema.entities for a in e.attributes
    ):
        raise ValueError("Schema cannot redefine runtime metadata")
    errors = []
    for asset in bundle.assets.assets:
        errors.extend(contract_errors(asset.input_contract, f"{asset.id}.input_contract"))
        errors.extend(contract_errors(asset.output_contract, f"{asset.id}.output_contract"))
    if errors:
        raise ValueError("Invalid asset contracts: " + " | ".join(errors[:24]))
    FunctionRegistry(bundle, forbidden_questions=forbidden_questions)
    CheckRegistry(bundle, forbidden_questions=forbidden_questions)
    return schema


def validate_graph(result: GraphResult, schema, expected_memory_fingerprint=None):
    if not isinstance(result, GraphResult):
        raise ValueError("Invalid graph result type")
    g = result.graph
    entities = []
    relations = []
    anchored = bool(schema.meta.get("anchoring"))
    if anchored:
        from darwinagent.kg.assembler import anchoring_invariants
    for nid, nd in g.nodes(data=True):
        sources = nd.get("__sources__", [])
        if not sources or set(sources) - set(result.sources):
            raise ValueError("Graph node requires authentic registered sources")
        if anchored:
            if nd.get("etype") == "AtomicFact":
                try:
                    fact = AtomicFact.from_dict(json.loads(nd.get("__fact__", "null")))
                except Exception as exc:
                    raise ValueError(f"Fact node {nid} does not carry a complete definition: {exc}")
                for ev in fact.evidence:
                    block = result.sources.get(ev.source_id)
                    if block is None or block.text[ev.start : ev.end] != ev.quote:
                        raise ValueError("Fact node evidence does not locate in its source")
            elif nd.get("etype") == "EvidenceSpan":
                block = result.sources.get(nd.get("source_id"))
                if block is None or block.text[
                    nd.get("start_offset", -1) : nd.get("end_offset", -1)
                ] != nd.get("quote"):
                    raise ValueError("Evidence span offsets do not locate in its source")
        else:
            claims = nd.get("__claims__", [])
            if not claims:
                raise ValueError("Graph node requires extraction claims")
            for claim in claims:
                block = result.sources.get(claim.get("source_id"))
                if block is None or not claim.get("quote") or claim["quote"] not in block.text:
                    raise ValueError("Forged source or unsupported quotation")
        entity = schema.entity(nd.get("etype"))
        if entity is None:
            raise ValueError("Undeclared graph type")
        values = node_view(nd)
        key = {k: values[k] for k in entity.primary_key}
        if nid != node_id(entity.name, key):
            raise ValueError("Graph identity differs from typed primary key")
        props = {k: v for k, v in values.items() if k not in key}
        entities.append(EntityCandidate(entity.name, key, props, sources[0]))
    for h, t, ed in g.edges(data=True):
        hv, tv = node_view(g.nodes[h]), node_view(g.nodes[t])
        he, te = schema.entity(g.nodes[h]["etype"]), schema.entity(g.nodes[t]["etype"])
        relations.append(
            RelationCandidate(
                ed["relation"],
                (he.name, {k: hv[k] for k in he.primary_key}),
                (te.name, {k: tv[k] for k in te.primary_key}),
            )
        )
    build_graph(
        entities, relations, schema
    )  # Recheck the actual saved graph, not just extraction output.
    from darwinagent.schema.graphcheck import instance_checks

    errors = instance_checks(g, schema)
    if errors:
        raise ValueError(str(errors))
    if anchored:
        violations = anchoring_invariants(g, result.sources, expected_memory_fingerprint, schema)
        if violations:
            raise ValueError("事实锚定不变量被违反: " + str(violations))
    if not entities:
        raise ValueError("Empty graph cannot enter inference")


def validate_candidate(candidate, visible_nodes, spec):
    if not isinstance(candidate, dict) or set(candidate) != {"status", "answer", "node_ids"}:
        raise ValueError("Candidate requires status, answer and node_ids only")
    if (
        candidate["status"] not in {"answered", "abstained"}
        or not isinstance(candidate["answer"], str)
        or not candidate["answer"].strip()
    ):
        raise ValueError("Invalid candidate status/text")
    ids = candidate["node_ids"]
    if (
        not isinstance(ids, list)
        or not all(isinstance(x, str) for x in ids)
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Invalid candidate evidence ids")
    if candidate["status"] == "answered":
        if not ids or set(ids) - set(visible_nodes):
            raise ValueError("Candidate evidence must have been visible")
        value = (
            json.loads(candidate["answer"]) if spec.answer_format == "json" else candidate["answer"]
        )
        validate_value(value, spec.answer_contract, "answer")
    elif ids:
        raise ValueError("Abstention cannot cite answer evidence")


def validate_published(answer, question, graph_result):
    if not isinstance(answer, AnswerResult) or answer.question_id != question.id:
        raise ValueError("Invalid published answer type/id")
    known = {b.source.id for b in graph_result.sources.values()}
    if any(s.id not in known for s in answer.evidence):
        raise ValueError("Unknown published source")
    if answer.status == "answered" and not answer.node_ids:
        raise ValueError("Published answer lacks graph lineage")


def validate_case(case, spec):
    if not isinstance(case, CaseInput):
        raise ValueError("Invalid case type")
    spec.validate_input(case)
    banned = {
        "gold",
        "answer",
        "evidence",
        "category",
        "adversarial_answer",
        "level",
        "has_answer",
        "answer_session_ids",
    }
    if banned & set(spec.metadata_keys) or banned & set(
        spec.parameter_contract.get("properties", {})
    ):
        raise ValueError("Evaluation fields cannot be declared as generation fields")
    for b in case.corpus:
        if banned & set(b.metadata):
            raise ValueError("Evaluation metadata in corpus")


# 任务检索底线 → 必须出现在 F 源码中的沙箱能力名（能力词汇属框架层，映射集中在此；
# 任务只声明意图键——语义参数化在 task.yaml，不进内核硬编码）。
from ..operators.sandbox import DATA_CAPABILITIES as _CAPS

DATA_CAPABILITY_NAMES = frozenset(_CAPS)
RETRIEVAL_FLOOR_CAPABILITIES = {"semantic_search": ("semantic_search",), "traversal": ("traverse",)}


def capability_names(floor):
    """任务声明的检索底线 → 必备能力名元组（未知键按能力名直传）。"""
    names = []
    for key, wanted in (floor or {}).items():
        if wanted:
            names += list(RETRIEVAL_FLOOR_CAPABILITIES.get(key, (key,)))
    return tuple(dict.fromkeys(names))


def capability_calls(source):
    """AST 识别 F 源码中对沙箱数据能力的真实调用（ast.Call+Name）；
    注释、字符串、变量名提及都不算——评审#4：只有真实调用满足底线。"""
    import ast

    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set()
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in DATA_CAPABILITY_NAMES
    }


def capability_floor_errors(assets, required):
    """候选资产集的 F 必须仍覆盖每个必备能力（任务冻结底线：迭代不可删光向量检索/关系遍历）。
    静态层＝AST 真实调用；动态层（试跑是否真的触发能力）由 FunctionRegistry 试跑记录
    的 capability_calls 判定，见 trial_capability_floor_errors。"""
    sources = [a.content or "" for a in assets.assets if a.kind == "F"]
    missing = [cap for cap in required if not any(cap in capability_calls(src) for src in sources)]
    return [
        f"F 集缺失必备能力 {cap}（至少一个 F 需真实调用该能力，注释/字符串提及无效）"
        for cap in missing
    ]


def trial_capability_floor_errors(trial_records, required):
    """动态底线：试跑记录必须显示每个必备能力被真实执行过（capability_calls 计数>0）。
    空结果如实保留在记录里，但能力未触发即不合规——试跑输入必须覆盖到底线能力。"""
    used = set()
    for record in trial_records or ():
        for name, count in (record.get("capability_calls") or {}).items():
            if count:
                used.add(name)
    missing = [cap for cap in required if cap not in used]
    return [
        f"试跑未触发必备能力 {cap}：底线 F 的 trial_inputs 必须真实调用该能力" for cap in missing
    ]
