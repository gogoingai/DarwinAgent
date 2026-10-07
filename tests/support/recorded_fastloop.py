"""Offline shared recorded fastloop fixtures; no test-case dependencies."""

import asyncio
import contextlib
import io

from darwinagent.kernel import TaskSpec
from tests.support.device import TASK
from tests.support.recorded_experiment import RecordedExperiment
from tests.support.recorded_wiki import WikiRecordedExperiment


class FastLoopExperiment(WikiRecordedExperiment):
    """记录式全环＋新模式参数（graph_builder 等在 super().__init__ 后注入——
    测试绕过构造校验，运行路径按属性生效）。"""

    def __init__(self, root, evaluator=None, **mode):
        RecordedExperiment.__init__(self, root, evaluator=evaluator)
        self.optimization_mode = "wiki"
        for key, value in mode.items():
            setattr(self, key, value)
        self._round_deadline = None
        self._rebuild_cache = {}


def _run(runner, rounds=1):
    with contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(
            runner.run(
                runner.case.id,
                TaskSpec.load(TASK / "task.yaml"),
                rounds=rounds,
                scope=("S", "F", "C", "P"),
            )
        )
