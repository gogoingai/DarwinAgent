"""Offline shared device fixtures; no test-case dependencies."""

from pathlib import Path

from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.recorded import RecordedClient

ROOT = Path(__file__).resolve().parents[2]

TASK = ROOT / "tasks/device_maintenance"


def case(name="case-a", serial="D-17", technician="林", day="2026-09-01"):
    return CaseInput(
        name,
        (
            CorpusBlock(
                SourceRef("maintenance_record", name, "row-1"),
                f"设备 {serial} 于 {day} 由{technician}维护。",
            ),
        ),
        (QuestionInput("q1", f"谁在什么时候维护了 {serial}？", {"serial": serial}),),
    )


def extraction(c):
    serial = c.questions[0].parameters["serial"]
    return {
        "entities": [
            {
                "type": "Maintenance",
                "key": {"serial": serial, "date": "2026-09-01"},
                "properties": {"technician": "林"},
                "source_id": c.corpus[0].source.id,
                "quote": c.corpus[0].text,
            }
        ],
        "relations": [],
    }


def review(accepted=True, status="answered"):
    return {
        "accepted": accepted,
        "supported": accepted if status == "answered" else False,
        "subject_correct": True,
        "consistent": True,
        "complete": True,
        "abstention_valid": accepted if status == "abstained" else False,
        "feedback": "支持" if accepted else "已有设备维护记录，需重新生成",
    }


def client(c, answers=None, reviews=None, tools=None):
    return RecordedClient(
        {
            "extraction": [extraction(c)],
            "tools": tools
            or [
                {"action": "call", "asset_id": "device_lookup", "parameters": {"serial": "D-17"}},
                {"action": "ready"},
            ],
            "answer": answers
            or [{"status": "answered", "answer": "林于2026-09-01维护。", "node_ids": ["n000000"]}],
            "review": reviews or [review()],
        }
    )


def spec(path):
    return TaskSpec.load(TASK / "task.yaml", load_assets(TASK).export(path))
