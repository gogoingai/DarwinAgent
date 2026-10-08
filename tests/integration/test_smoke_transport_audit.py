"""HTTP-pool measurement survives resume; no network transport is involved."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.smoke_transport_audit import instrument_pool


class PoolAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlap_and_cached_resume_preserve_measurements(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool.json"
            semaphore = asyncio.Semaphore(2)
            client = SimpleNamespace(_site_for=lambda _: (None, semaphore))
            state = instrument_pool(client, path)
            entered = asyncio.Event()
            count = 0

            async def request():
                nonlocal count
                _, slot = client._site_for("fixed-model")
                async with slot:
                    count += 1
                    if count == 2:
                        entered.set()
                    await entered.wait()

            await asyncio.gather(request(), request())
            self.assertEqual((state["peak"], state["active"]), (2, 0))
            self.assertEqual(len(state["intervals"]), 2)
            resumed = instrument_pool(SimpleNamespace(_site_for=lambda _: (None, semaphore)), path)
            self.assertEqual(resumed["peak"], 2)
            self.assertEqual(resumed["intervals"], state["intervals"])

    async def test_interrupted_active_slots_are_historical(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool.json"
            path.write_text(json.dumps({"active": 2, "peak": 2, "intervals": []}))
            client = SimpleNamespace(_site_for=lambda _: (None, asyncio.Semaphore(2)))
            state = instrument_pool(client, path)
            self.assertEqual(state["active"], 0)
            self.assertEqual(state["interrupted_invocations"][0]["previous_active"], 2)
