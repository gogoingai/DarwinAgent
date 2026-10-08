"""Record actual occupied transport slots, without credentials or request bodies."""

import json
import time

from darwinagent.runtime.artifacts import atomic_json


def instrument_pool(client, path):
    original = client._site_for
    state = json.loads(path.read_text()) if path.exists() else {"peak": 0, "intervals": []}
    if state.get("active", 0):
        state.setdefault("interrupted_invocations", []).append(
            {"previous_active": state["active"], "recovered_at": time.time()}
        )
    state["active"] = 0
    wrappers = {}

    class Slot:
        def __init__(self, semaphore):
            self.semaphore = semaphore

        async def __aenter__(self):
            await self.semaphore.acquire()
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            # Each task retains its own interval; the shared slot is reentrant.
            import asyncio

            task = asyncio.current_task()
            self.running[task] = {"started": time.time()}
            atomic_json(path, state)
            return self

        async def __aexit__(self, *args):
            import asyncio

            interval = self.running.pop(asyncio.current_task())
            interval["finished"] = time.time()
            state["intervals"].append(interval)
            state["active"] -= 1
            self.semaphore.release()
            atomic_json(path, state)

    def site(model):
        connection, semaphore = original(model)
        key = id(semaphore)
        if key not in wrappers:
            wrapper = wrappers[key] = Slot(semaphore)
            wrapper.running = {}
        return connection, wrappers[key]

    client._site_for = site
    return state
