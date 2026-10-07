"""Offline shared recorded wiki fixtures; no test-case dependencies."""

import json

from tests.support.clients import LedgerRecordedClient
from tests.support.device import client
from tests.support.recorded_experiment import RecordedExperiment


class WikiRecordedExperiment(RecordedExperiment):
    def __init__(self, root):
        super().__init__(root)
        self.optimization_mode = "wiki"

    def _client(self, stage):
        if stage == "optimization":
            return LedgerRecordedClient(
                {
                    "wiki_maintainer": [
                        {"cause": "需按来源复检", "action": "核对训练轨迹", "training_ids": []}
                    ]
                }
            )
        status = self.root / stage / "optimization/attempt-0/status.json"
        if (
            stage.startswith("R")
            and status.exists()
            and json.loads(status.read_text())["state"] == "passed"
            and not self.stage_clients[stage]
        ):
            self.stage_clients[stage] += 1
            return LedgerRecordedClient(client(self.case).replies)
        return super()._client(stage)
