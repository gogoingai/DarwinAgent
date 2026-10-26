"""Exact trajectory values share definitions without interpreting source markers."""

import copy
import json
import unittest
from unittest.mock import patch

from datasets.lift import trace_values


class ValueReferenceTests(unittest.TestCase):
    def fixture(self):
        data = {"text": "完整材料\n" * 2000, "path": "a.txt", "literal": ["r", 999]}
        return [
            json.dumps({"output": data}, ensure_ascii=False),
            json.dumps(
                {"actions": [{"observation": data}], "view": {"keep": True}}, ensure_ascii=False
            ),
            "原始文本 " + ("重复资料片段 " * 1000),
            "原始文本 " + ("重复资料片段 " * 1000),
        ]

    def test_nested_duplicates_restore_every_source_byte_and_shrink(self):
        texts = self.fixture()
        before = copy.deepcopy(texts)
        encoded = trace_values.encode(texts)
        self.assertEqual(trace_values.decode(encoded), texts)
        self.assertEqual(texts, before)
        self.assertLess(sum(map(len, trace_values.render(encoded))), sum(map(len, texts)))

    def test_key_order_spacing_precise_numbers_and_tool_prefix_survive(self):
        texts = [
            "[toolCall] "
            + json.dumps({"name": "read", "args": {"p": "x" * 2000}}, ensure_ascii=False),
            '{"a":1,"b":2}',
            '{"b":2,"a":1}',
            '{ "precise":0.123456789012345678901 }',
            '{"duplicate":1,"duplicate":2}',
            '{"number":1e999}',
            '{"s":"[\\"r\\",999]", "fraction":0.5}',
            "null",
        ]
        self.assertEqual(trace_values.decode(trace_values.encode(texts)), texts)

    def test_reference_looking_source_data_remains_literal(self):
        texts = self.fixture() + [
            '{"protocol":"trace-values-v1","body":["r",99]}',
            '{"payload":["r",99]}',
            '["object",[["r",99]]]',
        ]
        self.assertEqual(trace_values.decode(trace_values.encode(texts)), texts)

    def test_rehashed_unknown_reference_and_changed_literal_are_rejected(self):
        from datasets.lift.trace_chunks import digest

        encoded = trace_values.encode(self.fixture())
        for body in (["r", 999999], ["literal", "forged trajectory"]):
            bad = copy.deepcopy(encoded)
            bad["frames"][-1]["body"] = body
            bad["sha256"] = digest({k: v for k, v in bad.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                trace_values.decode(bad)

    def test_rehashed_definition_chain_is_rejected_by_allocation_budget(self):
        from datasets.lift.trace_chunks import digest

        value = trace_values.encode(["x"])
        frame = value["frames"][0]
        frame["definitions"] = [[0, ["literal", "a" * 256]]] + [
            [i, ["text", [["r", 0]] * 4]] for i in range(1, 2001)
        ]
        value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
        with self.assertRaisesRegex(ValueError, "allocation"):
            trace_values.decode(value, max_source_bytes=1024)

    def test_source_and_expansion_resource_limits_reject_before_large_allocation(self):
        with self.assertRaises(ValueError):
            trace_values.encode(["a" * 1025], max_source_bytes=1024)
        encoded = trace_values.encode(self.fixture())
        with self.assertRaises(ValueError):
            trace_values.decode(encoded, max_source_bytes=1024)

    def test_decoder_rejects_oversized_envelope_before_digest(self):
        encoded = trace_values.encode(["original content"])
        with patch.object(trace_values, "MAX_ENCODED_BYTES", 16):
            with patch.object(trace_values, "digest", side_effect=AssertionError("too late")):
                with self.assertRaisesRegex(ValueError, "representation"):
                    trace_values.decode(encoded)

    def test_readable_display_keeps_unique_messages_and_exposes_actions_directly(self):
        from datasets.lift.readable_trace import render_readable

        data = {"text": "original material " * 1000}
        texts = [
            "simple final response",
            json.dumps({"tool": "read", "params": data}),
            json.dumps({"tool": "write", "params": data}),
            "host_ref(V0)",
        ]
        encoded = trace_values.encode(texts)
        before = copy.deepcopy(encoded)
        visible = render_readable(encoded)
        self.assertEqual(visible[0], texts[0])
        self.assertIn('"tool": "write"', visible[2])
        self.assertIn("host_ref(V", visible[2])
        self.assertNotIn('["object",', visible[2])
        self.assertIn('"host_ref(V0)"', visible[3])
        self.assertEqual(trace_values.decode(encoded), texts)
        self.assertEqual(encoded, before)
