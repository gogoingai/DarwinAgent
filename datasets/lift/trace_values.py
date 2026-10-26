"""Deterministic lossless references for repeated JSON values and text chunks."""

import json
from collections import Counter

from .trace_chunks import _chunks, digest

PROTOCOL = "trace-values-v1"
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_ENCODED_BYTES = 16 * 1024 * 1024
MAX_VALUES = 100000
MAX_MESSAGES = 10000


def _json(value, style="compact"):
    separators = (",", ":") if style == "compact" else (", ", ": ")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=separators)


def _parse(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate source key")
            result[key] = value
        return result

    prefix = "[toolCall] " if text.startswith("[toolCall] ") else ""
    raw = text[len(prefix) :]
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
        for style in ("spaces", "compact"):
            if _json(value, style) == raw:
                return "json", prefix, style, value
    except (ValueError, TypeError, RecursionError):
        pass
    return "raw", "", "", text


def encode(texts, *, max_source_bytes=MAX_SOURCE_BYTES):
    if (
        type(texts) is not list
        or len(texts) > MAX_MESSAGES
        or any(type(t) is not str for t in texts)
    ):
        raise ValueError("Expected source strings")
    source_bytes = sum(len(t.encode()) for t in texts)
    if source_bytes > max_source_bytes:
        raise ValueError("Trajectory source exceeds local resource limit")
    parsed, counts, chunks = [], Counter(), Counter()
    indexed_bytes, value_count = 0, 0

    def collect(value, depth=0):
        nonlocal indexed_bytes, value_count
        value_count += 1
        if value_count > MAX_VALUES:
            raise ValueError("JSON value count exceeds local resource limit")
        if depth > 64:
            raise ValueError("JSON value depth exceeds local resource limit")
        key = _json(value)
        if len(key) >= 512:
            if key not in counts:
                indexed_bytes += len(key.encode())
                if indexed_bytes > MAX_INDEX_BYTES:
                    raise ValueError("Value index exceeds local resource limit")
            counts[key] += 1
        if type(value) is str:
            chunks.update(c for c in _chunks(value) if len(c) >= 512)
        elif type(value) is dict:
            for child in value.values():
                collect(child, depth + 1)
        elif type(value) is list:
            for child in value:
                collect(child, depth + 1)

    for text in texts:
        mode, prefix, style, value = _parse(text)
        if mode == "json":
            collect(value)
        else:
            chunks.update(c for c in _chunks(text) if len(c) >= 512)
        parsed.append((mode, prefix, style, value))
    shared, frames = {}, []
    for index, (mode, prefix, style, value) in enumerate(parsed):
        definitions = []

        def define(key, build, *, target=definitions):
            if key not in shared:
                node = build()
                number = len(shared)
                shared[key] = number
                target.append([number, node])
            return ["r", shared[key]]

        def emit(value, allow_reference=True, *, source_mode=mode):
            key = _json(value) if source_mode == "json" else None
            if allow_reference and key is not None and counts[key] > 1:
                return define(("json", key), lambda: emit(value, False))
            if type(value) is str:
                pieces = list(_chunks(value))
                if not any(chunks[c] > 1 for c in pieces):
                    return ["literal", value]
                parts = []
                for chunk in pieces:
                    if chunks[chunk] > 1:
                        parts.append(define(("text", chunk), lambda c=chunk: ["literal", c]))
                    else:
                        parts.append(["literal", chunk])
                return ["text", parts]
            if type(value) is dict:
                children = [[k, emit(v)] for k, v in value.items()]
                if all(v[0] == "literal" for _, v in children):
                    return ["literal", value]
                return ["object", children]
            if type(value) is list:
                children = [emit(v) for v in value]
                if all(v[0] == "literal" for v in children):
                    return ["literal", value]
                return ["array", children]
            return ["literal", value]

        body = emit(value)
        frames.append(
            dict(
                index=index,
                mode=mode,
                prefix=prefix,
                style=style,
                definitions=definitions,
                body=body,
            )
        )
    value = dict(
        protocol=PROTOCOL, frames=frames, source_sha256=digest(texts), source_bytes=source_bytes
    )
    size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
        size += len(part.encode())
        if size > MAX_ENCODED_BYTES:
            raise ValueError("Value representation exceeds local resource limit")
    return dict(value, sha256=digest(value))


def render(encoded):
    return [_json(frame) for frame in encoded["frames"]]


def _bounded_json(value, style, limit):
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":") if style == "compact" else (", ", ": "),
    )
    parts, size = [], 0
    for part in encoder.iterencode(value):
        size += len(part.encode())
        if size > limit:
            raise ValueError("Expanded source exceeds local resource limit")
        parts.append(part)
    return "".join(parts)


def decode(encoded, *, max_source_bytes=MAX_SOURCE_BYTES):
    keys = {"protocol", "frames", "source_sha256", "source_bytes", "sha256"}
    if (
        type(encoded) is not dict
        or set(encoded) != keys
        or encoded["protocol"] != PROTOCOL
        or type(encoded["source_bytes"]) is not int
        or not 0 <= encoded["source_bytes"] <= max_source_bytes
        or type(encoded["frames"]) is not list
        or len(encoded["frames"]) > MAX_MESSAGES
    ):
        raise ValueError("Invalid value reference envelope or resource limit")
    encoded_size = 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(encoded):
        encoded_size += len(part.encode())
        if encoded_size > MAX_ENCODED_BYTES:
            raise ValueError("Value representation exceeds local resource limit")
    if encoded["sha256"] != digest({k: v for k, v in encoded.items() if k != "sha256"}):
        raise ValueError("Invalid value reference digest")
    values, used, texts = [], set(), []
    remaining = max_source_bytes
    allocation_remaining = 4 * max_source_bytes

    def allocate(size):
        # References reuse existing objects. Charge only newly created containers
        # and strings, including definitions retained until all frames finish.
        nonlocal allocation_remaining
        allocation_remaining -= size
        if allocation_remaining < 0:
            raise ValueError("Value allocation exceeds local resource limit")

    def restore(node, depth=0):
        if depth > 64 or type(node) is not list or len(node) != 2:
            raise ValueError("Invalid value reference node")
        kind, payload = node
        if kind == "literal":
            return payload
        if kind == "r":
            if type(payload) is not int or not 0 <= payload < len(values):
                raise ValueError("Unknown or forward value reference")
            used.add(payload)
            return values[payload]
        if kind == "object":
            if type(payload) is not list:
                raise ValueError("Invalid object reference")
            allocate(128 + 72 * len(payload))
            result = {}
            for pair in payload:
                if (
                    type(pair) is not list
                    or len(pair) != 2
                    or type(pair[0]) is not str
                    or pair[0] in result
                ):
                    raise ValueError("Invalid object key")
                result[pair[0]] = restore(pair[1], depth + 1)
            return result
        if kind in {"array", "text"}:
            if type(payload) is not list:
                raise ValueError("Invalid child sequence")
            allocate(64 + 8 * len(payload))
            result = [restore(p, depth + 1) for p in payload]
            if kind == "array":
                return result
            if (
                any(type(p) is not str for p in result)
                or sum(len(p.encode()) for p in result) > remaining
            ):
                raise ValueError("Invalid text expansion or resource limit")
            allocate(64 + 4 * sum(map(len, result)))
            return "".join(result)
        raise ValueError("Unknown value reference tag")

    for index, frame in enumerate(encoded["frames"]):
        if (
            type(frame) is not dict
            or set(frame) != {"index", "mode", "prefix", "style", "definitions", "body"}
            or type(frame["index"]) is not int
            or frame["index"] != index
            or frame["mode"] not in {"json", "raw"}
            or frame["prefix"] not in {"", "[toolCall] "}
            or type(frame["definitions"]) is not list
        ):
            raise ValueError("Invalid value frame")
        for definition in frame["definitions"]:
            if (
                type(definition) is not list
                or len(definition) != 2
                or type(definition[0]) is not int
                or definition[0] != len(values)
                or len(values) >= MAX_VALUES
            ):
                raise ValueError("Invalid definition order")
            allocate(8)
            values.append(restore(definition[1]))
        value = restore(frame["body"])
        if frame["mode"] == "raw":
            if type(value) is not str or frame["prefix"] or frame["style"]:
                raise ValueError("Invalid raw source frame")
            text = value
        else:
            if frame["style"] not in {"compact", "spaces"}:
                raise ValueError("Invalid original JSON style")
            text = frame["prefix"] + _bounded_json(value, frame["style"], remaining)
        size = len(text.encode())
        remaining -= size
        if remaining < 0:
            raise ValueError("Expanded source exceeds local resource limit")
        texts.append(text)
    if (
        used != set(range(len(values)))
        or digest(texts) != encoded["source_sha256"]
        or max_source_bytes - remaining != encoded["source_bytes"]
    ):
        raise ValueError("Expanded source or definition inventory changed")
    if encode(texts, max_source_bytes=max_source_bytes) != encoded:
        raise ValueError("Noncanonical value reference envelope")
    return texts


GUIDE = (
    "每条 content 都是宿主机生成的 trace-values-v1 容器，index 从0递增。"
    "按顺序登记 definitions 的 [编号,节点]，编号跨消息共享且只能引用此前定义。"
    "节点 [literal,值] 是原始数据，内部标记禁止再解释；[r,编号] 展开此前完整值；"
    "[object,[[键,节点],...]] 保持键序；[array,[节点,...]] 保持元素顺序；"
    "[text,[节点,...]] 仅拼接字符串。mode=raw 时展开值就是原 content；"
    "mode=json 时展开值按 style=spaces（逗号空格、冒号空格）或 compact（无额外空格）"
    "序列化，前接 prefix 即原 content。角色、工具名、调用ID和用量仍为外层原生字段。"
    "按还原的全部原文使用原有评分要求，不能省略失败、纠错或底层工具调用，不能把容器当成 Agent 动作。\n"
)
