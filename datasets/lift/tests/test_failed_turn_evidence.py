"""A failed last turn must not disappear from the observed cost."""

import json
import tempfile
import unittest
from pathlib import Path

from datasets.lift.agents import DarwinChatAgent
from datasets.lift.turn_evidence import recorded_turns


class FailedTurnEvidenceTests(unittest.TestCase):
    def test_last_unknown_turn_is_included_once_without_changing_raw_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turns").mkdir()
            complete = {
                "status": "completed",
                "final_response": "done",
                "artifacts": [],
                "events": [],
                "usage": {
                    "model_calls": 2,
                    "leaf_calls": 1,
                    "unknown_calls": 0,
                    "pending_calls": 0,
                },
            }
            unknown = {
                "status": "unknown",
                "final_response": "",
                "artifacts": [],
                "events": [],
                "usage": {
                    "model_calls": 3,
                    "leaf_calls": 3,
                    "unknown_calls": 1,
                    "pending_calls": 1,
                },
            }
            (root / "chat.json").write_text(json.dumps({"turns": [{"episode": complete}]}))
            (root / "turns/0.json").write_text(json.dumps({"episode": complete, "elapsed_s": 2.0}))
            (root / "turns/1.json").write_text(json.dumps({"episode": unknown, "elapsed_s": 3.0}))
            before = {str(p): p.read_bytes() for p in root.rglob("*.json")}
            agent = object.__new__(DarwinChatAgent)
            agent.root = root
            episodes = agent.episodes()
            self.assertEqual(len(episodes), 2)
            self.assertEqual(sum(e["usage"]["model_calls"] for e in episodes), 5)
            self.assertEqual(sum(e["usage"]["leaf_calls"] for e in episodes), 4)
            self.assertEqual(sum(e["usage"]["unknown_calls"] for e in episodes), 1)
            self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*.json")})

    def test_interrupted_turn_without_episode_uses_original_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turns").mkdir()
            (root / "turns/0.json").write_text(
                json.dumps(
                    {
                        "status": "paused",
                        "error_type": "BudgetExceeded",
                        "elapsed_s": 2.0,
                        "episode": None,
                    }
                )
            )
            steps = root / "episodes/turn-0/steps"
            steps.mkdir(parents=True)
            (steps / "000000.json").write_text(
                json.dumps(
                    {
                        "kind": "model",
                        "state": "responded",
                        "response": {"usage": {"prompt_tokens": 11, "completion_tokens": 3}},
                    }
                )
            )
            (steps / "000001.json").write_text(json.dumps({"kind": "tool", "state": "submitted"}))
            agent = object.__new__(DarwinChatAgent)
            agent.root = root
            rows = agent.episodes()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usage"]["model_calls"], 1)
            self.assertEqual(rows[0]["usage"]["leaf_calls"], 1)
            self.assertEqual(rows[0]["usage"]["unknown_calls"], 1)
            self.assertEqual(rows[0]["usage"]["prompt_tokens"], 11)

    def test_legacy_chat_without_turn_receipts_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            episode = {"status": "completed", "usage": {"model_calls": 1}}
            (root / "chat.json").write_text(json.dumps({"turns": [{"episode": episode}]}))
            agent = object.__new__(DarwinChatAgent)
            agent.root = root
            self.assertEqual(agent.episodes(), [episode])

    def test_partial_legacy_turn_receipts_keep_other_chat_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turns").mkdir()
            first = {"status": "completed", "usage": {"model_calls": 2}}
            second = {"status": "failed", "usage": {"model_calls": 3}}
            (root / "chat.json").write_text(
                json.dumps({"turns": [{"episode": first}, {"episode": second}]})
            )
            (root / "turns/1.json").write_text(json.dumps({"episode": second}))
            agent = object.__new__(DarwinChatAgent)
            agent.root = root
            self.assertEqual(agent.episodes(), [first, second])

    def test_unknown_model_tokens_remain_unknown_with_known_subtotal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turns").mkdir()
            (root / "turns/0.json").write_text(json.dumps({"status": "paused"}))
            steps = root / "episodes/turn-0/steps"
            steps.mkdir(parents=True)
            (steps / "000000.json").write_text(
                json.dumps(
                    {
                        "kind": "model",
                        "state": "responded",
                        "response": {"usage": {"prompt_tokens": 11, "completion_tokens": 3}},
                    }
                )
            )
            (steps / "000001.json").write_text(json.dumps({"kind": "model", "state": "submitted"}))
            row = recorded_turns(root)[0]
            self.assertIsNone(row["episode"]["usage"]["prompt_tokens"])
            self.assertIsNone(row["episode"]["usage"]["completion_tokens"])
            self.assertEqual(row["accounting"]["known_tokens_subtotal"], 14)
            self.assertEqual(row["accounting"]["unknown_token_requests"], 1)
            self.assertIsNone(row["accounting"]["tokens"])

    def test_sparse_turn_receipt_keeps_its_original_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "turns").mkdir()
            episode = {"status": "completed", "events": [], "usage": {"model_calls": 1}}
            (root / "turns/3.json").write_text(json.dumps({"episode": episode}))
            row = recorded_turns(root)[0]
            self.assertEqual(row.get("evidence_turn_index"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
