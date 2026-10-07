"""Concurrent dispatch only; grading rules and aggregation come from frozen judge."""

from __future__ import annotations

import asyncio

from . import judge


async def grade_all_dispatched(
    qas, predictions, client, conv_id, answer_statuses=None, *, concurrency=4
):
    repairs = judge.load_repairs(conv_id)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(qa):
        async with semaphore:
            return await judge.grade_all(
                [qa], predictions, client, conv_id, repairs=repairs, answer_statuses=answer_statuses
            )

    parts = await asyncio.gather(*(one(qa) for qa in qas))
    grades = []
    for qa, part in zip(qas, parts):
        raw = part["grades"][0]
        target = judge.apply_repair(qa, repairs.get(qa.idx))
        f1 = (
            0.0
            if raw["grade"] in ("evaluation_error", "answer_error")
            or target.category == 5
            and target.answer is None
            else judge.char_bigram_f1(target.gold_text(), predictions.get(qa.idx, ""))
        )
        grades.append(
            judge.Grade(raw["idx"], raw["grade"], raw["source"], raw["reason"], raw["missing"], f1)
        )
    report = judge._aggregate(qas, grades, conv_id)
    orig_n = sum(part["orig_exact"] for part in parts)
    orig_errors = sum(part["orig_evaluation_errors"] for part in parts)
    report.update(
        orig_exact=orig_n,
        orig_exact_rate=None if orig_errors else round(orig_n / max(len(qas), 1), 4),
        orig_evaluation_errors=orig_errors,
        repairs_applied=len(repairs),
    )
    return report
