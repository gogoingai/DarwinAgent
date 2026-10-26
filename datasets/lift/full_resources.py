"""Explicit resource configuration for real local campaigns; legacy configs stay readable."""

from pathlib import Path

from darwinagent.runtime.resources import ResourceGuard

DEFAULT_LIMITS = {
    "reserve_bytes": 64 * 1024**2,
    "min_free_bytes": 2 * 1024**3,
    "max_rss_bytes": 640 * 1024**2,
}


def resource_limits(config):
    limits = config.get("resource_limits")
    if limits is None:
        return None
    if (
        type(limits) is not dict
        or limits != DEFAULT_LIMITS
        or any(type(v) is not int for v in limits.values())
    ):
        raise ValueError("Full resource limits must explicitly use the validated fixed policy")
    return dict(limits)


def prepare_resources(config, client):
    limits = resource_limits(config)
    if limits is None:
        return None
    if getattr(client, "resource_guard", None) is not None:
        raise ValueError("Existing resource guard cannot be silently replaced")
    guard = ResourceGuard(Path(config["run_root"]).resolve() / "resource-reserve", **limits)
    guard.prepare()
    client.resource_guard = guard
    return guard


def verify_injected_resources(config, client):
    limits = resource_limits(config)
    if limits is not None:
        guard = getattr(client, "resource_guard", None)
        if guard is None or any(guard.declaration().get(k) != v for k, v in limits.items()):
            raise ValueError("Injected transport does not provide the declared resource guard")
