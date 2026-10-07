"""Small installed example with independent generation and evaluation interfaces."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from darwinagent import (
    CaseInput,
    CorpusBlock,
    QuestionInput,
    SourceRef,
    EvaluationResult,
    Config,
    RunConfig,
    ExperimentRunner,
    AdoptionPolicy,
    TaskSpec,
)
from darwinagent.kernel import KernelBundle
from darwinagent.kernel.assets import KernelAssets
from darwinagent.kernel.registration import load_assets
from darwinagent.kernel.revision import training_id
from darwinagent.runtime.artifacts import atomic_json

TASK_ROOT = Path(__file__).resolve().parent / "device_maintenance"


class MaintenanceAdapter:
    """Generation sees records and questions; no evaluator references."""

    def generation_input(self, case_id):
        return CaseInput(
            case_id,
            (
                CorpusBlock(
                    SourceRef("maintenance_record", case_id, "row-1"),
                    "设备 D-17 于 2026-09-01 由林维护。",
                ),
            ),
            (QuestionInput("q1", "谁在什么时候维护了 D-17？", {"serial": "D-17"}),),
        )


class MaintenanceEvaluator:
    """Score actual answers against evaluator-only references, independent of stage."""

    def __init__(self):
        self._reference = {"q1": ("林", "2026-09-01")}

    async def evaluate(self, result):
        rows = []
        points = 0
        completed = 0
        for answer in result.answers:
            expected = self._reference[answer.question_id]
            matches = (
                sum(token in answer.answer for token in expected)
                if answer.status == "answered"
                else 0
            )
            points += matches / len(expected)
            completed += answer.status != "execution_error"
            rows.append(
                {
                    "question_id": answer.question_id,
                    "status": answer.status,
                    "original": {"precise": matches == len(expected)},
                    "error": answer.error,
                }
            )
        total = len(result.answers)
        return EvaluationResult(
            {"accuracy": points / total if total else 0.0},
            total,
            completed,
            total - completed,
            0,
            tuple(rows),
        )


class ScriptedTransport:
    """Bundled replay responses. It scripts model behavior, never evaluator scores."""

    def __init__(self):
        self.calls = []

    async def chat(self, **request):
        self.calls.append(request)
        role = request["role"]
        payload = json.loads(request["messages"][1]["content"])
        if role == "extraction":
            source = payload["sources"][0]
            value = {
                "entities": [
                    {
                        "type": "Maintenance",
                        "key": {"serial": "D-17", "date": "2026-09-01"},
                        "properties": {"technician": "林"},
                        "source_id": source["source_id"],
                        "quote": source["text"],
                    }
                ],
                "relations": [],
            }
        elif role == "tools":
            value = (
                {"action": "ready"}
                if payload["previous_results"]
                else {
                    "action": "call",
                    "asset_id": "device_lookup",
                    "parameters": {"serial": "D-17"},
                }
            )
        elif role == "answer":
            complete = "Include maintenance date" in request["messages"][0]["content"]
            value = {
                "status": "answered",
                "answer": "林于2026-09-01维护。" if complete else "林维护了设备。",
                "node_ids": ["n000000"],
            }
        elif role == "review":
            value = {
                "accepted": True,
                "supported": True,
                "subject_correct": True,
                "consistent": True,
                "complete": True,
                "abstention_valid": False,
                "feedback": "Recorded review accepted the candidate; independent scoring still applies.",
            }
        elif role == "proposal":
            # Revisions are based on the actual published bundle and Wiki facts.
            assets = payload["assets"]
            asset = next(a for a in assets if a["role"] == "answer")
            updated = dict(asset)
            addition = (
                "\nInclude maintenance date when the question asks when, as well as the technician."
                if "Include maintenance date" not in asset["content"]
                else "\nCheck each requested field against its cited maintenance source."
            )
            updated["content"] += addition
            evidence = [q["training_id"] for q in payload["questions"]]
            value = {
                "patches": [
                    {
                        "asset": updated,
                        "base_fingerprint": asset["fingerprint"],
                        "reason": "Refine requested-field coverage using current Wiki training feedback.",
                        "training_evidence": list(evidence),
                    }
                ]
            }
        elif role == "wiki_maintainer":
            value = {
                "cause": "Requested-field coverage is a hypothesis to inspect in the recorded training trace.",
                "action": "Check every requested field against retrieved sources; retain rejected decisions.",
                "training_ids": payload["event"]["training_ids"],
            }
        else:
            raise ValueError(f"Unsupported replay role: {role}")
        return SimpleNamespace(content=json.dumps(value, ensure_ascii=False))

    async def aclose(self):
        pass

    def ledger_summary(self):
        return {"total_calls": len(self.calls)}


async def run_demo(output, *, mode="replay", rounds=2, resume=False, config=None):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = config or Config(work_dir=root)
    if mode not in ("replay", "live"):
        raise ValueError("mode must be replay or live")
    if mode == "live":
        cfg.validate_model()
    seed = root / "seed"
    if not seed.exists():
        assets = load_assets(TASK_ROOT)
        baseline = tuple(
            replace(a, content="Answer using the technician named in the retrieved evidence.")
            if a.role == "answer"
            else a
            for a in assets.assets
        )
        KernelAssets(baseline).export(seed)
    runner = ExperimentRunner(
        MaintenanceAdapter(),
        lambda _client, _path: MaintenanceEvaluator(),
        cfg,
        RunConfig(protocol_attempts=2, answer_attempts=2, tool_steps=3, max_tokens=1800),
        AdoptionPolicy("accuracy", ()),
        root,
        client_factory=(lambda _stage: ScriptedTransport()) if mode == "replay" else None,
        optimization_mode="wiki",
        wiki_call_limit=6,
        proposal_attempts=2,
        seed_assets=seed,
    )
    summary = await runner.run(
        "maintenance-demo",
        TaskSpec.load(TASK_ROOT / "task.yaml"),
        rounds=rounds,
        resume=resume,
        scope=("P",),
    )
    atomic_json(
        root / "demo-summary.json",
        {
            "mode": mode,
            "notice": "Replay demonstrates mechanisms, not measured model performance."
            if mode == "replay"
            else "Live scores measure this tiny example only.",
            "summary": summary,
        },
    )
    return summary
