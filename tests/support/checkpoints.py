"""Offline shared checkpoints fixtures; no test-case dependencies."""

import json

from darwinagent.kernel import TaskSpec
from darwinagent.runtime.artifacts import digest
from tests.support.travel_faults import (
    TASK_YAML,
    legal_candidate,
)

TRAVEL_CONTRACT = TaskSpec.load(TASK_YAML).answer_contract


def write_checkpoint(root, stage, case_id, question_id, events, status="execution_error"):
    path = root / stage / "generation" / case_id / "answers" / f"{digest(question_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    result = {"question_id": question_id, "status": status, "trace": events}
    path.write_text(
        json.dumps(
            {"identity": "test", "digest": digest(result), "result": result}, ensure_ascii=False
        )
    )
    return path


def candidate_event(snapshot, check_id="c_answer_shape", issues=("answer is not an array",)):
    return {
        "stage": "candidate",
        "attempt": 0,
        "candidate": {
            "status": snapshot["status"],
            "answer": snapshot["answer"],
            "node_ids": snapshot["node_ids"],
        },
        "checks": [{"check_id": check_id, "ok": False, "issues": list(issues)}],
        "check_snapshot": snapshot,
    }


def legal_snapshot():
    candidate = legal_candidate()
    return {
        "stage": "answer",
        "question": "plan the trip",
        "parameters": {},
        "status": "answered",
        "answer": candidate["answer"],
        "node_ids": candidate["node_ids"],
        "evidence": [],
        "visible_evidence": [],
        "structured_answer": json.loads(candidate["answer"]),
    }
