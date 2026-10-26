"""Nested receipt references preserve complete evidence and literal markers."""

import copy
import json
import unittest

from datasets.lift import judge_projection as projection


def nested_messages():
    output = {"content": "材料\n" * 1500, "path": "a.txt"}
    tool = {
        "role": "tool",
        "toolName": "read_text",
        "toolCallId": "t/2",
        "content": json.dumps({"output": output}, ensure_ascii=False),
    }
    observation = {
        "actions": [{"tool": "read_text", "params": {"path": "a.txt"}, "observation": output}],
        "stopped": False,
    }
    return [
        tool,
        {"role": "assistant", "content": "retain intervening decision"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "observation": observation,
                    "view": {"literal": {"$darwin_lift_ref": {"message_index": 999}}},
                },
                ensure_ascii=False,
            ),
        },
    ]


class NestedProjectionTests(unittest.TestCase):
    def project(self, raw):
        return projection.project_nested_tool_observations(raw)

    def test_nested_result_references_prior_receipt_and_exactly_restores_bytes(self):
        raw = nested_messages()
        before = copy.deepcopy(raw)
        projected, proof = self.project(raw)
        self.assertEqual(raw, before)
        self.assertEqual(projected[:2], raw[:2])
        self.assertEqual(len(proof["entries"]), 1)
        self.assertEqual(
            proof["entries"][0]["target_path"], ["observation", "actions", 0, "observation"]
        )
        self.assertEqual(projection.restore_nested_projection(projected, proof), raw)
        projection.verify_nested_projection(raw, projected, proof)
        self.assertLess(len(projected[2]["content"]), len(raw[2]["content"]))
        self.assertEqual(
            json.loads(projected[2]["content"])["view"], json.loads(raw[2]["content"])["view"]
        )

    def test_future_ambiguous_failure_and_noncanonical_messages_are_not_referenced(self):
        for mode in ("future", "duplicate", "failure", "noncanonical", "float"):
            raw = nested_messages()
            if mode == "future":
                raw.append(raw.pop(0))
            if mode == "duplicate":
                raw.insert(1, copy.deepcopy(raw[0]))
            if mode == "failure":
                raw[0]["content"] = raw[0]["content"].replace('"output"', '"failure"', 1)
            if mode == "noncanonical":
                raw[-1]["content"] = " " + raw[-1]["content"]
            if mode == "float":
                raw[-1]["content"] = raw[-1]["content"][:-1] + ', "n": 0.1234567890123456789}'
            projected, proof = self.project(raw)
            self.assertEqual(projected, raw, mode)
            self.assertEqual(proof["entries"], [], mode)

    def test_identical_marker_in_unregistered_path_remains_literal(self):
        raw = nested_messages()
        obj = json.loads(raw[-1]["content"])
        obj["observation"]["untrusted"] = {
            "$darwin_lift_ref": {"message_index": 0, "tool_call_id": "t/2", "path": ["output"]}
        }
        raw[-1]["content"] = json.dumps(obj, ensure_ascii=False)
        projected, proof = self.project(raw)
        self.assertEqual(projection.restore_nested_projection(projected, proof), raw)
        self.assertEqual(
            json.loads(projected[-1]["content"])["observation"]["untrusted"],
            obj["observation"]["untrusted"],
        )

    def test_different_object_key_order_preserves_original_observation_bytes(self):
        raw = nested_messages()
        payload = json.loads(raw[-1]["content"])
        output = payload["observation"]["actions"][0]["observation"]
        payload["observation"]["actions"][0]["observation"] = dict(reversed(output.items()))
        raw[-1]["content"] = json.dumps(payload, ensure_ascii=False)
        projected, proof = self.project(raw)
        self.assertEqual(projected, raw)
        self.assertEqual(proof["entries"], [])
        self.assertEqual(projection.restore_nested_projection(projected, proof), raw)

    def test_tampered_target_source_and_path_are_rejected(self):
        raw = nested_messages()
        projected, proof = self.project(raw)
        for kind in ("target", "source", "path"):
            bad, manifest = copy.deepcopy(projected), copy.deepcopy(proof)
            if kind == "target":
                bad[-1]["content"] = bad[-1]["content"].replace(
                    '"stopped": false', '"stopped": true'
                )
            if kind == "source":
                bad[0]["content"] += " "
            if kind == "path":
                manifest["entries"][0]["target_path"] = ["view"]
            with self.assertRaises(ValueError, msg=kind):
                projection.verify_nested_projection(raw, bad, manifest)
            with self.assertRaises(ValueError, msg=kind):
                projection.restore_nested_projection(bad, manifest)


class NestedNativeInputTests(unittest.TestCase):
    def setUp(self):
        try:
            from src.postprocess import judge
        except ModuleNotFoundError as exc:
            if exc.name not in {"src", "src.lift"}:
                raise
            raise unittest.SkipTest("Optional pinned LIFT upstream is not configured") from None
        import hashlib
        from pathlib import Path

        from darwinagent.runtime.artifacts import digest

        self.judge = judge
        messages = nested_messages()
        # Empty content does not consume a chunk content_index.
        messages.insert(0, {"role": "assistant", "content": None})
        value = {
            "identity": {"task_id": "synthetic", "kernel_id": "fixed"},
            "episodes": [{"identity": {"task": {"instruction": "original task"}}}],
            "all_messages": messages,
            "source_hashes": {},
        }
        self.transcript = dict(value, sha256=digest(value))
        path = str(Path(judge.__file__).resolve())
        self.sources = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()}

    def build(self):
        from datasets.lift.full_trajectory import build_native_judge_input

        return build_native_judge_input(
            self.transcript,
            task_query="original task",
            trajectory_reqs="original rules",
            source_hashes=self.sources,
            reference_projection=True,
            chunk_projection=True,
            nested_reference_projection=True,
        )

    def test_native_envelope_restores_every_message_and_declares_new_protocol(self):
        from datasets.lift.full_trajectory import verify_native_judge_projection
        from datasets.lift.trace_chunks import decode

        value = self.build()
        self.assertEqual(value["schema"], "full-lift-judge-input-v4")
        projected, proof = projection.project_nested_tool_observations(
            self.transcript["all_messages"]
        )
        self.assertEqual(value["trajectory_projection"], proof)
        self.assertEqual(
            decode(value["text_chunks"]),
            [
                self.judge.format_content_blocks(m["content"])
                for m in projected
                if m.get("content") is not None
            ],
        )
        self.assertIn('"content_index":2', value["messages"][1]["content"])
        self.assertEqual(
            projection.restore_nested_projection(projected, proof), self.transcript["all_messages"]
        )
        verify_native_judge_projection(value)

    def test_rehashed_prompt_and_downgraded_protocol_are_rejected(self):
        from darwinagent.runtime.artifacts import digest
        from datasets.lift.full_trajectory import verify_native_judge_projection

        for kind in ("prompt", "protocol"):
            value = self.build()
            if kind == "prompt":
                value["messages"][1]["content"] = "hide failed calls"
            else:
                value["schema"] = "full-lift-judge-input-v3"
            value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
            with self.assertRaises(ValueError):
                verify_native_judge_projection(value)
