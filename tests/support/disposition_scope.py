"""Small historical-run scope with an unrelated completed tool journal."""

import json

from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.model_disposition import ModelDispositionStore


def disposition_scope(root):
    root = root.resolve()
    shared = root / "budget/shared"
    shared.mkdir(parents=True)
    run = root / "run"
    session = run / "worker"
    steps = session / "steps"
    steps.mkdir(parents=True)
    (run / "integration-identity.json").write_text(json.dumps({"run_id": "historical"}))
    (session / "session-identity.json").write_text(
        json.dumps({"run_id": "historical", "session": "worker-1", "role": "worker"})
    )
    step = steps / "model.json"
    inp = {"role": "worker", "namespace": "worker", "messages": []}
    step.write_text(json.dumps({"kind": "model", "state": "submitted", "input": inp}))
    unrelated = run / "other/steps/tool.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps({"kind": "tool", "state": "completed", "output": "x" * 100000}))
    receipt = {
        "run_id": "historical",
        "session": "worker-1",
        "role": "worker",
        "max_tokens": 10,
        "request_fingerprint": digest(
            dict(inp, temperature=0, durable=True, use_cache=False, max_tokens=10)
        ),
    }
    return ModelDispositionStore(shared), step, receipt, unrelated
