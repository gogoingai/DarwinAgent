"""Independent confirmation is sealed from development; uses only fake q6 fixtures."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from darwinagent.config import Config
from darwinagent.runtime.artifacts import atomic_json, digest
from datasets.lift.data import EALE_REVISION, LiftConfirmationDataset, LiftDataset, sha

try:
    import src.lift  # noqa: F401
except ModuleNotFoundError:
    NATIVE = False
else:
    NATIVE = True


@unittest.skipUnless(NATIVE, "Pinned native environment required")
class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_q6_only_separate_identity_selection_and_no_training(self):
        from datasets.lift.kernel import initial_kernel
        from datasets.lift.local import LocalRunner
        from datasets.lift.tests.offline_local import infrastructure_guard

        class Client:
            def __init__(self, root):
                self.cfg = Config(
                    model_strong="recorded",
                    model_middle="recorded",
                    model_fast="recorded",
                    max_http_requests=100,
                    model_profiles=False,
                    work_dir=root,
                )
                self.calls = []

            def http_attempts(self):
                return 0

            async def chat(self, **kwargs):
                request = json.loads(kwargs["messages"][1]["content"])
                self.calls.append((kwargs["role"], request["task"]["id"]))
                assert request["task"]["id"] == "q6"
                assert kwargs["role"] in {"worker", "judge"}
                if kwargs["role"] == "worker":
                    assert "PRIVATE_CRITERION" not in json.dumps(kwargs["messages"])
                    answer = "done"
                else:
                    assert "PRIVATE_CRITERION" in json.dumps(kwargs["messages"])
                    assert "write_text" not in {t["name"] for t in request["tools"]}
                    answer = json.dumps({"success": True, "score": 1, "reason": ""})
                return SimpleNamespace(
                    content=json.dumps({"final_response": answer, "artifacts": []}),
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )

        with tempfile.TemporaryDirectory() as d, infrastructure_guard():
            root = Path(d)
            data = root / "confirmation-data"
            task = data / "fixture/q6_fixture/q6_fixture.md"
            task.parent.mkdir(parents=True)
            task.write_text(
                "### query\nIndependent fixture deliverable\n### 要求\nPRIVATE_CRITERION\n### 轨迹要求\n\n"
            )
            notes = task.parent / "q6_materials/notes.txt"
            notes.parent.mkdir()
            notes.write_text("public fixture only")
            atomic_json(
                data / "confirmation-manifest.json",
                {
                    "revision": EALE_REVISION,
                    "q6": "authorized_confirmation",
                    "files": [
                        {"path": str(p.relative_to(data)), "sha256": sha(p)} for p in (task, notes)
                    ],
                },
            )
            dataset = LiftConfirmationDataset(data)
            with self.assertRaises(PermissionError):
                dataset.write_suite(root / "must-not-train.json")
            with self.assertRaises((ValueError, FileNotFoundError)):
                LiftDataset(data)
            with self.assertRaises(ValueError):
                dataset.task("q1")
            client = Client(root / "client")
            from datasets.lift.tests.fixtures import worker16_fixture

            shared = worker16_fixture(client, root / "budget/shared")
            origin = root / "development"
            initial = initial_kernel(origin / "K0")
            with self.assertRaisesRegex(ValueError, "training"):
                LocalRunner(
                    dataset=dataset,
                    shared=shared,
                    root=root / "wrong",
                    initial=initial,
                    run_id="wrong",
                    max_tokens=16384,
                )
            runner = LocalRunner(
                dataset=dataset,
                shared=shared,
                root=root / "confirmation",
                initial=initial,
                run_id="fixture-confirmation",
                mode="confirmation",
                max_tokens=16384,
                selection_run_root=origin,
            )
            atomic_json(
                origin / "integration-identity.json",
                {
                    **runner.frozen_identity,
                    "mode": "local-native-task-adaptation-v1",
                    "selection_run_root": None,
                    "run_id": "fixture-development",
                },
            )
            atomic_json(
                origin / "selected-state.json",
                {
                    "bundle_root": str(initial.root),
                    "kernel_id": initial.content_id,
                    "fallback": True,
                    "source_sha256": digest(runner.source),
                },
            )
            atomic_json(
                origin / "all-pair-report.json",
                {
                    "status": "complete",
                    "rows": [
                        {"task_id": "q5", "arm": arm, "status": "completed"}
                        for arm in ("A", "B", "C")
                    ],
                },
            )
            result = await runner.run()
            self.assertEqual(result["comparison_task"], "q6")
            self.assertEqual(result["q6"], "independent_confirmation")
            self.assertFalse(result["q5_verification_complete"])
            self.assertFalse(result["evolution_adopted"])
            self.assertEqual({r["arm"] for r in result["rows"]}, {"A", "B", "C"})
            self.assertEqual({r["task_id"] for r in result["rows"]}, {"q6"})
            self.assertEqual(len({r["native"]["workspace_dir"] for r in result["rows"]}), 3)
            self.assertFalse((runner.root / "campaign").exists())
            for row in result["rows"]:
                workspace = Path(row["native"]["workspace_dir"])
                self.assertEqual(
                    {str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()},
                    {"q6_materials/notes.txt"},
                )
            calls = len(client.calls)
            await runner.run()
            self.assertEqual(len(client.calls), calls)
