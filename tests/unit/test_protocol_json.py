import unittest

from darwinagent.agents.protocol import parse_json


class ProtocolJsonTests(unittest.TestCase):
    def test_saved_provider_orphan_closing_fence_is_unambiguous(self):
        for raw in ['\n{"facts": []}\n```', '{"facts": []}`', '{"facts": []} ``']:
            self.assertEqual(parse_json(raw), {"facts": []})

    def test_full_markdown_wrapper_remains_supported(self):
        self.assertEqual(parse_json('```json\n{"facts": []}\n```'), {"facts": []})

    def test_closing_wrapper_does_not_accept_additional_content(self):
        for raw in [
            '{"facts": []} {"other": true}`',
            '{"facts": []} trailing`',
            '{"facts": []}``` instructions',
            '{"facts":',
            "[]`",
        ]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_json(raw)
