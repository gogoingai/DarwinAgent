"""Framework-owned, read-only data operators and capability registry."""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

from darwinagent.contracts import plain
from darwinagent.kg.graph import node_view

from .sandbox import DATA_CAPABILITIES


class DataCapabilities:
    def __init__(self, graph_result):
        self.graph_result = graph_result
        self.rows = {}
        self.actual_ids = {}
        self.read_ids = set()
        self.read_operations = 0
        self.capability_calls = {}  # 能力名 -> 实际调用次数（准入试跑与轨迹用，注释/字符串不算）
        self.traverse_directions = set()
        self.traverse_observations = []
        self._memory_rows = None  # lazy: 原子记忆 id -> row_id（首次 semantic_search 时构建）
        # 行序＝图插入序（冻结快照的 JSON 装载序，确定且在改名探针副本中保持同序；
        # 按节点 id 排序会在改名后重排，使带 limit 截断的 F 输出无法做改名跟随比对）。
        for index, (nid, nd) in enumerate(graph_result.graph.nodes(data=True)):
            rid = f"n{index:06d}"
            self.actual_ids[rid] = nid
            self.rows[rid] = {
                "node_id": rid,
                "entity_type": nd["etype"],
                **node_view(nd),
                "source_ids": list(nd.get("__sources__", [])),
                "claims": plain(nd.get("__claims__", [])),
            }

    def _read(self, rows):
        self.read_ids.update(r["node_id"] for r in rows)
        return rows

    def nodes(self, entity_type="", filters=None, limit=100):
        self.read_operations += 1
        if not isinstance(entity_type, str) or type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("Invalid data query")
        filters = filters or {}
        if not isinstance(filters, (dict, MappingProxyType)):
            raise ValueError("Filters must be an object")
        rows = [
            r
            for r in self.rows.values()
            if (not entity_type or r["entity_type"] == entity_type)
            and all(r.get(k) == v for k, v in filters.items())
        ]
        return self._read(rows[:limit])

    def search(self, terms, entity_type="", limit=40):
        self.read_operations += 1
        if isinstance(terms, str):
            terms = [terms]
        if (
            not isinstance(terms, (list, tuple))
            or not all(isinstance(x, str) and 0 < len(x) <= 100 for x in terms)
            or len(terms) > 30
        ):
            raise ValueError("Search requires short text terms")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Search limit must be 1..200")
        ranked = []
        import json

        for r in self.rows.values():
            if entity_type and r["entity_type"] != entity_type:
                continue
            text = json.dumps(r, ensure_ascii=False).lower()
            score = sum(1 for term in terms if term.lower() in text)
            if score:
                ranked.append((score, r["node_id"], r))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return self._read([r for _, _, r in ranked[:limit]])

    def traverse(self, node_ids, relation, direction="out"):
        self.read_operations += 1
        if isinstance(node_ids, str):
            node_ids = [node_ids]
        if direction not in {"out", "in"} or len(node_ids) > 100:
            raise ValueError("Invalid traversal")
        self.traverse_directions.add(direction)
        g = self.graph_result.graph
        rev = {v: k for k, v in self.actual_ids.items()}
        found = set()
        matched_edges = 0
        for rid in node_ids:
            if rid not in self.actual_ids:
                raise ValueError("Unknown graph node")
            self.read_ids.add(rid)
            nid = self.actual_ids[rid]
            edges = (
                g.out_edges(nid, data=True) if direction == "out" else g.in_edges(nid, data=True)
            )
            for head, tail, attrs in edges:
                if attrs.get("relation") == relation:
                    matched_edges += 1
                    found.add(rev[tail if direction == "out" else head])
        self.traverse_observations.append(
            {"direction": direction, "relation": relation, "matched_edges": matched_edges}
        )
        return self._read([self.rows[r] for r in sorted(found)])

    @staticmethod
    def project(rows, fields):
        # Lineage is kept out of the function's control by the capability read log.
        return [{k: r.get(k) for k in fields} for r in rows]

    @staticmethod
    def aggregate(rows, field="", operation="count"):
        if operation == "count":
            return len(rows)
        values = [r[field] for r in rows if r.get(field) is not None]
        if not all(type(x) in (float, int) for x in values):
            raise ValueError("Aggregation requires numbers")
        if operation == "sum":
            return sum(values)
        if operation == "min":
            return min(values) if values else None
        if operation == "max":
            return max(values) if values else None
        raise ValueError("Unregistered aggregation")

    @staticmethod
    def order_by(rows, field, descending=False):
        return sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=descending)

    @staticmethod
    def date_difference(left, right):
        return (date.fromisoformat(left) - date.fromisoformat(right)).days

    def semantic_search(self, query="", subject="", limit=8):
        """Cosine retrieval over the frozen vector index attached to the graph; hits resolve
        back to atomic-memory rows by the index id field, keeping read lineage row-level."""
        self.read_operations += 1
        if not isinstance(query, str) or not query.strip():
            raise ValueError("semantic_search requires a query string")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("semantic_search limit must be 1..200")
        index = getattr(self.graph_result, "vector", None)
        if index is None:
            raise ValueError("semantic_search 不可用：本图未挂载冻结向量索引")
        if self._memory_rows is None:
            self._memory_rows = {
                str(r.get(index.id_field)): rid
                for rid, r in self.rows.items()
                if r.get(index.id_field) is not None
            }
        hits = index.search(query.strip(), top_k=max(8, limit * 3))
        picked = []
        for memory_id, score, _meta in hits:
            rid = self._memory_rows.get(str(memory_id))
            if rid is None:
                continue
            row = dict(self.rows[rid])
            row["score"] = round(float(score), 4)
            if subject and str(subject) not in str(row.get("主体", "")):
                continue
            picked.append(row)
            if len(picked) >= limit:
                break
        return self._read(picked)

    @staticmethod
    def relative_date(anchor_iso="", expression=""):
        """Deterministic Chinese relative-date resolution against an ISO anchor date."""
        from .dates import resolve_relative

        try:
            anchor = date.fromisoformat(str(anchor_iso).strip())
        except ValueError:
            raise ValueError("relative_date 需要 ISO 锚日期（YYYY-MM-DD）") from None
        expr = str(expression or "").strip()
        if not expr:
            raise ValueError("relative_date 需要相对时间表达")
        resolved, granularity = resolve_relative(anchor, expr)
        return {
            "anchor": anchor.isoformat(),
            "expression": expr,
            "resolved": resolved,
            "granularity": granularity,
        }

    def registry(self):
        # 每次调用计数后透传：能力是否「真的执行过」以这里为准（评审#4），
        # F 试跑记录与运行轨迹据此判定检索底线是否被触发。
        registry = {}
        for name in DATA_CAPABILITIES:
            fn = getattr(self, name)

            def counted(fname=name, inner=fn):
                def call(*args, **kwargs):
                    self.capability_calls[fname] = self.capability_calls.get(fname, 0) + 1
                    return inner(*args, **kwargs)

                return call

            registry[name] = counted()
        return registry
