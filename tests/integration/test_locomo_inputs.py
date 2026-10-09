"""Fresh dataset input preparation and dynamic cold-start vocabulary."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, QuestionInput
from darwinagent.experiments.bootstrap import AssetBootstrapper
from darwinagent.kernel import TaskSpec
from darwinagent.kernel.registration import load_assets
from darwinagent.llm.recorded import RecordedClient
from datasets.locomo.inputs import prepare_memory, resolve_dataset
from tests.support.device import TASK, case
from tests.support.facts import block, fact_reply


class FreshInputTests(unittest.IsolatedAsyncioTestCase):
    def test_hf_input_pins_revision_and_never_falls_back_to_local_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache" / "snapshots" / ("a" * 40) / "locomo"
            cache.mkdir(parents=True)
            for name in ("locomo10_zh.json", "locomo10.json"):
                (cache / name).write_text("[]")
            args = SimpleNamespace(dataset_repo="org/data", dataset_revision="main")
            api = mock.Mock()
            api.dataset_info.return_value.sha = "a" * 40
            hub = SimpleNamespace(
                HfApi=mock.Mock(return_value=api),
                hf_hub_download=mock.Mock(
                    side_effect=lambda **kw: str(cache / Path(kw["filename"]).name)
                ),
            )
            with mock.patch.dict("sys.modules", {"huggingface_hub": hub}):
                self.assertEqual(resolve_dataset(args, root / "run"), cache.resolve())
                api.dataset_info.assert_called_once_with("org/data", revision="main")
                self.assertEqual(hub.hf_hub_download.call_count, 2)
                self.assertTrue(
                    all(
                        c.kwargs["revision"] == "a" * 40 for c in hub.hf_hub_download.call_args_list
                    )
                )
                api.reset_mock()
                resolve_dataset(args, root / "run")
                api.dataset_info.assert_not_called()
                saved = json.loads((root / "run" / "dataset-source.json").read_text())
                self.assertEqual(saved["resolved_revision"], "a" * 40)
                moved = root / "another-cache" / "locomo"
                moved.mkdir(parents=True)
                for name in ("locomo10_zh.json", "locomo10.json"):
                    (moved / name).write_bytes((cache / name).read_bytes())
                hub.hf_hub_download.side_effect = lambda **kw: str(
                    moved / Path(kw["filename"]).name
                )
                self.assertEqual(resolve_dataset(args, root / "run"), moved.resolve())
                self.assertEqual(
                    json.loads((root / "run" / "dataset-source.json").read_text()), saved
                )
                hub.hf_hub_download.side_effect = lambda **kw: str(
                    cache / Path(kw["filename"]).name
                )
                (cache / "locomo10_zh.json").write_text("[{}]")
                with self.assertRaisesRegex(ValueError, "Dataset contents changed"):
                    resolve_dataset(args, root / "run")
            with self.assertRaisesRegex(ValueError, "--dataset-repo.*--data-dir"):
                resolve_dataset(SimpleNamespace(), root / "other")

    async def test_memory_extraction_excludes_questions_and_reuses_facts(self):
        b = block("D1:1", "我下周修打印机。")
        c = CaseInput("conv-t", (b,), (QuestionInput("q", "不应传给抽取的题目"),))
        client = RecordedClient({"extraction": [fact_reply("m0", "我下周修打印机")]})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = RunConfig(protocol_attempts=1)
            manifest = await prepare_memory(c, root, client, config)
            raw = (root / c.id / "facts.jsonl").read_bytes()
            restarted = RecordedClient({})
            await prepare_memory(
                replace(c, questions=(QuestionInput("q2", "另一个问题"),)), root, restarted, config
            )
            self.assertEqual(restarted.calls, [])
            self.assertEqual((root / c.id / "facts.jsonl").read_bytes(), raw)
            self.assertEqual(manifest["vector_mode"], "none")
            self.assertNotIn("不应传给抽取的题目", json.dumps(client.calls, ensure_ascii=False))
            (root / c.id / "facts.jsonl").write_text("{}\n")
            with self.assertRaisesRegex(ValueError, "facts changed"):
                await prepare_memory(c, root, restarted, config)

    async def test_dynamic_bootstrap_can_declare_types_absent_from_initial_sample(self):
        assets = load_assets(TASK)
        schema = next(a for a in assets.assets if a.kind == "S")
        raw = yaml.safe_load(schema.content)
        raw.setdefault("meta", {})["atomic_memory_type"] = "Maintenance"
        raw["entity_types"]["Maintenance"]["attributes"].extend(
            [{"name": "编号", "dtype": "string"}, {"name": "陈述", "dtype": "string"}]
        )
        raw["entity_types"]["Part"] = {
            "primary_key": ["name"],
            "attributes": [{"name": "name", "dtype": "string"}],
        }
        reply = {
            "assets": [
                replace(a, content=yaml.safe_dump(raw)).to_dict() if a.kind == "S" else a.to_dict()
                for a in assets.assets
            ]
        }
        client = RecordedClient({"bootstrap": [reply]})
        with tempfile.TemporaryDirectory() as tmp:
            bundle = await AssetBootstrapper().initialize(
                case(),
                TaskSpec.load(TASK / "task.yaml"),
                client,
                RunConfig(protocol_attempts=1),
                Path(tmp),
                structure_sample={"node_types": {"Maintenance": 1}, "graph_mode": "llm"},
            )
            self.assertIn("Part", next(a.content for a in bundle.assets.assets if a.kind == "S"))
            protocol = client.calls[0]["messages"][0]["content"]
            self.assertNotIn("The frozen memory graph already exists", protocol)
            self.assertNotIn("at least one semantic vector-retrieval F", protocol)

    def test_evaluation_input_identity_survives_cache_relocation(self):
        import hashlib

        from datasets.locomo.evaluator import LocomoEvaluator
        from tests.support.locomo import fixture_dataset

        with tempfile.TemporaryDirectory() as tmp:
            first = fixture_dataset(Path(tmp) / "first")
            moved = fixture_dataset(Path(tmp) / "moved")
            hashes = {
                name: hashlib.sha256((first / name).read_bytes()).hexdigest()
                for name in ("locomo10_zh.json", "locomo10.json")
            }
            evaluator = LocomoEvaluator(
                None,
                Path(tmp) / "eval",
                dataset_path=moved / "locomo10_zh.json",
                original_only=True,
                dataset_hashes=hashes,
            )
            evaluator._verify_inputs()
            (moved / "locomo10_zh.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "Frozen dataset contents changed"):
                evaluator._verify_inputs()
