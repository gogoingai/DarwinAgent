"""Public ExperimentRunner loop preflight; transport is the only recorded component."""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from darwinagent.config import Config
from scripts.smoke_live_loop import AuditTransport, build_report, execute


class LiveLoopFixtureTests(unittest.TestCase):
    def run_fixture(self, root, **options):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch(
                "httpx.AsyncClient.send", side_effect=AssertionError("Replay must not use HTTP")
            ),
            mock.patch(
                "darwinagent.vector.load_embedder", side_effect=AssertionError("No embeddings")
            ),
        ):
            return asyncio.run(execute(root, **options))

    def test_two_actual_candidates_are_admitted_answered_scored_and_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = self.run_fixture(root)
            self.assertIsNone(report["error"], report["error"])
            self.assertEqual(report["formal_candidate_rounds"], ["R1", "R2"])
            self.assertEqual(report["candidate_answer_calls"], 8)
            self.assertEqual(report["http_attempts"], 0)
            self.assertEqual(report["status"], "complete")
            decisions = report["summary"]["rounds"]
            self.assertEqual([d["candidate"]["metrics"]["field_exact"] for d in decisions], [2, 4])
            self.assertEqual([d["accepted"] for d in decisions], [True, True])
            self.assertEqual(report["branch"]["adopted"], decisions[-1]["candidate_version"])
            self.assertEqual(report["branch"]["working"], report["branch"]["adopted"])
            for row in report["rounds"]:
                candidate = row["decision"]["candidate_version"]
                self.assertEqual(row["stage_record"]["asset_version"], candidate)
                admission = json.loads(
                    (root / row["stage"] / "candidate/admission.json").read_text()
                )
                self.assertEqual(admission["verdict"], "passed")
                self.assertEqual(admission["smoke"]["status"], "passed")
                for result in row["generation"]:
                    self.assertTrue(
                        all(p["asset_version"] == candidate for p in result["answer_provenance"])
                    )
                session = json.loads(Path(row["proposal_sessions"][0]).read_text())
                self.assertEqual(session["action"]["action"], "submit_patch")
                self.assertTrue(session["exchanges"], "Proposer must query the real Wiki service")
            calls = [
                json.loads(p.read_text()) for p in sorted((root / "model-calls").glob("*.json"))
            ]
            self.assertEqual({c["requested_model"] for c in calls}, {"glm-5.3-flash"})
            self.assertEqual(
                {c["request"]["role"] for c in calls},
                {"tools", "answer", "review", "wiki_maintainer", "proposal"},
            )
            self.assertTrue(
                all(c["state"] == "received" and c["response"]["content"] for c in calls)
            )
            optimization_calls = [
                c for c in calls if c["request"]["role"] in ("proposal", "wiki_maintainer")
            ]
            for call in optimization_calls:
                serialized = json.dumps(call["request"])
                self.assertNotIn('"oracle"', serialized)
                self.assertNotIn('"reference"', serialized)
            second_session = json.loads(
                (root / "R2/optimization/attempt-0/proposal-call.json").read_text()
            )
            self.assertEqual(
                second_session["input"]["base_version"], decisions[0]["candidate_version"]
            )

    def test_no_change_never_claims_actual_candidate_loop(self):
        with tempfile.TemporaryDirectory() as td:
            report = self.run_fixture(Path(td), no_change=True)
            self.assertIsNone(report["error"], report["error"])
            self.assertEqual(report["status"], "partial")
            self.assertFalse(report["loop_proven"])
            self.assertEqual(report["formal_candidate_rounds"], [])
            self.assertEqual([r["outcome"] for r in report["rounds"]], ["round_no_change"] * 2)
            self.assertEqual(report["candidate_answer_calls"], 0)

    def test_rejected_candidate_is_working_base_of_next_round(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = self.run_fixture(root, rounds=3, tie_second=True)
            self.assertIsNone(report["error"], report["error"])
            first, second, third = report["summary"]["rounds"]
            self.assertTrue(first["accepted"])
            self.assertFalse(second["accepted"])
            self.assertFalse(third["accepted"])
            self.assertEqual(report["branch"]["adopted"], first["candidate_version"])
            self.assertEqual(report["branch"]["working"], third["candidate_version"])
            session = json.loads(
                (root / "R3/optimization/attempt-0/proposal-call.json").read_text()
            )
            self.assertEqual(session["input"]["base_version"], second["candidate_version"])
            self.assertEqual(report["formal_candidate_rounds"], ["R1", "R2", "R3"])

    def test_live_rejects_unspecified_or_switched_model_before_connection(self):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch("scripts.smoke_live_loop.LLMClient") as client,
        ):
            for model in (None, "another-model"):
                with self.assertRaisesRegex(ValueError, "explicit user-specified"):
                    asyncio.run(execute(Path(td), mode="live", model=model))
            client.assert_not_called()

    def test_faulted_candidate_and_failed_runner_never_pass_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = self.run_fixture(root)
            transport = AuditTransport(root, Config(), replay=True)
            stage = root / "R2/stage.json"
            payload = json.loads(stage.read_text())
            payload["scores"]["generation_faults"] = 1
            stage.write_text(json.dumps(payload))
            failed = build_report(root, "replay", report["summary"], transport)
            self.assertFalse(failed["loop_proven"])
            self.assertEqual(failed["formal_candidate_rounds"], ["R1"])
            payload["scores"]["generation_faults"] = 0
            stage.write_text(json.dumps(payload))
            summary = dict(report["summary"], status="failed")
            failed = build_report(root, "replay", summary, transport)
            self.assertFalse(failed["loop_proven"])
            self.assertEqual(failed["status"], "partial")


if __name__ == "__main__":
    unittest.main()
