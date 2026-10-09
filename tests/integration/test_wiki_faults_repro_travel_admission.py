"""Offline regression scenarios for travel admission."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.experiments.admission import AdmissionError, admit_candidate
from darwinagent.kernel import KernelBundle, TaskSpec
from darwinagent.kernel.validation import capability_names
from tests.support.travel_faults import candidate_bundle


class RealFaultAdmissionTests(unittest.TestCase):
    """二次复查 P1-C：真实故障 F（conv-30 f_filter_facts limit=500 预算耗尽）必须被
    准入在正式答题前拦截——数值放大×宽过滤组合样本＋历史失败实参回放，同预算不改。"""

    REAL_PARAMS = {"subject": "乔恩", "fact_type": "", "date_prefix": "", "limit": 500}
    MC_B0 = Path("datasets/locomo/runs/wiki_gap_repair_20261005_mc/train/B0/assets")

    def setUp(self):
        if not os.environ.get("LOCOMO_TEST_MEMORY_ROOT") or not os.environ.get(
            "LOCOMO_TEST_DATA_DIR"
        ):
            self.skipTest("Explicit external replay evidence not configured")
        required = (
            self.MC_B0 / "manifest.json",
            Path(os.environ["LOCOMO_TEST_MEMORY_ROOT"]) / "conv-30/manifest.json",
            Path(os.environ["LOCOMO_TEST_MEMORY_ROOT"]) / "conv-30/graph.json",
        )
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            self.skipTest("External archived evidence missing: " + ", ".join(missing))

    FIXED_FILTER_F = """def run(params):
    filters = {}
    subject = params.get('subject', '')
    if subject != '':
        filters['主体'] = subject
    ftype = params.get('fact_type', '')
    if ftype != '':
        filters['类型'] = ftype
    date_prefix = params.get('date_prefix', '')
    limit = params.get('limit', 50)
    if isinstance(limit, int) is False or limit < 1 or limit > 500:
        limit = 50
    rows = list(nodes(entity_type='原子事实', filters=filters, limit=limit))
    if date_prefix != '':
        rows = [r for r in rows if str(dict(r).get('日期') or '').startswith(date_prefix)]
    cap = 60
    truncated = len(rows) > cap
    if truncated:
        rows = rows[:cap]
    out = []
    for r in rows:
        d = dict(r)
        for f in ['编号', '陈述', '主体', '类型', '日期', '日期原文', '主题', '出处', 'node_id', 'entity_type']:
            if d.get(f) is None:
                d[f] = ''
        if d.get('source_ids') is None:
            d['source_ids'] = []
        out.append(d)
    return {'rows': out, 'count': len(out), 'scanned': len(rows), 'truncated': truncated,
            'note': 'rows capped before per-row normalization with truncated disclosure'}
"""

    @classmethod
    def _case_graph(cls):
        from darwinagent.experiments.snapshots import load_frozen_graph
        from datasets.locomo.adapter import LocomoAdapter

        case = LocomoAdapter(
            Path(os.environ["LOCOMO_TEST_DATA_DIR"]) / "locomo10_zh.json"
        ).generation_input("conv-30")
        graph = load_frozen_graph(
            Path(os.environ["LOCOMO_TEST_MEMORY_ROOT"]) / "conv-30", case.corpus
        )
        return case, graph

    def _admit_mc(self, content_override=None):
        from datasets.locomo.run import TASK_DIR

        base = KernelBundle(self.MC_B0)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        replacements = {}
        if content_override is not None:
            replacements["f_filter_facts"] = content_override
        bundle = candidate_bundle(base, replacements, holder.name) if replacements else base
        case, graph = self._case_graph()
        spec = TaskSpec.load(TASK_DIR / "task.yaml")
        report_path = Path(holder.name) / "admission.json"
        try:
            admit_candidate(
                bundle,
                [case],
                {case.id: graph},
                RunConfig(),
                capability_names(spec.retrieval_floor),
                report_path,
                replay_inputs=(("conv-30", "f_filter_facts", self.REAL_PARAMS),),
            )
            return json.loads(report_path.read_text())
        except AdmissionError as exc:
            return exc.report

    def test_original_fault_f_blocked_at_admission(self):
        report = self._admit_mc()
        budget = [
            s
            for s in report["scenarios"]
            if s.get("asset_id") == "f_filter_facts"
            and s.get("status") == "failed"
            and "budget" in str(s.get("error", ""))
        ]
        self.assertTrue(
            budget,
            [s.get("scenario_id") for s in report["scenarios"] if s.get("status") == "failed"],
        )

    def test_fixed_f_passes_same_budget_with_truncation(self):
        report = self._admit_mc(self.FIXED_FILTER_F)
        rows = [s for s in report["scenarios"] if s.get("asset_id") == "f_filter_facts"]
        self.assertTrue(rows)
        self.assertTrue(
            all(s.get("status") in ("passed", "skipped") for s in rows),
            [s for s in rows if s.get("status") == "failed"],
        )

    def test_stress_combo_covers_real_fault_shape(self):
        from darwinagent.experiments.trials import stress_trial_samples

        _, graph = self._case_graph()
        out = stress_trial_samples(
            [{"subject": "乔恩", "fact_type": "事件", "date_prefix": "2023-05", "limit": 20}], graph
        )
        combo = {"subject": "乔恩", "fact_type": "", "date_prefix": "", "limit": 500}
        self.assertTrue(any(p == combo for p in out if isinstance(p, dict)), out[:6])
