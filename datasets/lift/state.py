"""Materialize accepted assets in a container layer; verify fresh-container reloads."""

import json
from pathlib import Path

from src.lift.adapters.container.exec import docker_exec_async

from darwinagent.agent import AgentKernelBundle
from darwinagent.runtime.artifacts import atomic_json, digest

STATE_PATH = "/opt/darwin-state/accepted.json"


def snapshot(bundle, wiki=None):
    bundle.verify()
    files = {
        str(p.relative_to(bundle.root)): p.read_text()
        for p in sorted(bundle.root.rglob("*"))
        if p.is_file()
    }
    value = {"kernel_id": bundle.content_id, "files": files, "wiki": wiki or {}}
    return {"value": value, "sha256": digest(value)}


async def install(session, value):
    import asyncio

    encoded = json.dumps(value, ensure_ascii=False)
    script = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); value=sys.stdin.read(); "
        "p.parent.mkdir(parents=True,exist_ok=True); "
        "old=p.read_text() if p.exists() else None; "
        "p.write_text(value) if old != value else None; print('written' if old != value else 'replayed')"
    )
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        session.container_name,
        "python",
        "-c",
        script,
        STATE_PATH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(encoded.encode())
    if process.returncode:
        raise RuntimeError("Accepted-state materialization failed")
    return stdout.decode().strip()


async def load(session, root, expected=None):
    raw = await docker_exec_async(
        session.container_name,
        [
            "python",
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
            STATE_PATH,
        ],
    )
    envelope = json.loads(raw)
    if digest(envelope["value"]) != envelope["sha256"]:
        raise ValueError("Container accepted state digest differs")
    value = envelope["value"]
    if expected and value["kernel_id"] != expected:
        raise ValueError("Container loaded a different selected kernel")
    root = Path(root)
    bundle_root = root / "bundle"
    for name, text in value["files"].items():
        path = bundle_root / name
        if not path.resolve().is_relative_to(bundle_root.resolve()):
            raise ValueError("Invalid exported bundle path")
        if path.exists() and path.read_text() != text:
            raise ValueError("Previously loaded bundle changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(text)
    bundle = AgentKernelBundle(bundle_root)
    if bundle.content_id != value["kernel_id"]:
        raise ValueError("Loaded asset content differs")
    atomic_json(
        root / "load-receipt.json",
        {
            "container": session.container_name,
            "image": session.image,
            "state_sha256": envelope["sha256"],
            "kernel_id": bundle.content_id,
            "source": STATE_PATH,
        },
    )
    return bundle, envelope
