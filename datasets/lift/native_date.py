"""Durable native judge calendar; changes only the upstream's current-date field.

The calendar is scoped to one episode, not the process clock. Context-local dates
permit independent async episodes; unscoped upstream callers retain native time.
Existing journals with one unambiguous date can establish the legacy calendar.
Mixed historical dates require explicit migration and are rejected here.
"""

import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.leases import execution_lease

_ACTIVE = ContextVar("lift_native_judge_date", default=None)
_MARKER = re.compile(
    r"(【当前真实日期】今天是 )(\d{4}-\d{2}-\d{2})(（评测环境系统时间，权威可信）)"
)
_PROTOCOL = "native-judge-calendar-v1"


def today():
    return datetime.now().date().isoformat()


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid native evaluation date")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("Noncanonical native evaluation date")
    return value


def _legacy_date(root):
    paths = [
        root / "judge/chat.json",
        *sorted((root / "judge/episodes").glob("turn-*/identity.json")),
    ]
    dates = set()
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError("Native date evidence cannot be a symlink")
        record = json.loads(path.read_text())
        messages = record.get(
            "history", record.get("task", {}).get("parameters", {}).get("native_messages", [])
        )
        for message in messages:
            if message.get("role") != "user":
                continue
            matches = _MARKER.findall(message.get("content", ""))
            if len(matches) > 1:
                raise ValueError("Ambiguous legacy native date marker")
            dates.update(_date(match[1]) for match in matches)
    if len(dates) > 1:
        raise ValueError("Mixed legacy native dates require explicit migration")
    return next(iter(dates), None)


def _install():
    from src.lift.eval import run_task

    original = run_task._build_judge_prompt
    if getattr(original, "_darwin_native_calendar", False):
        return

    @wraps(original)
    def scoped(*args, **kwargs):
        result = original(*args, **kwargs)
        active = _ACTIVE.get()
        if active is None:
            return result
        if len(_MARKER.findall(result)) != 1:
            raise ValueError("Native calendar marker changed or is ambiguous")
        return _MARKER.sub(lambda m: m[1] + active + m[3], result, count=1)

    scoped._darwin_native_calendar = True
    run_task._build_judge_prompt = scoped


@contextmanager
def native_date_scope(root, identity):
    """Freeze native calendar before any judge prompt is constructed or replayed."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Native calendar root cannot be a symlink")
    root = root.resolve()
    identity = json.loads(json.dumps(identity))
    if not isinstance(identity, dict) or not identity:
        raise ValueError("Native date requires an episode identity")
    path = root / "native-date.json"
    with execution_lease(root / "native-date.lock"):
        if path.is_symlink():
            raise ValueError("Native date record cannot be a symlink")
        if path.exists():
            record = json.loads(path.read_text())
            if (
                record.get("protocol") != _PROTOCOL
                or record.get("identity") != identity
                or record.get("sha256")
                != digest({k: v for k, v in record.items() if k != "sha256"})
            ):
                raise ValueError("Native date identity or integrity changed")
            chosen = _date(record["date"])
        else:
            chosen = _legacy_date(root) or _date(today())
            record = {"protocol": _PROTOCOL, "identity": identity, "date": chosen}
            atomic_json(path, dict(record, sha256=digest(record)))
    _install()
    token = _ACTIVE.set(chosen)
    try:
        yield chosen
    finally:
        _ACTIVE.reset(token)
