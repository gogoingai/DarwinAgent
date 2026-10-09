"""Explicit legacy graph-QA dispatch, retaining v3 admission and recovery contracts."""

from darwinagent.engine import Pipeline


class GraphQACompatibilityBackend:
    """The old facade lifecycle owns QA retries, journals and v3 asset admission.

    Generic AgentTask callers use ExecutionBackend instead. No synthetic question
    wrappers or v4-to-v3 asset conversion occur at this compatibility boundary.
    """

    def identity(self):
        return {"kind": "graph-qa-v3"}

    async def run_experiment(self, runner, *args, **kwargs):
        return await runner._run_graphqa(*args, **kwargs)

    async def run_campaign(self, controller, *args, **kwargs):
        return await controller._run_graphqa(*args, **kwargs)

    async def admit(self, candidate, spec, *args, **kwargs):
        from .stages import _preflight

        return await _preflight(candidate, spec, *args, **kwargs)

    async def run(
        self, task, spec, config, *, client, work_dir, frozen_snapshot=None, graph_builder=None
    ):
        return await Pipeline(
            client, work_dir, frozen_snapshot=frozen_snapshot, graph_builder=graph_builder
        ).run(task, spec, config)
