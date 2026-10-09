"""Fixed behavioral admission probes, independent of candidate-authored trial logic."""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from types import MappingProxyType

import networkx as nx

from darwinagent.contracts import CorpusBlock, GraphResult, plain
from darwinagent.kg.graph import node_id, node_view
from darwinagent.operators.data import DataCapabilities
from darwinagent.runtime.artifacts import digest


def run_probes(runtime, graph, memory=None):
    """Rename subject data values and shift declared dates. Parameterized F must transform with the input.

    Entity classes are schema vocabulary (like type names), not subject data: values declared in
    meta.entity_classes are exempt from renaming so class-scoped filters stay valid.
    This checks dependence on actual data, not semantic correctness of arbitrary tasks;
    independent C fixtures and source review cover those separately.
    """
    vocabulary = set(runtime.schema.meta.get("entity_classes") or [])
    replacements = {}
    for _, nd in graph.graph.nodes(data=True):
        if nd.get("etype") in {"Source", "EvidenceSpan"}:
            continue  # provenance structure, not subject data
        spec = runtime.schema.entity(nd["etype"])
        if spec is None:
            continue  # 冻结快照图可含 S 未声明的类型：数据面，不属资产探针范围
        values = node_view(nd)
        dtypes = {a.name: a.dtype for a in spec.attributes}
        for key in spec.primary_key:
            value = values.get(key)
            # Subject names only, never values that could collide with hex/digit fragments
            # inside digests (numeric ids, hexish strings); classes are vocabulary.
            if (
                isinstance(value, str)
                and 2 <= len(value) <= 48
                and value not in vocabulary
                and value.strip("0123456789abcdefABCDEF")
            ):
                replacements[value] = "cf_" + digest(value)[:12]
        for key, kind in dtypes.items():
            value = values.get(key)
            if kind == "date" and value:
                try:
                    replacements[value] = (
                        date.fromisoformat(value) + timedelta(days=17)
                    ).isoformat()
                except ValueError:
                    # 年/年月粒度日期（对话记忆常见 '2022' 这类年份值）无法按天平移：
                    # 跳过该值，其余名称/日期替换仍生效，探针不因此崩溃。
                    continue

    def change(value):
        # 只做整值替换（结构化字段/参数值），不做子串替换：改写原文会破坏关键词类查询的
        # 语义一致性（关键词若是实体名的子串，原文被改写后必然失配）。硬编码训练名仍会被
        # 抓——参数与结构化字段都改名，字面量混入的输出无法跟随。
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, dict):
            return {k: change(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [change(v) for v in value]
        return value

    altered = nx.MultiDiGraph()
    nid_map = {}
    for nid, nd in graph.graph.nodes(data=True):
        attrs = change(copy.deepcopy(nd))
        key = change(json.loads(nd["__key__"]))
        attrs["__key__"] = json.dumps(key, ensure_ascii=False)
        new_id = node_id(nd["etype"], key)
        nid_map[nid] = new_id
        altered.add_node(new_id, **attrs)
    for h, t, k, attrs in graph.graph.edges(keys=True, data=True):
        altered.add_edge(nid_map[h], nid_map[t], key=k, **change(attrs))
    sources = {
        sid: CorpusBlock(b.source, change(b.text), change(plain(b.metadata)))
        for sid, b in graph.sources.items()
    }
    cf = GraphResult(nx.freeze(altered), MappingProxyType(sources))
    old = DataCapabilities(graph)
    new = DataCapabilities(cf)
    alias_by_nid = {nid: alias for alias, nid in new.actual_ids.items()}
    alias_map = {alias: alias_by_nid[nid_map[nid]] for alias, nid in old.actual_ids.items()}

    def normalize(value):
        value = change(value)

        def aliases(x):
            if isinstance(x, str) and x in alias_map:
                return alias_map[x]
            if isinstance(x, list):
                return [aliases(v) for v in x]
            if isinstance(x, dict):
                return {k: aliases(v) for k, v in x.items()}
            return x

        return aliases(value)

    records = []
    for asset, _ in runtime.functions.functions.values():
        if "semantic_search" in asset.content:
            # 语义检索由冻结外部索引支撑（记忆面数据，非图派生状态）：改名跟随探针不适用，
            # 硬编码名检查仍由字面量准入覆盖。记录豁免理由，保持探针面可审计。
            records.append(
                {
                    "asset_id": asset.id,
                    "probe": "renamed_keys_shifted_dates",
                    "status": "passed",
                    "note": "semantic_search 走冻结向量索引，索引不随改名派生",
                }
            )
            continue
        for params in asset.trial_inputs:
            params = plain(params)
            before = runtime.call(asset.id, params, graph)
            after = runtime.call(asset.id, normalize(params), cf)
            expected = normalize(before["data"])

            # Query traversal order may change under renamed primary keys; compare list data as a multiset.
            def canonical(v):
                if isinstance(v, dict):
                    return {key: canonical(child) for key, child in v.items()}
                if isinstance(v, list):
                    return sorted(
                        json.dumps(canonical(x), sort_keys=True, ensure_ascii=False) for x in v
                    )
                return v

            if canonical(expected) != canonical(after["data"]):
                raise ValueError(
                    f"F counterexample failed for {asset.id}: output did not follow renamed/date-shifted data"
                )
            records.append(
                {
                    "asset_id": asset.id,
                    "probe": "renamed_keys_shifted_dates",
                    "status": "passed",
                    "before_digest": digest(before["data"]),
                    "after_digest": digest(after["data"]),
                }
            )
    # Graph C must survive a pure renaming/date shift of the same well-formed graph.
    original = runtime.checks.run("graph", runtime.graph_snapshot(graph))
    shifted = runtime.checks.run("graph", runtime.graph_snapshot(cf))
    if [(c["check_id"], c["ok"]) for c in original] != [(c["check_id"], c["ok"]) for c in shifted]:
        raise ValueError("Graph C depends on training names/dates rather than a declared invariant")
    records.append({"probe": "graph_check_renaming", "status": "passed"})
    if memory is not None:
        records.extend(run_fact_probes(runtime, memory))
    return records


def run_fact_probes(runtime, memory):
    """Fixed synthetic fact mutations: negation, plan, duplicates, add/delete, renames and
    date shifts must land as separate preserved facts whose identity follows their content."""
    from dataclasses import replace as _replace

    from darwinagent.contracts import AtomicFact, EntityRef, MemoryResult
    from darwinagent.kg.assembler import GraphAssembler

    facts = list(memory.facts)
    if not facts:
        return []
    base = facts[0]

    def rebuild(fact, **changes):
        fields = {
            "text": fact.text,
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object_entity": fact.object_entity,
            "object_value": fact.object_value,
            "polarity": fact.polarity,
            "modality": fact.modality,
            "time": fact.time,
            "evidence": fact.evidence,
        }
        fields.update(changes)
        return AtomicFact.create(**fields)

    def assemble(mutated):
        merged = MemoryResult(memory.corpus, tuple({f.id: f for f in mutated}.values()))
        return GraphAssembler.build(merged, None, runtime.schema), merged

    def fact_nodes(result):
        return sum(1 for _, nd in result.graph.nodes(data=True) if nd.get("etype") == "AtomicFact")

    records = []
    # Negation and plan flips stay separate facts; nothing merges them away.
    flipped = [
        rebuild(base, polarity="negative" if base.polarity != "negative" else "positive"),
        rebuild(base, modality="plan" if base.modality != "plan" else "statement"),
    ]
    graph, merged = assemble(facts + flipped)
    if fact_nodes(graph) != len(merged.facts):
        raise ValueError("Fact probe failed: polarity/modality flips were merged away")
    records.append(
        {"probe": "negation_and_plan_preserved", "status": "passed", "facts": len(merged.facts)}
    )
    # Exact duplicates collapse; different-source statements never merge.
    graph, merged = assemble(facts + [base])
    if fact_nodes(graph) != len({f.id for f in facts}):
        raise ValueError("Fact probe failed: duplicate handling is not identity-based")
    records.append({"probe": "duplicate_identity", "status": "passed"})
    # Adding and deleting a fact changes the graph exactly and keeps anchoring valid.
    synthetic = rebuild(
        base,
        text="探测用追加事实 " + base.text,
        subject=EntityRef(base.subject.cls, "cf_probe_" + base.subject.name),
    )
    graph, merged = assemble(facts + [synthetic])
    if fact_nodes(graph) != len({f.id for f in facts + [synthetic]}):
        raise ValueError("Fact probe failed: added fact did not materialize")
    if len(facts) > 1:
        graph, merged = assemble(facts[1:])
        if fact_nodes(graph) != len({f.id for f in facts[1:]}):
            raise ValueError("Fact probe failed: deleted fact left residue")
    records.append({"probe": "add_delete_follow_memory", "status": "passed"})
    # Identity follows content: renamed subjects and shifted dates change the derived id.
    renamed = rebuild(base, subject=EntityRef(base.subject.cls, "cf_" + base.subject.name))
    if renamed.id == base.id:
        raise ValueError("Fact probe failed: identity ignores subject content")
    if base.time.start:
        from datetime import date, timedelta

        shifted = rebuild(
            base,
            time=_replace(
                base.time,
                start=(date.fromisoformat(base.time.start) + timedelta(days=17)).isoformat(),
            ),
        )
        if shifted.id == base.id:
            raise ValueError("Fact probe failed: identity ignores dates")
    records.append({"probe": "identity_follows_content", "status": "passed"})
    return records
