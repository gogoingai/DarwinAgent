"""LoCoMo generation conversion only. No reference-answer dataclasses or model calls.

Generation sees exactly the original dialogue: message text, speaker and session date.
Observation, event_summary, image captions, question types and gold never enter generation."""

from __future__ import annotations

import json
from pathlib import Path

from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef

from .pipeline.dates import parse_session_datetime


class LocomoAdapter:
    def __init__(self, path: Path):
        self.path = Path(path)

    def generation_input(self, case_id):
        raw = next(c for c in json.loads(self.path.read_text()) if c["sample_id"] == case_id)
        convo = raw["conversation"]
        blocks = []
        sessions = sorted(int(k[8:]) for k in convo if k.startswith("session_") and k[8:].isdigit())
        for n in sessions:
            dt = parse_session_datetime(convo.get(f"session_{n}_date_time", ""))
            iso = dt.isoformat() if dt else ""
            for turn in convo[f"session_{n}"]:
                if not turn.get("text"):
                    continue
                blocks.append(
                    CorpusBlock(
                        SourceRef("message_text", case_id, str(turn["dia_id"])),
                        turn["text"],
                        {"speaker": str(turn.get("speaker", "")), "date": iso},
                    )
                )
        questions = tuple(
            QuestionInput(str(i), str(q["question"])) for i, q in enumerate(raw["qa"])
        )
        return CaseInput(case_id, tuple(blocks), questions)
