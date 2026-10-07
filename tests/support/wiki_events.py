"""Recorded admission events for Wiki fix-binding scenarios."""


def failed_attempt(ref, graph_digests=None, scenario="stress", asset="f_flight_pair"):
    admission = {
        "scenarios": [
            {
                "asset_id": asset,
                "scenario_id": scenario,
                "status": "failed",
                "required": True,
                "input_ref": ref,
                "error_type": "ValueError",
                "error": "tool.result.outbound: expected object",
            }
        ]
    }
    if graph_digests is not None:
        admission["graph_digests"] = graph_digests
    return {
        "id": "a" * 64,
        "stage": "R1",
        "kind": "attempt",
        "category": "runtime",
        "scope": "admission",
        "training_ids": [],
        "fact_status": "recorded",
        "confidence": "hypothesis",
        "pending_attribution": False,
        "facts": {"status": "failed", "admission": admission},
    }


def passed_attempt(rows, graph_digests=None, asset_id="f_flight_pair", kind="F"):
    verification = {"verdict": "passed", "scenarios": rows}
    if graph_digests is not None:
        verification["graph_digests"] = graph_digests
    return {
        "id": "b" * 64,
        "stage": "R2",
        "kind": "attempt",
        "category": "strategy",
        "scope": "admission",
        "training_ids": [],
        "fact_status": "recorded",
        "confidence": "hypothesis",
        "pending_attribution": False,
        "facts": {
            "status": "passed",
            "asset_changes": [
                {
                    "asset_id": asset_id,
                    "after": {
                        "id": asset_id,
                        "kind": kind,
                        "content": 'def run(p):\n    return {"rows": [], "truncated": False}\n',
                        "input_contract": {"type": "any"},
                        "output_contract": {"type": "any"},
                        "trial_inputs": [{"org": "A", "dest": "B"}],
                        "fingerprint": "e" * 64,
                    },
                }
            ],
            "verification": verification,
        },
    }


INPUT_DIGEST = "3320bdd2d325b999efdc40189b4d0306caa9e69f59deedfdf14f8cad34c6fb1d"
