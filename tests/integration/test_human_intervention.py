import tempfile
import unittest
from pathlib import Path

from darwinagent.experiments.control import intervene
from darwinagent.runtime.workspace import SelectionConflict, Workspace
from tests.support.device import spec


class HumanIntervention(unittest.TestCase):
    def test_ordinary_asset_draft_needs_no_model_training_or_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = spec(root / "original")
            result = intervene(
                root, {"kind": "assets", "assets": [a.to_dict() for a in task.bundle.assets.assets]}
            )
            self.assertIsNone(result["draft_error"])
            self.assertTrue(
                (root / "versions" / result["branch"]["working"] / "manifest.json").exists()
            )
            self.assertIsNone(result["branch"]["adopted"])
            self.assertEqual(
                Workspace(root / "workspace").events(kind="selection")[0]["payload"]["source"],
                "human",
            )

    def test_invalid_draft_saved_and_previous_candidate_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = intervene(root, {"kind": "select", "candidate": "existing"})
            failed = intervene(root, {"kind": "assets", "assets": [{"id": "broken"}]})
            self.assertTrue(failed["draft_error"])
            self.assertEqual(failed["branch"]["working"], selected["branch"]["working"])
            self.assertEqual(
                Workspace(root / "workspace").read_json(failed["draft_ref"])["assets"],
                [{"id": "broken"}],
            )

    def test_secrets_replaced_with_change_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = intervene(
                root,
                {
                    "kind": "model",
                    "connection_config": {
                        "api_key": "do-not-persist",
                        "model_strong": "user-specified",
                    },
                },
            )
            stored = Workspace(root / "workspace").read_json(result["draft_ref"])
            self.assertTrue(stored["connection_config"]["api_key_changed"])
            self.assertNotIn("do-not-persist", repr(stored))
            self.assertFalse(
                any(
                    b"do-not-persist" in path.read_bytes()
                    for path in (root / "workspace").rglob("*")
                    if path.is_file()
                )
            )

    def test_late_automatic_selection_does_not_overwrite_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = intervene(root, {"kind": "select", "candidate": "first"})
            intervene(root, {"kind": "select", "candidate": "human"})
            workspace = Workspace(root / "workspace")
            with self.assertRaises(SelectionConflict):
                workspace.publish_selection(
                    "main", expected_revision=initial["branch"]["revision"], working="automatic"
                )
            self.assertEqual(workspace.branch("main")["working"], "human")
