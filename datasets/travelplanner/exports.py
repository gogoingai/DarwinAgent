"""Pure published-JSON serialization; no hotel substitution or plan repair."""

import json


def plan(answer):
    return json.loads(answer.answer) if answer.status == "answered" else []


def write(result, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"query_idx": int(a.question_id), "plan": plan(a)}, ensure_ascii=False)
            + "\n"
            for a in result.answers
        )
    )
