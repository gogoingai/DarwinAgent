"""Recorded client for full controller tests, with no provider or credential access."""

import json
from types import SimpleNamespace


class RecordedFullClient:
    def __init__(
        self,
        cfg,
        *,
        fail_worker=False,
        false_success=False,
        adopt=False,
        initial=None,
        thirty=False,
    ):
        self.cfg, self.calls = cfg, 0
        self.requests = []
        self.fail_worker, self.false_success = fail_worker, false_success
        self.adopt, self.initial, self.thirty = adopt, initial, thirty
        self.judge_count = 0
        self.q4_failed = False

    def http_attempts(self):
        return 0

    async def chat(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        role = kwargs["role"]
        if role == "trajectory_judge":
            response = {"trajectory_score": 0.75}
        elif role == "judge":
            self.judge_count += 1
            request = json.loads(kwargs["messages"][1]["content"])
            q4 = request["task"]["id"].endswith("-q4")
            fail_q4 = self.adopt and q4 and not self.q4_failed
            self.q4_failed = self.q4_failed or fail_q4
            success = (not self.thirty or self.judge_count == 30) and not fail_q4
            response = {
                "final_response": json.dumps(
                    {
                        "success": success,
                        "score": 0.5 if self.false_success or not success else 1,
                        "reason": "Q4_PRIVATE_FEEDBACK_SENTINEL" if fail_q4 else "synthetic",
                    }
                )
            }
        elif role == "worker":
            if self.fail_worker:
                self.fail_worker = False
                response = {"unknown_action": True}
            elif (
                self.adopt
                and "K1_SYNTHETIC_STRATEGY" not in json.dumps(kwargs["messages"])
                and not any(m["role"] == "assistant" for m in kwargs["messages"])
            ):
                response = {"tool": "list_files", "params": {}}
            else:
                response = {"final_response": "Synthetic delivery"}
        elif role == "proposal":
            if self.adopt:
                assets = [a.to_dict() for a in self.initial.assets.assets]
                for asset in assets:
                    if asset["kind"] == "P":
                        asset["content"] += "\nK1_SYNTHETIC_STRATEGY"
                response = {"action": "submit_patch", "patches": {"assets": assets}}
            else:
                response = {"action": "no_change", "reason": "synthetic no change"}
        else:
            response = {"summary": "synthetic training evidence"}
        return SimpleNamespace(
            content=json.dumps(response),
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            elapsed_s=0.0,
        )
