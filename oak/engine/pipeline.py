from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class BuildEngine:
    """Invoke the domain builder against a fixed asset identity."""
    builder: Callable[..., Awaitable]
    identity: Callable[[], dict]

    async def run(self, *args, **kwargs):
        before = self.identity()
        result = await self.builder(*args, **kwargs)
        if self.identity() != before:
            raise ValueError("Kernel changed while building")
        return result


@dataclass
class InferenceEngine:
    answerer: Callable[..., Awaitable]
    identity: Callable[[], dict]
    concurrency: int = 8

    async def run(self, questions, *, on_result=None):
        """Questions are opaque generation inputs, never reference objects."""
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        before = self.identity()
        semaphore = asyncio.Semaphore(self.concurrency)
        async def one(question):
            async with semaphore:
                if self.identity() != before:
                    raise ValueError("Kernel changed before inference")
                result = await self.answerer(question)
                if self.identity() != before:
                    raise ValueError("Kernel changed during inference")
                if on_result:
                    await on_result(question, result)
                return result
        return await asyncio.gather(*(one(q) for q in questions))
