"""Admission extraction preserves ordered checkpoints and interruption evidence."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput, GraphResult, QuestionInput
from darwinagent.experiments.admission import AdmissionError, admit_candidate
from darwinagent.kernel.assets import Asset, KernelAssets
from darwinagent.kernel.functions import FunctionRegistry
from darwinagent.runtime.artifacts import atomic_json
from tests.support.graphs import cold_bundle, corpus, gvtest_graph


class AdmissionBoundaryTests(unittest.TestCase):
    def bundle(self, root):
        seed = cold_bundle(root / "seed", with_c=False)
        function = next(a for a in seed.assets.assets if a.kind == "F")
        function = replace(
            function,
            content="def run(params):\n return nodes('原子事实',limit=2)",
            input_contract={"type": "object", "properties": {"rows": {"type": "array"}}},
            trial_inputs=({"rows": []},),
        )
        checks = tuple(
            Asset(
                name,
                "C",
                'def check(candidate):\n return {"ok": True, "issues": []}',
                stage="graph",
                schema_dependencies=["schema"],
            )
            for name in ("c_z", "c_a")
        )
        return KernelAssets(
            tuple(a for a in seed.assets.assets if a.kind != "F")
            + (replace(function, id="f_z"), replace(function, id="f_a"))
            + checks
        ).export(root / "candidate")

    def test_case_asset_replay_composition_order_and_write_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root)
            cases = [
                CaseInput(name, corpus(), (QuestionInput("q1", "事实？"),))
                for name in ("case-z", "case-a")
            ]
            graph = GraphResult(gvtest_graph(), {b.source.id: b for b in corpus()})
            writes = []

            def record(path, report):
                writes.append(json.loads(json.dumps(report)))
                atomic_json(path, report)

            with (
                patch("darwinagent.experiments.admission.atomic_json", side_effect=record),
                patch(
                    "darwinagent.experiments.admission_reporting.atomic_json", side_effect=record
                ),
                self.assertRaises(AdmissionError) as rejected,
            ):
                admit_candidate(
                    bundle,
                    cases,
                    {case.id: graph for case in cases},
                    RunConfig(),
                    ("nodes",),
                    root / "report.json",
                    replay_inputs=(("missing", {}), ("f_z", {"rows": []})),
                    replay_checks=(
                        {
                            "case_id": "case-z",
                            "question_id": "q1",
                            "snapshot": {"stage": "answer"},
                            "expectation": "must_reject",
                        },
                    ),
                )
            report = rejected.exception.report
            self.assertEqual(report, json.loads((root / "report.json").read_text()))
            self.assertEqual(report["scenarios"][0]["scenario_id"], "static")
            self.assertEqual(report["scenarios"][-1]["scenario_id"], "capability_floor")
            # Each case: initial checks, successful replay (including pending missing
            # asset), optional check replay, each sorted F, composition; final verdict.
            self.assertEqual(len(writes), 12)
            self.assertEqual(
                [row["asset_id"] for row in writes[0]["scenarios"]],
                ["bundle", "c_a", "c_z"],
            )
            self.assertEqual(
                [row["scenario_id"] for row in writes[1]["scenarios"][-2:]],
                ["replay", "replay"],
            )
            self.assertEqual(writes[1]["scenarios"][-2]["status"], "incomplete")
            self.assertEqual(writes[2]["scenarios"][-1]["scenario_id"], "check_replay_reject")
            self.assertEqual(list(writes[3]["counts"]["case-z"]), ["f_a"])
            self.assertEqual(list(writes[4]["counts"]["case-z"]), ["f_a", "f_z"])
            self.assertEqual(
                [row["input_ref"].split(":")[1] for row in writes[5]["scenarios"][-4:]],
                ["f_a->f_a", "f_a->f_z", "f_z->f_a", "f_z->f_z"],
            )
            case_refs = [
                row["input_ref"].split(":")[0] for row in report["scenarios"] if row["input_ref"]
            ]
            first_a = case_refs.index("case-a")
            self.assertTrue(all(ref == "case-z" for ref in case_refs[:first_a]))
            self.assertTrue(all(ref == "case-a" for ref in case_refs[first_a:]))
            for counts in report["counts"].values():
                for asset_counts in counts.values():
                    self.assertEqual(
                        asset_counts["executed"],
                        asset_counts["base"] + asset_counts["stress_legal"],
                    )

    def test_interruption_keeps_previous_asset_checkpoint(self):
        class WorkerStopped(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = self.bundle(root)
            case = CaseInput("case", corpus(), (QuestionInput("q1", "事实？"),))
            graph = GraphResult(gvtest_graph(), {b.source.id: b for b in corpus()})
            original_call = FunctionRegistry.call

            def stop_second(registry, aid, *args, **kwargs):
                if aid == "f_z":
                    raise WorkerStopped
                return original_call(registry, aid, *args, **kwargs)

            with (
                patch.object(FunctionRegistry, "call", new=stop_second),
                self.assertRaises(WorkerStopped),
            ):
                admit_candidate(
                    bundle, [case], {case.id: graph}, RunConfig(), (), root / "report.json"
                )
            saved = json.loads((root / "report.json").read_text())
            self.assertEqual(saved["verdict"], "incomplete")
            self.assertEqual(list(saved["counts"][case.id]), ["f_a"])
            self.assertEqual(saved["scenarios"][0]["scenario_id"], "static")
            self.assertEqual(
                {row["asset_id"] for row in saved["scenarios"]},
                {"bundle", "c_a", "c_z", "f_a"},
            )
            counts = saved["counts"][case.id]["f_a"]
            self.assertEqual(counts["executed"], counts["base"] + counts["stress_legal"])
