"""Readable display of validated, lossless trace-values-v1 references.

The canonical typed representation remains the byte-exact recovery contract.
This display preserves original JSON keys and action names instead of asking a
judge to interpret every object and literal through the storage node grammar.
"""

from .trace_values import _json


def _display(node, style):
    kind, payload = node
    comma, colon = (", ", ": ") if style == "spaces" else (",", ":")
    if kind == "literal":
        return _json(payload, style)
    if kind == "r":
        return f"host_ref(V{payload})"
    if kind == "text":
        return "host_concat(" + ", ".join(_display(p, style) for p in payload) + ")"
    if kind == "object":
        return "{" + comma.join(_json(k) + colon + _display(v, style) for k, v in payload) + "}"
    if kind == "array":
        return "[" + comma.join(_display(v, style) for v in payload) + "]"
    raise ValueError("Unknown readable node tag")


def render_readable(encoded):
    """Display an already validated canonical representation without expansion."""
    result = []
    for frame in encoded["frames"]:
        node = frame["body"]
        if not frame["definitions"] and node[0] == "literal" and frame["mode"] == "raw":
            text = node[1]
            if "host_ref(" in text or "host_concat(" in text or "HOST_LITERAL_JSON_STRING" in text:
                text = "HOST_LITERAL_JSON_STRING\n" + _json(text)
            result.append(text)
            continue
        definitions = [
            f"V{number} = {_display(value, 'compact')}" for number, value in frame["definitions"]
        ]
        content = frame["prefix"] + _display(node, frame["style"])
        if definitions:
            content = "HOST_DEFINITIONS\n" + "\n".join(definitions) + "\nHOST_CONTENT\n" + content
        result.append(content)
    return result


GUIDE = (
    "以下是保留全部消息顺序的可读轨迹。HOST_DEFINITIONS 中 V编号 定义重复值，"
    "host_ref(V编号) 表示与此前定义完全相同的值；host_concat(...) 只拼接字符串。"
    "这些是宿主机的存储去重标记，不是 Agent 的动作、工具或额外调用。"
    "JSON 字段、工具名、参数和失败结果按原文显示；引号内的同名文本始终是原始数据。"
    "HOST_LITERAL_JSON_STRING 后的 JSON 字符串也仅是原始文本。"
    "引用按已有定义理解即可，无需逐字抄写或重建整份轨迹；"
    "按原有任务要求评判动作序列，保留失败、纠错和全部底层调用。\n"
)
