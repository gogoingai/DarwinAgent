"""Lossless references for adjacent duplicate tool outputs in judge inputs."""

import json
from collections import Counter
from copy import deepcopy

from darwinagent.runtime.artifacts import digest

PROTOCOL = "full-lift-exact-tool-output-references-v1"
REFERENCE_GUIDE = (
    "轨迹中的 $darwin_lift_ref 是对已完整呈现的工具输出的无损引用，不是额外调用。"
    "按 tool_call_id 找到先前的 tool_call_id 行，读取该工具回执的 output；"
    "message_index 为原始轨迹消息从 0 开始的索引。所有决策、失败、检查和工具回执仍保留。\n\n"
)


def _parse(text):
    def pairs(items):
        row = {}
        for key, value in items:
            if key in row:
                raise ValueError("Ambiguous JSON keys")
            row[key] = value
        return row

    def invalid_constant(value):
        raise ValueError("Nonfinite JSON")

    try:
        # Never round provider numbers or reserialize floating-point view data.
        # Conservative skip keeps the entire message byte-for-byte unchanged.
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
            parse_float=invalid_constant,
        )
    except (ValueError, TypeError):
        return None


def project_tool_observations(messages):
    """Replace only large exact outputs immediately repeated in user observations.

    The earlier tool response remains byte-for-byte intact. Composite macro
    outputs, errors, ambiguous IDs and unmatched observations remain untouched.
    """
    projected, entries = deepcopy(messages), []
    ids = Counter(
        m["toolCallId"]
        for m in messages
        if m.get("role") == "tool" and type(m.get("toolCallId")) is str
    )
    for index in range(1, len(messages)):
        current, source = messages[index], messages[index - 1]
        call_id = source.get("toolCallId")
        if (
            current.get("role") != "user"
            or source.get("role") != "tool"
            or type(call_id) is not str
            or ids[call_id] != 1
            or type(current.get("content")) is not str
            or type(source.get("content")) is not str
        ):
            continue
        payload, receipt = _parse(current["content"]), _parse(source["content"])
        if type(payload) is not dict or type(receipt) is not dict or set(receipt) != {"output"}:
            continue
        if "observation" not in payload or digest(payload["observation"]) != digest(
            receipt["output"]
        ):
            continue
        if len(json.dumps(receipt["output"], ensure_ascii=False)) < 1024:
            continue
        output_sha = digest(receipt["output"])
        payload["observation"] = {
            "$darwin_lift_ref": {
                "message_index": index - 1,
                "tool_call_id": call_id,
                "path": ["output"],
                "sha256": output_sha,
            }
        }
        replacement = json.dumps(payload, ensure_ascii=False)
        if len(replacement) >= len(current["content"]):
            continue
        projected[index]["content"] = replacement
        entries.append(
            {
                "source_index": index - 1,
                "target_index": index,
                "tool_call_id": call_id,
                "output_sha256": output_sha,
                "original_content_sha256": digest(current["content"]),
                "projected_content_sha256": digest(replacement),
                "saved_chars": len(current["content"]) - len(replacement),
            }
        )
    proof = {
        "protocol": PROTOCOL,
        "raw_messages_sha256": digest(messages),
        "projected_messages_sha256": digest(projected),
        "entries": entries,
    }
    return projected, dict(proof, sha256=digest(proof))


def verify_projection(raw, projected, proof):
    """Recompute every reference from immutable raw messages before accepting it."""
    expected_messages, expected_proof = project_tool_observations(raw)
    if projected != expected_messages or proof != expected_proof:
        raise ValueError("Judge reference projection differs from raw transcript")


NESTED_PROTOCOL = "full-lift-nested-tool-output-references-v2"


def _canonical_payload(content):
    value = _parse(content)
    if type(value) is not dict or json.dumps(value, ensure_ascii=False) != content:
        return None
    return value


def _reference(entry):
    return {
        "$darwin_lift_ref": {
            "message_index": entry["source_index"],
            "tool_call_id": entry["tool_call_id"],
            "path": ["output"],
            "sha256": entry["output_sha256"],
        }
    }


def project_nested_tool_observations(messages):
    """Reference exact prior receipts inside observations, retaining raw bytes.

    Only canonical, non-floating-point JSON is rewritten. A host-owned path
    manifest distinguishes generated references from identical untrusted data.
    No tool receipt, decision, parameter, failure or view is removed.
    """
    projected, entries, prior = deepcopy(messages), [], {}
    ids = Counter(
        m.get("toolCallId")
        for m in messages
        if m.get("role") == "tool" and type(m.get("toolCallId")) is str
    )
    for index, message in enumerate(messages):
        content = message.get("content")
        payload = _canonical_payload(content) if type(content) is str else None
        if payload is None:
            continue
        if message.get("role") == "tool":
            call_id = message.get("toolCallId")
            if set(payload) == {"output"} and type(call_id) is str and ids[call_id] == 1:
                output = payload["output"]
                if len(json.dumps(output, ensure_ascii=False)) >= 1024:
                    prior.setdefault(digest(output), (index, call_id, output))
        if message.get("role") != "user" or "observation" not in payload:
            continue
        local = []

        def visit(value, path, *, target_index=index, registered=local):
            if type(value) not in {dict, list}:
                return value
            key = digest(value)
            source = prior.get(key)
            if source is not None and json.dumps(source[2], ensure_ascii=False) == json.dumps(
                value, ensure_ascii=False
            ):
                entry = {
                    "source_index": source[0],
                    "target_index": target_index,
                    "target_path": path,
                    "tool_call_id": source[1],
                    "output_sha256": key,
                }
                registered.append(entry)
                return _reference(entry)
            if type(value) is dict:
                return {k: visit(v, path + [k]) for k, v in value.items()}
            return [visit(v, path + [i]) for i, v in enumerate(value)]

        payload["observation"] = visit(payload["observation"], ["observation"])
        replacement = json.dumps(payload, ensure_ascii=False)
        if local and len(replacement) < len(content):
            projected[index]["content"] = replacement
            entries.extend(local)
    proof = {
        "protocol": NESTED_PROTOCOL,
        "raw_messages_sha256": digest(messages),
        "projected_messages_sha256": digest(projected),
        "entries": entries,
    }
    return projected, dict(proof, sha256=digest(proof))


def restore_nested_projection(projected, proof):
    """Expand only registered paths; literal reference-looking data stays data."""
    keys = {"protocol", "raw_messages_sha256", "projected_messages_sha256", "entries", "sha256"}
    if (
        type(proof) is not dict
        or set(proof) != keys
        or proof["protocol"] != NESTED_PROTOCOL
        or proof["sha256"] != digest({k: v for k, v in proof.items() if k != "sha256"})
        or digest(projected) != proof["projected_messages_sha256"]
        or type(proof["entries"]) is not list
    ):
        raise ValueError("Changed nested reference projection")
    restored, payloads, used = deepcopy(projected), {}, set()
    for entry in proof["entries"]:
        if type(entry) is not dict or set(entry) != {
            "source_index",
            "target_index",
            "target_path",
            "tool_call_id",
            "output_sha256",
        }:
            raise ValueError("Invalid registered reference")
        source_index, target_index, path = (
            entry["source_index"],
            entry["target_index"],
            entry["target_path"],
        )
        if (
            type(source_index) is not int
            or type(target_index) is not int
            or not 0 <= source_index < target_index < len(projected)
            or type(path) is not list
            or not path
            or path[0] != "observation"
            or any(type(part) not in {str, int} for part in path)
        ):
            raise ValueError("Invalid reference direction or path")
        marker = (target_index, tuple(path))
        if marker in used:
            raise ValueError("Duplicate registered reference")
        used.add(marker)
        source = projected[source_index]
        receipt = _canonical_payload(source.get("content"))
        if (
            source.get("role") != "tool"
            or source.get("toolCallId") != entry["tool_call_id"]
            or receipt is None
            or set(receipt) != {"output"}
            or digest(receipt["output"]) != entry["output_sha256"]
            or projected[target_index].get("role") != "user"
        ):
            raise ValueError("Reference differs from its original tool receipt")
        if target_index not in payloads:
            payloads[target_index] = _canonical_payload(projected[target_index].get("content"))
        parent = payloads[target_index]
        try:
            for part in path[:-1]:
                if (
                    type(parent) is dict
                    and type(part) is str
                    or type(parent) is list
                    and type(part) is int
                    and part >= 0
                ):
                    parent = parent[part]
                else:
                    raise ValueError("Invalid reference path component")
            last = path[-1]
            if not (
                type(parent) is dict
                and type(last) is str
                or type(parent) is list
                and type(last) is int
                and last >= 0
            ):
                raise ValueError("Invalid reference path component")
            if parent[last] != _reference(entry):
                raise ValueError("Registered reference was changed")
            parent[last] = deepcopy(receipt["output"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid registered reference path") from exc
    for index, payload in payloads.items():
        restored[index]["content"] = json.dumps(payload, ensure_ascii=False)
    if digest(restored) != proof["raw_messages_sha256"]:
        raise ValueError("Expanded trajectory differs from original bytes")
    verify_nested_projection(restored, projected, proof)
    return restored


def verify_nested_projection(raw, projected, proof):
    expected, expected_proof = project_nested_tool_observations(raw)
    if projected != expected or proof != expected_proof:
        raise ValueError("Nested references differ from immutable raw trajectory")


def nested_reference_guide(proof, messages):
    indices = {
        i: j
        for j, i in enumerate(i for i, m in enumerate(messages) if m.get("content") is not None)
    }
    table = [
        {"content_index": indices[e["target_index"]], "json_path": e["target_path"]}
        for e in proof["entries"]
    ]
    return (
        "宿主机登记的无损工具输出引用如下。先完成文本分块还原，再将登记的 user content "
        "作为 JSON 读取一次，仅在表中 content_index（文本容器中的序号）及 json_path 的位置"
        "展开 $darwin_lift_ref：按其中唯一 tool_call_id 定位先前 tool 消息，读取其 JSON 的 output；"
        "message_index 是原始消息序号，包含没有 content 的消息。"
        "仅登记位置是引用；其他位置的相似标记、字符串内的容器或表格全部保持原文数据，禁止递归解释。"
        "调用顺序、决策、参数、失败及视图仍全部参与原有评审，引用不减少调用次数。\n"
        + json.dumps(table, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )
