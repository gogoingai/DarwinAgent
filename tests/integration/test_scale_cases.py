"""Independent offline audit of scale-smoke sources, references and page contracts."""

import copy
import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import smoke_extended as extended  # noqa: E402
import smoke_scale as scale_run  # noqa: E402
import smoke_scale_cases as scale  # noqa: E402
import smoke_stress as stress  # noqa: E402

from darwinagent.contracts import QuestionInput  # noqa: E402


def independent_reference(text, serial, records):
    """Interpret the actual Chinese question, without Query/reference helper calls."""
    selected = [(day, person) for device, day, person in records if device == serial]
    exact_date = re.search(r"在(\d{4}-\d{2}-\d{2})由谁", text)
    month = re.search(r"在(\d{4}-\d{2})这个月", text)
    cutoff = re.search(r"在(\d{4}-\d{2}-\d{2})之后（不含当天）", text)
    person = re.search(r"匹配。(.+?)对" + re.escape(serial) + r"执行维护", text)
    if exact_date:
        selected = [(day, who) for day, who in selected if day == exact_date[1]]
    if month:
        selected = [(day, who) for day, who in selected if day[:7] == month[1]]
    if cutoff:
        selected = [(day, who) for day, who in selected if day > cutoff[1]]
    if person:
        selected = [(day, who) for day, who in selected if who == person[1]]
    if "返回count" in text:
        return {"count": len(selected)}
    if not selected:
        return None
    if "technicians数组" in text:
        return {"technicians": sorted({who for _, who in selected})}
    selected.sort()
    day, who = selected[0] if "最早一次" in text else selected[-1]
    return {"technician": who, "date": day}


class ScaleCasePreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.definitions = scale.definitions()
        with patch.object(extended.base, "LLMClient", side_effect=AssertionError("No model calls")):
            cls.cases = extended.fixtures(Path(cls.folder.name), cls.definitions)

    def test_24_cases_72_unique_questions_distinct_from_prior_12(self):
        prior = stress.definitions()
        self.assertEqual(len(self.definitions), 24)
        self.assertEqual(sum(len(questions) for _, _, questions in self.definitions), 72)
        names = [name for name, _, _ in self.definitions]
        qids = [qid for _, _, questions in self.definitions for qid, *_ in questions]
        self.assertEqual(len(set(names)), 24)
        self.assertEqual(len(set(qids)), 72)
        self.assertTrue(all(name.startswith("scale-") for name in [*names, *qids]))
        prior_names = {name for name, _, _ in prior}
        prior_serials = {serial for _, records, _ in prior for serial, _, _ in records}
        prior_records = {row for _, records, _ in prior for row in records}
        self.assertTrue(set(names).isdisjoint(prior_names))
        for _, records, questions in self.definitions:
            self.assertEqual(len(questions), 3)
            self.assertTrue(0 < len(records) <= 150)
            self.assertEqual(len({(serial, day) for serial, day, _ in records}), len(records))
            self.assertTrue(set(records).isdisjoint(prior_records))
            self.assertTrue({serial for serial, _, _ in records}.isdisjoint(prior_serials))
            self.assertTrue({serial for _, _, serial, _ in questions}.isdisjoint(prior_serials))

    def test_correct_field_answer_does_not_hide_skipped_paging_or_reset(self):
        cid, _, questions = next(c for c in self.definitions if c[0] == "scale-out-of-range-reset")
        qid = questions[0][0]
        audit = {
            "rows": [
                {
                    "case_id": cid,
                    "question_id": qid,
                    "passed": True,
                    "tools": [
                        {
                            "asset_id": "f_page_facts",
                            "parameters": {"offset": 0, "limit": 100},
                            "control": {"more_remain": False, "next_offset": 10},
                        },
                    ],
                }
            ]
        }
        checked = scale_run.audit_page_execution(audit, self.definitions)
        self.assertEqual(checked["passed"], 0)
        self.assertTrue(checked["rows"][0]["field_passed"])
        self.assertIn("out_of_range_not_reset", checked["rows"][0]["faults"])
        audit["rows"][0]["tools"] = [
            {
                "asset_id": "f_page_facts",
                "parameters": {"offset": 999, "limit": 20},
                "control": {"more_remain": False, "next_offset": 999},
            },
            {
                "asset_id": "f_page_facts",
                "parameters": {"offset": 0, "limit": 20},
                "control": {"more_remain": False, "next_offset": 10},
            },
        ]
        self.assertEqual(scale_run.audit_page_execution(audit, self.definitions)["passed"], 1)

    def test_all_references_match_actual_question_text_and_original_records(self):
        kinds = set()
        for _, _, queries in scale.specifications():
            kinds.update(q.kind for q in queries)
        self.assertEqual(
            kinds,
            {
                "count",
                "latest",
                "earliest",
                "technicians",
                "date",
                "month-count",
                "month-technicians",
                "person-latest",
                "after-count",
            },
        )
        for case_id, records, questions in self.definitions:
            for qid, text, serial, expected in questions:
                with self.subTest(case=case_id, question=qid):
                    self.assertIn(serial, text)
                    self.assertEqual(independent_reference(text, serial, records), expected)
                    if expected is not None:
                        self.assertTrue(
                            set(expected) <= {"count", "date", "technician", "technicians"}
                        )
                        if "technicians" in expected:
                            self.assertEqual(
                                len(expected["technicians"]), len(set(expected["technicians"]))
                            )
                        if "count" in expected:
                            self.assertIs(type(expected["count"]), int)
        by_name = {name: questions for name, _, questions in self.definitions}
        self.assertEqual(
            by_name["scale-page-21"][1][3], {"date": "2028-01-21", "technician": "尾页员"}
        )
        self.assertEqual(
            by_name["scale-leap-day"][0][3], {"date": "2028-02-29", "technician": "何"}
        )
        self.assertEqual(by_name["scale-exclusive-cutoff"][0][3], {"count": 2})
        self.assertEqual(by_name["scale-interleaved-month-person"][0][3], {"count": 10})
        self.assertIsNone(by_name["scale-absent-date"][0][3])

    def test_original_fixture_graph_facts_source_and_generation_boundary(self):
        report = scale.audit_fixtures(self.cases, self.definitions)
        self.assertEqual(
            report,
            {
                "cases": 24,
                "questions": 72,
                "records": 533,
                "max_records_per_case": 101,
                "synthetic": True,
                "model_calls": 0,
            },
        )
        for (case, snapshot, _, _, references), (_, records, questions) in zip(
            self.cases, self.definitions, strict=True
        ):
            payload = case.to_dict()
            self.assertEqual(set(payload), {"id", "corpus", "questions"})
            self.assertNotIn("expected", json.dumps(payload))
            self.assertNotIn("reference", json.dumps(payload))
            self.assertEqual(
                case.corpus[0].text.splitlines(),
                [f"设备{s}于{d}由{t}维护。" for s, d, t in records],
            )
            self.assertEqual(
                len(json.loads((snapshot / "graph.json").read_text())["nodes"]), len(records)
            )
            self.assertEqual(len((snapshot / "facts.jsonl").read_text().splitlines()), len(records))
            self.assertEqual(len(references), 3)
            for item, (qid, text, serial, _) in zip(payload["questions"], questions, strict=True):
                self.assertEqual(item, {"id": qid, "text": text, "parameters": {"serial": serial}})

    def test_registered_page_function_on_all_boundaries_empty_and_reset(self):
        sizes = (1, 19, 20, 21, 39, 40, 41, 75, 101)
        longest = 0
        for (case, snapshot, bundle, _, _), (_, records, questions) in zip(
            self.cases, self.definitions, strict=True
        ):
            graph_nodes = json.loads((snapshot / "graph.json").read_text())["nodes"]

            def nodes(etype, where, limit=20, graph_nodes=graph_nodes):
                return [
                    row
                    for row in graph_nodes
                    if row["etype"] == etype
                    and all(row.get(key) == value for key, value in where.items())
                ][:limit]

            namespace = {"nodes": nodes}
            exec(bundle.get("f_page_facts").content, namespace)
            page = namespace["run"]
            for _, text, serial, _ in questions:
                if "f_page_facts" not in text:
                    continue
                self.assertIn("offset=0", text)
                self.assertIn("limit=20", text)
                selected = [row for row in graph_nodes if row["serial"] == serial]
                calls = 0
                if "offset=999" in text:
                    empty = page({"serial": serial, "offset": 999, "limit": 20})
                    self.assertEqual(empty["rows"], [])
                    self.assertFalse(empty["more_remain"])
                    self.assertEqual(empty["matched_count"], len(selected))
                    calls += 1
                returned = []
                offset = 0
                while True:
                    response = page({"serial": serial, "offset": offset, "limit": 20})
                    calls += 1
                    self.assertEqual(response["rows"], selected[offset : offset + 20])
                    self.assertEqual(response["matched_count"], len(selected))
                    self.assertEqual(response["next_offset"], offset + len(response["rows"]))
                    returned.extend(response["rows"])
                    if not response["more_remain"]:
                        break
                    self.assertGreater(response["next_offset"], offset)
                    offset = response["next_offset"]
                self.assertEqual(returned, selected)
                self.assertLessEqual(calls + 1, 16)  # Includes ready; no live model involved.
                longest = max(longest, calls + 1)
            if case.id.startswith("scale-page-"):
                n = int(case.id.removeprefix("scale-page-"))
                self.assertIn(n, sizes)
                self.assertEqual(sum(row[0] == f"SC-P{n}" for row in records), n)
        self.assertEqual(longest, 7)

    def test_preflight_rejects_controller_reference_and_question_tampering(self):
        original = copy.deepcopy(self.definitions)
        original[0][2][0] = (*original[0][2][0][:3], {"count": 99})
        with self.assertRaisesRegex(ValueError, "definitions differ"):
            scale.audit_fixtures(self.cases, original)
        altered = list(self.cases)
        case, *rest = altered[0]
        q = case.questions[0]
        for replacement in (
            QuestionInput(q.id, q.text + "错误问题", q.parameters),
            QuestionInput(
                q.id, q.text, {"serial": q.parameters["serial"], "expected": {"count": 1}}
            ),
        ):
            altered[0] = (replace(case, questions=(replacement, *case.questions[1:])), *rest)
            with self.assertRaisesRegex(ValueError, "Original question mismatch"):
                scale.audit_fixtures(altered)
        altered = list(self.cases)
        altered[0] = (*altered[0][:4], {"bad-reference": {"count": 1}})
        with self.assertRaisesRegex(ValueError, "Controller reference mismatch"):
            scale.audit_fixtures(altered)

    def test_preflight_rejects_original_graph_fact_and_manifest_tampering(self):
        snapshot = self.cases[0][1]
        for filename, mutate, message in (
            (
                "graph.json",
                lambda data: data["nodes"][0].update(technician="伪造"),
                "Original graph mismatch",
            ),
            ("manifest.json", lambda data: data.update(n_facts=999), "Original manifest mismatch"),
        ):
            path = snapshot / filename
            original = path.read_text()
            data = json.loads(original)
            mutate(data)
            try:
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, message):
                    scale.audit_fixtures(self.cases)
            finally:
                path.write_text(original)
        path = snapshot / "facts.jsonl"
        original = path.read_text()
        try:
            path.write_text(original.replace("林", "伪造"))
            with self.assertRaisesRegex(ValueError, "Original facts mismatch"):
                scale.audit_fixtures(self.cases)
        finally:
            path.write_text(original)


if __name__ == "__main__":
    unittest.main()
