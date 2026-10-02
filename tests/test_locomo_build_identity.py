from datetime import date
from pathlib import Path
import unittest

from oak.config import Config
from datasets.locomo.pipeline.build import graph_fingerprint
from datasets.locomo.pipeline.config import LocomoConfig
from datasets.locomo.pipeline.data import Conversation, Session, Turn, QA
from datasets.locomo.pipeline.schema_skeleton import load_skeleton


class BuildIdentity(unittest.TestCase):
    def test_reference_changes_do_not_affect_runtime_identity(self):
        conv = Conversation("conv-x", "A", "B", [Session(1, date(2023, 1, 1), "2023-01-01", [Turn("A", "D1:1", "消息")])], [QA(1, "q", 4, "secret")])
        cfg = LocomoConfig(Config(api_key="fake"), Path("unused"))
        before = graph_fingerprint(conv, load_skeleton(), cfg, [])
        conv.qas[0].answer = "different gold"
        conv.qas[0].evidence = ["gold-only-evidence"]
        self.assertEqual(before, graph_fingerprint(conv, load_skeleton(), cfg, []))
        conv.sessions.append(Session(2, date(2023, 1, 2), "2023-01-02", [Turn("B", "D2:1", "后续消息")]))
        self.assertNotEqual(before, graph_fingerprint(conv, load_skeleton(), cfg, []))


if __name__ == "__main__":
    unittest.main()
