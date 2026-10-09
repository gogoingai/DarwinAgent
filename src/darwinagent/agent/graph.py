"""Source-bound typed task and trajectory projection; facts/events never mutate."""

from collections.abc import Mapping

from darwinagent.contracts import plain
from darwinagent.kernel.spec import validate_value
from darwinagent.runtime.artifacts import digest


def validate_schema(schema):
    if not isinstance(schema, Mapping) or set(schema) != {"nodes", "relations"}:
        raise ValueError("Schema requires only nodes and relations")
    if (
        not isinstance(schema["nodes"], (list, tuple))
        or not schema["nodes"]
        or not isinstance(schema["relations"], (list, tuple))
    ):
        raise ValueError("Schema requires nonempty node types and a relation list")
    ids = set()
    for node in schema["nodes"]:
        if set(node) != {"id", "source", "fields", "where"} or node["source"] not in {
            "task",
            "attachments",
            "events",
        }:
            raise ValueError("Unsupported node declaration")
        if not isinstance(node["fields"], Mapping) or not isinstance(node["where"], Mapping):
            raise ValueError("Typed fields and source filters must be objects")
        if not node["id"] or node["id"] in ids or not node["fields"]:
            raise ValueError("Unique node types with materialized fields required")
        ids.add(node["id"])
        for field in node["fields"].values():
            if set(field) != {"path", "type"} or field["type"] not in {
                "string",
                "integer",
                "number",
                "boolean",
                "object",
                "array",
            }:
                raise ValueError("Unsupported typed field")
            if not isinstance(field["path"], str) or not field["path"]:
                raise ValueError("Field needs a source path")
    relations = set()
    for rel in schema["relations"]:
        if (
            set(rel) != {"id", "from", "to", "from_field", "to_field"}
            or rel["from"] not in ids
            or rel["to"] not in ids
            or not rel["id"]
            or rel["id"] in relations
        ):
            raise ValueError("Unsupported relation declaration")
        relations.add(rel["id"])
        for side in ("from", "to"):
            node = next(n for n in schema["nodes"] if n["id"] == rel[side])
            if rel[side + "_field"] not in node["fields"]:
                raise ValueError("Relation references unmaterialized field")
        left = next(n for n in schema["nodes"] if n["id"] == rel["from"])["fields"][
            rel["from_field"]
        ]["type"]
        right = next(n for n in schema["nodes"] if n["id"] == rel["to"])["fields"][rel["to_field"]][
            "type"
        ]
        if left != right or left not in {"string", "integer", "number", "boolean"}:
            raise ValueError("Relations must join matching scalar field types")
    return schema


def lookup(value, path):
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise ValueError(f"Unmaterializable source path: {path}")
        value = value[part]
    return plain(value)


def project(schema, task, events, *, max_items=12000):
    validate_schema(schema)
    sources = {
        "task": [task.to_dict()],
        "attachments": plain(task.attachments),
        "events": plain(events),
    }
    nodes = []
    for declaration in schema["nodes"]:
        for index, source in enumerate(sources[declaration["source"]]):
            if any(lookup(source, key) != val for key, val in declaration["where"].items()):
                continue
            fields = {}
            for name, spec in declaration["fields"].items():
                value = lookup(source, spec["path"])
                validate_value(value, {"type": spec["type"], "additionalProperties": True}, name)
                fields[name] = value
            ref = {"source": declaration["source"], "index": index, "digest": digest(source)}
            if len(nodes) >= max_items:
                raise ValueError("Derived graph node budget exhausted")
            nodes.append(
                {
                    "id": digest({"type": declaration["id"], "ref": ref}),
                    "type": declaration["id"],
                    "fields": fields,
                    "source_ref": ref,
                }
            )
    edges = []
    for rel in schema["relations"]:
        targets = {}
        for right in (n for n in nodes if n["type"] == rel["to"]):
            targets.setdefault(right["fields"][rel["to_field"]], []).append(right)
        for left in (n for n in nodes if n["type"] == rel["from"]):
            for right in targets.get(left["fields"][rel["from_field"]], ()):
                if len(edges) >= max_items:
                    raise ValueError("Derived graph relation budget exhausted")
                edges.append(
                    {
                        "type": rel["id"],
                        "from": left["id"],
                        "to": right["id"],
                        "source_refs": [left["source_ref"], right["source_ref"]],
                    }
                )
    return {
        "schema_id": digest(schema),
        "source_id": digest(sources),
        "nodes": nodes,
        "relations": edges,
    }
