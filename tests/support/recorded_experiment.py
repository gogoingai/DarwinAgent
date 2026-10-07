"""Offline shared recorded experiment fixtures; no test-case dependencies."""

import json
from collections import Counter
from pathlib import Path

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.experiments import AdoptionPolicy, ExperimentRunner
from darwinagent.kernel import KernelBundle
from darwinagent.kernel.registration import load_assets
from darwinagent.kernel.revision import training_id
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK, case, client


class FixtureEvaluator:
    def __init__(self, stage):
        stage = Path(stage)
        name = stage.parent.name
        self.stage = name if name == "B0" or name.startswith("R") else stage.parent.parent.name

    async def evaluate(self, result, asked=None):
        assert all(a.status == "answered" for a in result.answers)
        return EvaluationResult({"precise": 0 if self.stage == "B0" else 1}, 1, 1, 0, 0)


class RecordedExperiment(ExperimentRunner):
    """Transport fixture only; the actual controller, agents and asset runtime execute."""

    def __init__(self, root, stale=False, evaluator=None):
        self.case = case()
        self.created = []
        self.stage_clients = Counter()
        self.stale = stale
        super().__init__(
            type("Adapter", (), {"generation_input": lambda _, ident: self.case})(),
            evaluator or (lambda transport, path: FixtureEvaluator(path)),
            Config(),
            RunConfig(protocol_attempts=1),
            AdoptionPolicy("precise", ()),
            root,
        )

    def _client(self, stage):
        self.stage_clients[stage] += 1
        if self.stage_clients[stage] == 1:
            if stage == "B0":
                replies = {
                    "bootstrap": [{"assets": [a.to_dict() for a in load_assets(TASK).assets]}]
                }
            else:
                pointer = json.loads((self.root / "published/current.json").read_text())
                base = KernelBundle(self.root / "published" / pointer["path"])
                if (
                    self.optimization_mode == "wiki"
                    and stage.startswith("R")
                    and (self.root / "workspace/workspace.sqlite3").exists()
                ):
                    from darwinagent.runtime.workspace import Workspace

                    selected = Workspace(self.root / "workspace").branch("main")
                    saved = self.root / "workspace/bundles" / (selected["working"] + ".json")
                    if saved.exists():
                        base = KernelBundle(json.loads(saved.read_text())["path"])
                asset = next(a for a in base.assets.assets if a.role == "answer")
                updated = asset.to_dict()
                updated["content"] += "\nUse the current source records carefully. " + stage
                replies = {
                    "proposal": [
                        {
                            "patches": [
                                {
                                    "asset": updated,
                                    "base_fingerprint": "f" * 64
                                    if self.stale and stage == "R2"
                                    else asset.fingerprint,
                                    "reason": "General task instruction refined from this training run",
                                    "training_evidence": [
                                        training_id(self.case.id, self.case.questions[0].id)
                                    ],
                                }
                            ]
                        }
                    ]
                }
        else:
            replies = {role: list(values) for role, values in client(self.case).replies.items()}
        transport = LedgerRecordedClient(replies)
        self.created.append(transport)
        return transport
