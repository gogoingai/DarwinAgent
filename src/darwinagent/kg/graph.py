"""图构建与合并：键签名（类型+主键）折叠 → 边端点重连 → 去重。

EntityCandidate/RelationCandidate 是建图输入契约（框架层）；具体抽取在各任务模块。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

_WS_RE = re.compile(r"\s+")


def canon_value(v) -> str:
    """主键值规范化：strip + 多空格折叠 + 统一字符串。"""
    if v is None:
        return ""
    return _WS_RE.sub(" ", str(v).strip())


def node_view(nd: dict) -> dict:
    """统一节点视图：`__key__` 主键 + 普通非元数据属性（后者覆盖同名）。

    全仓唯一解析点 —— operators/library、graph evidence、validator、cost、
    City enrich 都必须经由它读取节点，禁止各自半套解析（曾因此丢 city）。
    """
    view = {}
    try:
        view.update(json.loads(nd.get("__key__", "{}")))
    except Exception:
        pass
    view.update({k: v for k, v in nd.items() if not k.startswith("__") and k != "etype"})
    return view


class GraphValidationError(ValueError):
    pass


def node_id(etype: str, key: dict) -> str:
    # Structured boundaries prevent delimiter collisions. Values remain typed.
    payload = [[str(k), v] for k, v in sorted(key.items())]
    return f"{etype}::" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _typed(value, dtype):
    import math

    if value is None:
        return None
    if dtype == "string":
        if isinstance(value, (dict, list, tuple)):
            raise GraphValidationError("Expected scalar string")
        return str(value)
    if dtype == "int":
        if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value).strip()):
            raise GraphValidationError(f"Expected int: {value!r}")
        return int(value)
    if dtype == "float":
        if isinstance(value, bool):
            raise GraphValidationError("Boolean is not a float")
        number = float(value)
        if not math.isfinite(number):
            raise GraphValidationError("Nonfinite float")
        return number
    if dtype == "bool":
        if type(value) is bool:
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise GraphValidationError("Expected bool")
    if dtype == "date":
        from datetime import date

        return date.fromisoformat(str(value)).isoformat()
    raise GraphValidationError(f"Unknown dtype: {dtype}")


def build_graph(entities, relations, schema, *, on_invalid="raise") -> nx.MultiDiGraph:
    """Validate all candidates; quarantine must be explicitly requested."""
    if on_invalid not in ("raise", "isolate"):
        raise ValueError("on_invalid must be raise or isolate")
    errors = schema.validate()
    if errors:
        raise GraphValidationError(str(errors))
    g = nx.MultiDiGraph()
    g.graph.update(format_version=2, validation_errors=[])
    relations_by_name = {r.name: r for r in schema.relations}

    def invalid(kind, value, exc):
        issue = {"kind": kind, "value": repr(value)[:500], "error": str(exc)}
        if on_invalid == "raise":
            raise GraphValidationError(str(issue)) from exc
        g.graph["validation_errors"].append(issue)

    def normalize(etype, key, properties):
        entity = schema.entity(etype)
        if entity is None:
            raise GraphValidationError(f"Undeclared entity type {etype!r}")
        attrs = {a.name: a.dtype for a in entity.attributes}
        if set(key) != set(entity.primary_key):
            raise GraphValidationError(f"{etype}: expected primary key {entity.primary_key}")
        nk = {k: _typed(v, attrs[k]) for k, v in key.items()}
        if any(v is None or (isinstance(v, str) and not v.strip()) for v in nk.values()):
            raise GraphValidationError("Empty primary key")
        np = {}
        for raw, value in properties.items():
            name = entity.canonical_attr(raw)
            if name is None:
                raise GraphValidationError(f"{etype}: undeclared attribute {raw!r}")
            v = _typed(value, attrs[name])
            if name in nk:
                if v != nk[name]:
                    raise GraphValidationError("Property conflicts with primary key")
            else:
                np[name] = v
        return nk, np

    def subtype(actual, allowed):
        seen, pending = set(), [actual]
        while pending:
            current = pending.pop()
            if current in allowed:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(
                ax.params["sup"]
                for ax in schema.axioms
                if ax.kind == "subclass" and ax.params.get("sub") == current
            )
        return False

    for e in entities:
        try:
            key, props = normalize(e.etype, e.key, e.properties)
            nid = node_id(e.etype, key)
            if nid not in g:
                g.add_node(
                    nid,
                    etype=e.etype,
                    __key__=json.dumps(key, ensure_ascii=False),
                    __merged__=0,
                    __sources__=[],
                    **props,
                )
            cur = g.nodes[nid]
            for k, v in props.items():
                if cur.get(k) in (None, "") and v not in (None, ""):
                    cur[k] = v
            cur["__merged__"] += 1
            if e.chunk_id not in cur["__sources__"]:
                cur["__sources__"].append(e.chunk_id)
        except (ValueError, TypeError, KeyError) as exc:
            invalid("entity", e, exc)

    for r in relations:
        try:
            spec = relations_by_name.get(r.relation)
            if spec is None:
                raise GraphValidationError(f"Undeclared relation {r.relation!r}")
            hk, _ = normalize(r.head[0], r.head[1], {})
            tk, _ = normalize(r.tail[0], r.tail[1], {})
            if not subtype(r.head[0], spec.domain) or not subtype(r.tail[0], [spec.range]):
                raise GraphValidationError(f"Domain/range mismatch for {r.relation}")
            head_id, tail_id = node_id(r.head[0], hk), node_id(r.tail[0], tk)
            if spec.functional and head_id in g:
                if any(
                    ed.get("relation") == r.relation and tgt != tail_id
                    for _, tgt, ed in g.out_edges(head_id, data=True)
                ):
                    raise GraphValidationError(f"Functional relation conflict: {r.relation}")
            for nid, etype, key in ((head_id, r.head[0], hk), (tail_id, r.tail[0], tk)):
                if nid not in g:
                    g.add_node(
                        nid,
                        etype=etype,
                        __key__=json.dumps(key, ensure_ascii=False),
                        __merged__=0,
                        __sources__=[],
                        __incomplete__=True,
                    )
            if not g.has_edge(head_id, tail_id, key=r.relation):
                g.add_edge(head_id, tail_id, key=r.relation, relation=r.relation)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            invalid("relation", r, exc)
    return g


def save_graph(g: nx.MultiDiGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = nx.node_link_data(g, edges="links")
    path.write_text(json.dumps(data, ensure_ascii=False))


def load_graph(path: Path) -> nx.MultiDiGraph:
    data = json.loads(path.read_text())
    return nx.node_link_graph(data, edges="links")


def derive_relations(g: nx.MultiDiGraph, schema) -> nx.MultiDiGraph:
    """按 relation.derive={attr: 字段, split?: 分隔符} 从属性值派生边。

    属性可能存于 __key__（主键）或普通属性 —— 两者都读。
    目标节点不存在时自动创建（值类实体）。幂等：重复调用不产生重边。
    """
    if schema.validate():
        raise GraphValidationError(str(schema.validate()))

    for rel in schema.relations:
        if not rel.derive:
            continue
        rng = schema.entity(rel.range)
        if len(rng.primary_key) != 1:
            raise GraphValidationError(
                "Attribute-derived endpoints need an explicit single primary key"
            )
        pk_field = rng.primary_key[0]
        dtype = next(a.dtype for a in rng.attributes if a.name == pk_field)
        attr = rel.derive.get("attr")
        attr_by_domain = rel.derive.get("attr_by_domain") or {}
        if not attr and not attr_by_domain:
            continue
        sep = rel.derive.get("split") or ";"
        for nid, nd in list(g.nodes(data=True)):
            etype = nd.get("etype")
            if etype not in rel.domain:
                continue
            # 按类型选字段（不同源同语义字段大小写不一：city vs City）
            fld = attr_by_domain.get(etype) or attr
            if not fld:
                continue
            raw = node_view(nd).get(fld)
            if raw in (None, ""):
                continue
            for val in str(raw).split(sep):
                val = val.strip()
                if not val:
                    continue
                key_value = _typed(val, dtype)
                tid = node_id(rel.range, {pk_field: key_value})
                if rel.functional and any(
                    ed.get("relation") == rel.name and tgt != tid
                    for _, tgt, ed in g.out_edges(nid, data=True)
                ):
                    raise GraphValidationError(f"Functional derived relation conflict: {rel.name}")
                if not g.has_node(tid):
                    g.add_node(
                        tid,
                        etype=rel.range,
                        __key__=json.dumps({pk_field: key_value}, ensure_ascii=False),
                        __incomplete__=True,
                        **{pk_field: key_value},
                    )
                if not g.has_edge(nid, tid, key=rel.name):
                    g.add_edge(nid, tid, key=rel.name, relation=rel.name, derived=True)
    return g


def graph_stats(g: nx.MultiDiGraph) -> dict:
    by_type: dict[str, int] = {}
    for _, nd in g.nodes(data=True):
        t = nd.get("etype", "?")
        by_type[t] = by_type.get(t, 0) + 1
    by_rel: dict[str, int] = {}
    for _, _, ed in g.edges(data=True):
        r = ed.get("relation", "?")
        by_rel[r] = by_rel.get(r, 0) + 1
    return {
        "n_nodes": g.number_of_nodes(),
        "n_edges": g.number_of_edges(),
        "by_type": by_type,
        "by_relation": by_rel,
    }


def graph_samples(g: nx.MultiDiGraph, per_type: int = 5) -> dict:
    out: dict[str, list] = {}
    for nid, nd in g.nodes(data=True):
        t = nd.get("etype", "?")
        if len(out.setdefault(t, [])) < per_type:
            out[t].append(
                {
                    "id": nid,
                    # node_view：主键也可见（此前 judge 以为 schema 缺字段，实为主键未物化）
                    "props": {k: v for k, v in node_view(nd).items() if v not in (None, "")},
                }
            )
    return out


@dataclass
class EntityCandidate:
    etype: str
    key: dict
    properties: dict
    chunk_id: str


@dataclass
class RelationCandidate:
    relation: str
    head: tuple[str, dict]
    tail: tuple[str, dict]
