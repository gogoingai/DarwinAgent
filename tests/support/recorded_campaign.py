"""Offline shared recorded campaign fixtures; no test-case dependencies."""

import asyncio
import contextlib
import io
import json
import tempfile
from collections import Counter
from pathlib import Path

from darwinagent.config import Config, RunConfig
from darwinagent.contracts import EvaluationResult
from darwinagent.experiments import (
    AdoptionPolicy,
    CampaignController,
    ExperimentSpec,
    SelectionPolicy,
)
from darwinagent.experiments.spec import precheck_identity
from darwinagent.kernel import KernelBundle, TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.kernel.revision import training_id
from tests.support.clients import LedgerRecordedClient
from tests.support.device import TASK, review

SERIALS = {
    "train-case": "D-17",
    "train-case-2": "D-27",
    "val-case": "D-18",
    "val-case-2": "D-28",
    "test-case": "D-19",
    "test-case-2": "D-29",
}


class StubAdapter:
    def generation_input(self, case_id):
        from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef

        serial = SERIALS[case_id]
        return CaseInput(
            case_id,
            (
                CorpusBlock(
                    SourceRef("maintenance_record", case_id, "row-1"),
                    f"设备 {serial} 于 2026-09-01 由林维护。",
                ),
            ),
            (QuestionInput("q1", f"谁在什么时候维护了 {serial}？", {"serial": serial}),),
        )


def generation_replies(case_id):
    serial = SERIALS[case_id]
    block = StubAdapter().generation_input(case_id).corpus[0]
    return {
        "extraction": [
            {
                "entities": [
                    {
                        "type": "Maintenance",
                        "key": {"serial": serial, "date": "2026-09-01"},
                        "properties": {"technician": "林"},
                        "source_id": block.source.id,
                        "quote": block.text,
                    }
                ],
                "relations": [],
            }
        ],
        "tools": [
            {"action": "call", "asset_id": "device_lookup", "parameters": {"serial": serial}},
            {"action": "ready"},
        ],
        "answer": [
            {"status": "answered", "answer": "林于2026-09-01维护。", "node_ids": ["n000000"]}
        ],
        "review": [review()],
    }


class VersionAwareEvaluator:
    """Anything produced after B0 (an adopted candidate) scores strictly better."""

    def __init__(self, client, path, root):
        self.client, self.path, self.root = client, Path(path), Path(root)

    async def evaluate(self, result, asked=None):
        b0 = json.loads((self.root / "train" / "B0" / "assets" / "manifest.json").read_text())[
            "version"
        ]
        good = 1 if result.asset_version != b0 else 0
        n = len(result.answers)
        faults = sum(a.status == "execution_error" for a in result.answers)
        return EvaluationResult({"precise": good, "lenient": good}, n, n - faults, faults, 0)


class RecordedCampaign(CampaignController):
    def __init__(self, root, spec, **kwargs):
        self.stage_clients = Counter()
        super().__init__(
            StubAdapter(),
            lambda client, path: VersionAwareEvaluator(client, path, root),
            Config(),
            RunConfig(protocol_attempts=1),
            spec=spec,
            work_dir=root,
            client_factory=self._recorded_client,
            **kwargs,
        )

    def _recorded_client(self, stage_dir):
        name = stage_dir.name if isinstance(stage_dir, Path) else str(stage_dir)
        self.stage_clients[name] += 1
        if self.stage_clients[name] == 1 and name == "B0":
            replies = {"bootstrap": [{"assets": [a.to_dict() for a in load_assets(TASK).assets]}]}
        elif self.stage_clients[name] == 1 and name.startswith("R"):
            pointer = json.loads((self.root / "train" / "published" / "current.json").read_text())
            base = KernelBundle(self.root / "train" / "published" / pointer["path"])
            asset = next(a for a in base.assets.assets if a.role == "answer")
            updated = asset.to_dict()
            updated["content"] += "\nUse the records carefully. " + name
            replies = {
                "proposal": [
                    {
                        "patches": [
                            {
                                "asset": updated,
                                "base_fingerprint": asset.fingerprint,
                                "reason": "General instruction refined from this training run",
                                "training_evidence": [training_id("train-case", "q1")],
                            }
                        ]
                    }
                ]
            }
        else:
            declaration = json.loads((self.root / "campaign.json").read_text())["declaration"][
                "experiment_spec"
            ]
            phase = (
                "train"
                if name.startswith("R") or name == "B0"
                else ("validation" if "validation" in str(stage_dir) else "test")
            )
            if name in SERIALS:
                # 逐 case 客户端（验证/测试）：只装载本 case 的回复
                replies = generation_replies(name)
            else:
                # 训练阶段客户端：按声明顺序装载全部 case 的回复
                replies = {}
                for case_id in declaration[phase]:
                    for role, values in generation_replies(case_id).items():
                        replies.setdefault(role, []).extend(values)
        return LedgerRecordedClient(replies)


def protocol(rounds, cap=None, multi=False):
    if multi:
        return ExperimentSpec(
            train=("train-case", "train-case-2"),
            validation=("val-case", "val-case-2"),
            test=("test-case", "test-case-2"),
            rounds=rounds,
            adoption=AdoptionPolicy("precise", ("lenient",)),
            selection=SelectionPolicy("precise", "lenient"),
            max_question_runs=cap,
        )
    return ExperimentSpec(
        train=("train-case",),
        validation=("val-case",),
        test=("test-case",),
        rounds=rounds,
        adoption=AdoptionPolicy("precise", ("lenient",)),
        selection=SelectionPolicy("precise", "lenient"),
        max_question_runs=cap,
    )


def execute(spec, resume=False, stop=False, root=None):
    td = None
    if root is None:
        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
    try:
        (root / "precheck.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "checks": {},
                    "identity": precheck_identity(Config(), RunConfig(protocol_attempts=1)),
                }
            )
        )
        if stop:
            (root / "STOP").write_text("operator stop\n")
        controller = RecordedCampaign(root, spec=spec, frozen_files=())
        task = TaskSpec.load(TASK / "task.yaml")
        with contextlib.redirect_stdout(io.StringIO()):
            summary = asyncio.run(controller.run(task, resume=resume))
    except BaseException:
        if td is not None:
            td.cleanup()
        raise
    return root, td, summary
