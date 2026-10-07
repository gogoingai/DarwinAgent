"""The independent evaluation interface delegates to the preserved official bridge."""

import asyncio
from pathlib import Path

from darwinagent.contracts import EvaluationResult
from .exports import plan
from .pipeline.config_task import TPConfig
from .pipeline.data.queries import _from_local
from .pipeline.eval.adapter import TravelPlannerEvaluator as OfficialEvaluator


class TravelPlannerEvaluator:
    def __init__(self, work_dir, tp_root=None, data_dir=None):
        self.cfg = TPConfig()
        self.cfg.work_dir = Path(work_dir).resolve()
        if tp_root is not None:
            self.cfg.tp_root = Path(tp_root).resolve()
        self.data_dir = Path(data_dir or self.cfg.data_dir).resolve()
        self.official = OfficialEvaluator(self.cfg)

    async def evaluate(self, result):
        split, case_id = result.case_id.split(":")
        if split not in {"train", "validation", "test"} or not case_id.isdecimal():
            raise ValueError("Invalid independent evaluation case identity")
        if tuple(a.question_id for a in result.answers) != (str(int(case_id)),):
            raise ValueError("Complete matching single-case answer required")
        # Evaluation consumes its own explicit local references; missing files must not
        # trigger a download or silently fall back to another dataset directory.
        by_id = {q.idx: q for q in _from_local(self.data_dir / f"{split}.queries.jsonl")}
        queries = [by_id[int(a.question_id)] for a in result.answers]
        generation_faults = sum(a.status == "execution_error" for a in result.answers)
        try:
            scores = await asyncio.to_thread(
                self.official.eval_subset, queries, [plan(a) for a in result.answers]
            )
            if scores.n != len(queries) or [p.get("idx") for p in scores.per_query] != [
                q.idx for q in queries
            ]:
                raise ValueError("Incomplete or mismatched official evaluation result")
        except Exception as exc:
            return EvaluationResult(
                {"final": 0, "macro_cs": 0, "macro_hc": 0, "delivered": 0},
                len(queries),
                0,
                generation_faults,
                len(queries),
                tuple(
                    {"question_id": str(q.idx), "error": f"{type(exc).__name__}: {exc}"}
                    for q in queries
                ),
            )
        faults = sum(bool(p.get("error")) for p in scores.per_query)
        return EvaluationResult(
            {
                "final": scores.final_cnt,
                "macro_cs": scores.macro_cs_cnt,
                "macro_hc": scores.macro_hc_cnt,
                "delivered": scores.delivered,
            },
            scores.n,
            scores.n - faults,
            generation_faults,
            faults,
            tuple(scores.per_query),
        )
