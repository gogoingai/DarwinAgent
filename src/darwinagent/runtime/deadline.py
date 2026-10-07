"""A round's absolute deadline, shared by async tasks and synchronous primitives."""

import time
from contextvars import ContextVar

ROUND_DEADLINE = ContextVar("oak_round_deadline", default=None)


class RoundDeadlineExceeded(TimeoutError):
    pass


def remaining_seconds():
    deadline = ROUND_DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RoundDeadlineExceeded("Round deadline exceeded")
    return remaining


def bounded_timeout(default):
    remaining = remaining_seconds()
    return default if remaining is None else min(default, remaining)
