"""Synchronous entry point for offline candidate preflight fixtures."""

import asyncio


def preflight_sync(runner, *args, **kwargs):
    return asyncio.run(runner._preflight(*args, **kwargs))
