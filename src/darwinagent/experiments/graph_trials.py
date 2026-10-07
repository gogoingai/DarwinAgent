"""Identity-bound trial graph rebuilding, extraction and cache supply."""

from __future__ import annotations

import json

from darwinagent.contracts import GraphResult
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import transport_identity


def _rebuild_graph_cached(bundle, case, *, rebuild_cache, graph_builder, snapshot_root):
    """重建图供给（准入/试跑共用）：键＝固定事实摘要＋S 规则指纹＋builder 源码
    摘要（投影实现版本）——F/C/P 改动复用同图，S 变才重建。core 不 import
    任务侧模块：builder 身份用源码摘要（与 Pipeline 身份同口径）。"""

    import inspect

    from darwinagent.runtime.artifacts import digest as _digest

    from ..kernel.validation import validate_bundle
    from .snapshots import snapshot_manifest

    manifest = snapshot_manifest(snapshot_root / case.id)
    schema = validate_bundle(bundle)
    try:
        builder_src = inspect.getsource(graph_builder)
    except (OSError, TypeError):
        builder_src = repr(graph_builder)
    key = (
        case.id,
        manifest.get("facts_digest", ""),
        _digest(schema.to_yaml()),
        _digest(builder_src),
    )
    if key not in rebuild_cache:
        rebuild_cache[key] = graph_builder(
            snapshot_root / case.id, schema, getattr(case, "corpus", ()), None
        )
    return rebuild_cache[key]


async def _rebuild_trial_supply(bundle, cases, *, rebuild_graph_cached_hook):
    """新模式冷启动试跑图供给：草案 bundle 也在「按当前 S 重建的图」上试跑
    （与正式/准入同派生规则），异常处理复用 bootstrap 的 ValueError 反馈路径。"""
    return {case.id: rebuild_graph_cached_hook(bundle, case) for case in cases}


async def _dynamic_trial_graphs(
    bundle, cases, *, adopted_stage_graph_hook, extract_trial_graph_hook
):
    """动态图任务的真图供给：候选补丁未触碰 S/P.extract 时复用已采纳 stage 的
    同题真图（stage 资产版本==候选基线版本＋graph.complete.json digest 校验），
    触碰时必须用候选资产重抽——旧图证明不了新候选安全（拿旧图过检=身份失配）。
    重抽结果键控缓存，bootstrap 纠错与 resume 复用同一份确定性产物。"""

    from collections.abc import Mapping

    origin = getattr(bundle.assets, "origin", None)
    origin = origin if isinstance(origin, Mapping) else {}
    rebuild = any(
        (p.get("asset") or {}).get("kind") == "S"
        or (
            (p.get("asset") or {}).get("kind") == "P"
            and (p.get("asset") or {}).get("role") == "extract"
        )
        for p in origin.get("patches", ())
    )
    graphs = {}
    for case in cases:
        graph = None
        if not rebuild:
            graph = adopted_stage_graph_hook(case, origin.get("base_version"))
        if graph is None:
            graph = await extract_trial_graph_hook(bundle, case)
        graphs[case.id] = graph
    return graphs


def _adopted_stage_graph(case, base_version, *, root):
    """最近已采纳 stage 的同题真图；无基线版本/stage 缺图/digest 不符 → None（走重抽）。"""
    import re as _re
    from types import MappingProxyType as _MP

    import networkx as nx

    from darwinagent.kg.graph import load_graph

    if not base_version:
        return None

    def stage_key(path):
        name = path.parent.name
        if name == "B0":
            return (0, 0)
        m = _re.fullmatch(r"R(\d+)", name)
        return (1, int(m.group(1))) if m else (2, 0)

    stages = []
    for path in root.glob("*/stage.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if row.get("asset_version") == base_version:
            stages.append(path)
    for path in sorted(stages, key=stage_key):
        generation = path.parent / "generation" / case.id
        graph_path = generation / "graph.json"
        complete = generation / "graph.complete.json"
        if not (graph_path.exists() and complete.exists()):
            continue
        try:
            payload = json.loads(graph_path.read_text())
            if digest(payload) != json.loads(complete.read_text()).get("digest"):
                continue
            return GraphResult(
                nx.freeze(load_graph(graph_path)), _MP({b.source.id: b for b in case.corpus})
            )
        except (OSError, ValueError):
            continue
    return None


async def _extract_trial_graph(bundle, case, *, client_factory, config, connection_config, root):
    """用候选资产真抽一次试验图；键=(case, S 指纹, P.extract 指纹, config, 传输身份)
    ——不含候选整体版本，未触碰抽取面的后续候选共享缓存。"""
    from types import MappingProxyType as _MP

    import networkx as nx

    from darwinagent.agents import ExtractionAgent
    from darwinagent.kernel.execution import KernelRuntime
    from darwinagent.kg.graph import load_graph, save_graph

    schema = next(a.fingerprint for a in bundle.assets.assets if a.kind == "S")
    extract = next(
        (a.fingerprint for a in bundle.assets.assets if a.kind == "P" and a.role == "extract"),
        None,
    )
    transport = transport_identity(type("Connection", (), {"cfg": connection_config})())
    key = digest(
        {
            "case": case.to_dict(),
            "schema": schema,
            "extract": extract,
            "config": digest(config.to_dict()),
            "transport": transport,
        }
    )
    cache = root / "trial-graphs" / key
    graph_path = cache / "graph.json"
    if graph_path.exists() and (cache / "graph.complete.json").exists():
        try:
            payload = json.loads(graph_path.read_text())
            if digest(payload) == json.loads((cache / "graph.complete.json").read_text()).get(
                "digest"
            ):
                return GraphResult(
                    nx.freeze(load_graph(graph_path)),
                    _MP({b.source.id: b for b in case.corpus}),
                )
        except (OSError, ValueError):
            pass
    client = client_factory("trial-graph")
    try:
        runtime = KernelRuntime(bundle, config, tuple(q.text for q in case.questions))
        graph = await ExtractionAgent(runtime, client, config, key[:16]).extract_entities(
            case.corpus
        )
    finally:
        await client.aclose()
    cache.mkdir(parents=True, exist_ok=True)
    save_graph(graph.graph, graph_path)
    payload = json.loads(graph_path.read_text())
    atomic_json(
        cache / "graph.complete.json",
        {
            "digest": digest(payload),
            "case": case.id,
            "bundle_version": bundle.version,
            "schema": schema,
            "extract": extract,
        },
    )
    return graph
