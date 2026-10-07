"""Offline regression scenarios for checkpoints."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.support.graphs import cold_bundle


class CurrentAssetReferenceRegression(unittest.TestCase):
    def payload(self, base):
        asset = next(a for a in base.assets.assets if a.kind == "P")
        return {
            "patches": [
                {
                    "asset": asset.to_dict(),
                    "base_fingerprint": "current:" + asset.id,
                    "reason": "Verified current base reference",
                    "training_evidence": ["6:conv-x::q1"],
                }
            ]
        }

    def test_reference_resolves_exact_current_fingerprint_and_saved_reply(self):
        from darwinagent.experiments.proposal import ProposalGenerator
        from darwinagent.experiments.runner import ExperimentRunner

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            patch = ProposalGenerator.decode(obj, base)[0]
            original = next(a for a in base.assets.assets if a.id == patch.asset.id)
            self.assertEqual(patch.base_fingerprint, original.fingerprint)
            self.assertTrue(ExperimentRunner._valid_proposal_raw(json.dumps(obj), base))
            self.assertFalse(ExperimentRunner._valid_proposal_raw(json.dumps(obj)))

    def test_reference_cannot_select_other_asset_or_change_kind(self):
        from darwinagent.experiments.proposal import ProposalGenerator

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            obj["patches"][0]["base_fingerprint"] = "current:unknown"
            with self.assertRaisesRegex(ValueError, "reference"):
                ProposalGenerator.decode(obj, base)
            obj = self.payload(base)
            obj["patches"][0]["asset"]["kind"] = "F"
            obj["patches"][0]["asset"]["role"] = ""
            obj["patches"][0]["asset"]["trial_inputs"] = [{}]
            with self.assertRaisesRegex(ValueError, "type change"):
                ProposalGenerator.decode(obj, base)

    def test_literal_wrong_hash_is_rejected_and_legacy_literal_remains_valid(self):
        from darwinagent.experiments.proposal import ProposalGenerator

        with tempfile.TemporaryDirectory() as tmp:
            base = cold_bundle(Path(tmp) / "base", with_c=False)
            obj = self.payload(base)
            obj["patches"][0]["base_fingerprint"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "Wrong fingerprint"):
                ProposalGenerator.decode(obj, base)
            asset = next(a for a in base.assets.assets if a.id == obj["patches"][0]["asset"]["id"])
            obj["patches"][0]["base_fingerprint"] = asset.fingerprint
            self.assertEqual(ProposalGenerator.decode(obj)[0].base_fingerprint, asset.fingerprint)
