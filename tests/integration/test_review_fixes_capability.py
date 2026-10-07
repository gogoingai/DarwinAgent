"""Offline regression scenarios for capability."""

import unittest

from darwinagent.operators.sandbox import Interpreter, admit


class InterpreterFixes(unittest.TestCase):
    def test_isinstance_resolves_type_names(self):
        fn = admit(
            'def check(candidate):\n v = candidate.get("k", "")\n return {"ok": isinstance(v, str), "issues": []}',
            "C",
        )
        self.assertTrue(Interpreter(fn, {}).execute({"k": "v"}).get("ok"))

    def test_unknown_name_still_rejected(self):
        with self.assertRaises(Exception):
            Interpreter(admit("def run(params):\n return undefined_name", "F"), {}).execute({})
