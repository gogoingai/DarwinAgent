"""Shared invocation and identity for synchronous and model-backed graph builders."""

from __future__ import annotations

import inspect

from darwinagent.runtime.artifacts import digest


def builder_identity(builder):
    if hasattr(builder, "identity"):
        return builder.identity()
    try:
        source = inspect.getsource(builder)
    except (OSError, TypeError):
        source = repr(builder)
    return {"graph_mode": "rebuild", "graph_builder": digest(source)}


async def build_snapshot_graph(
    builder, snapshot, runtime, corpus, client, config, *, embedder_factory=None, workspace=None
):
    if hasattr(builder, "build"):
        result = builder.build(
            snapshot,
            runtime,
            corpus,
            client,
            config,
            embedder_factory=embedder_factory,
            workspace=workspace,
        )
    else:
        result = builder(snapshot, runtime.schema, corpus, embedder_factory)
    return await result if inspect.isawaitable(result) else result
