import copy
import json
import unittest

from datasets.lift.judge_projection import project_tool_observations, verify_projection


class JudgeReferenceProjectionTests(unittest.TestCase):
    def fixture(self):
        output = {"content": "完整文件内容\n" * 2000, "path": "a.html"}
        return [
            {"role": "assistant", "content": "规划和纠错必须保留"},
            {
                "role": "tool",
                "toolCallId": "turn-1/000002",
                "toolName": "read_text",
                "content": json.dumps({"output": output}, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "observation": output,
                        "view": {"state": "retained"},
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    def test_exact_repeated_output_is_referenced_and_raw_messages_unchanged(self):
        raw = self.fixture()
        before = copy.deepcopy(raw)
        projected, proof = project_tool_observations(raw)
        self.assertEqual(raw, before)
        self.assertEqual(projected[:2], raw[:2])
        self.assertEqual(len(proof["entries"]), 1)
        decoded = json.loads(projected[2]["content"])
        ref = decoded["observation"]["$darwin_lift_ref"]
        self.assertEqual(ref["tool_call_id"], "turn-1/000002")
        self.assertEqual(ref["message_index"], 1)
        self.assertEqual(decoded["view"], {"state": "retained"})
        self.assertEqual(len(proof["entries"]), 1)
        self.assertLess(len(projected[2]["content"]), len(raw[2]["content"]))
        verify_projection(raw, projected, proof)

    def test_different_output_macro_error_and_ambiguous_ids_not_projected(self):
        for kind in ("different", "macro", "failure", "duplicate_id", "intervening"):
            raw = self.fixture()
            payload = json.loads(raw[2]["content"])
            if kind == "different":
                payload["observation"]["path"] = "different.html"
            if kind == "macro":
                payload["observation"] = {"first": payload["observation"], "second": "macro"}
            if kind == "failure":
                raw[1]["content"] = json.dumps({"failure": "error"})
            if kind == "duplicate_id":
                raw.insert(0, copy.deepcopy(raw[1]))
            if kind == "intervening":
                raw.insert(2, {"role": "assistant", "content": "new decision"})
            raw[-1]["content"] = json.dumps(payload, ensure_ascii=False)
            projected, proof = project_tool_observations(raw)
            self.assertEqual(projected, raw, kind)
            self.assertEqual(proof["entries"], [], kind)

    def test_tampered_projection_and_manifest_are_rejected(self):
        raw = self.fixture()
        projected, proof = project_tool_observations(raw)
        bad = copy.deepcopy(projected)
        bad[0]["content"] = "hidden regression"
        with self.assertRaises(ValueError):
            verify_projection(raw, bad, proof)
        bad_proof = copy.deepcopy(proof)
        bad_proof["entries"][0]["source_index"] = 0
        with self.assertRaises(ValueError):
            verify_projection(raw, projected, bad_proof)

    def test_small_outputs_and_ordinary_messages_preserve_bytes(self):
        raw = [
            {"role": "tool", "toolCallId": "small", "toolName": "read", "content": '{"output":1}'},
            {"role": "user", "content": '{ "observation": {"tool":"read","output":1} }'},
            {"role": "user", "content": "native correction remains"},
        ]
        projected, proof = project_tool_observations(raw)
        self.assertEqual(projected, raw)
        self.assertFalse(proof["entries"])

    def test_float_precision_overflow_and_retained_view_are_never_rewritten(self):
        for first, second, view in (
            ("0.12345678901234567890", "0.12345678901234567899", "1"),
            ("1e999", "2e999", "1"),
            ("0.5", "0.5", "1"),
            ("1", "1", "0.12345678901234567890"),
            ("1", "true", "1"),
        ):
            text = json.dumps("large repeated output" * 200)
            tool = '{"output":{"content":' + text + ',"n":' + first + "}}"
            observation = (
                '{"observation":{"content":'
                + text
                + ',"n":'
                + second
                + '},"view":{"precise":'
                + view
                + "}}"
            )
            raw = [
                {"role": "tool", "toolCallId": "n", "toolName": "read", "content": tool},
                {"role": "user", "content": observation},
            ]
            projected, proof = project_tool_observations(raw)
            self.assertEqual(projected, raw, (first, second, view))
            self.assertEqual(proof["entries"], [])
