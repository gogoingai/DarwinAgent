"""Read-only turn evidence, including callbacks absent from chat history."""

import json
from pathlib import Path

from .report import available_sum, turn_totals


def recorded_turns(root):
    root = Path(root)
    chat = root / "chat.json"
    legacy = json.loads(chat.read_text())["turns"] if chat.exists() else []
    paths = sorted((root / "turns").glob("*.json"), key=lambda p: int(p.stem))
    if not paths:
        return [{**row, "evidence_turn_index": index} for index, row in enumerate(legacy)]
    rows = dict(enumerate(legacy))
    for path in paths:
        receipt = json.loads(path.read_text())
        if isinstance(receipt.get("episode"), dict):
            rows[int(path.stem)] = receipt
            continue
        journal = root / "episodes" / ("turn-" + path.stem)
        steps = [json.loads(p.read_text()) for p in sorted((journal / "steps").glob("*.json"))]
        events = [json.loads(p.read_text()) for p in sorted((journal / "events").glob("*.json"))]
        accounting = turn_totals(root, int(path.stem), receipt)
        usage = dict(accounting["usage"])
        for key in ("prompt_tokens", "completion_tokens"):
            values = [
                ((s.get("response") or {}).get("usage") or {}).get(key)
                if s["state"] == "responded"
                else None
                for s in steps
                if s["kind"] == "model"
            ]
            usage[key] = available_sum(values) if steps else None
        rows[int(path.stem)] = {
            **receipt,
            "accounting": accounting,
            "episode": {
                "status": "unknown" if usage["unknown_calls"] else "failed",
                "final_response": "",
                "artifacts": [],
                "events": events,
                "usage": usage,
            },
        }
    return [{**rows[index], "evidence_turn_index": index} for index in sorted(rows)]
