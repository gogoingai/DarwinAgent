"""Offline shared travel faults fixtures; no test-case dependencies."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from networkx import freeze

from darwinagent.config import RunConfig
from darwinagent.contracts import GraphResult
from darwinagent.experiments.admission import AdmissionError, admit_candidate
from darwinagent.kernel import KernelBundle, TaskSpec
from darwinagent.kernel.revision import AssetPatch, AssetRevisionService, training_id
from darwinagent.kernel.validation import capability_names
from darwinagent.kg.graph import load_graph

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "travel_faults"

TASK_YAML = Path("tasks/travel_planning/task.yaml")


def travel_case():
    from darwinagent.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef

    raw = json.loads((FIXTURES / "case_input.json").read_text())
    blocks = tuple(
        CorpusBlock(SourceRef(**b["source"]), b["text"], b["metadata"]) for b in raw["corpus"]
    )
    return CaseInput(raw["id"], blocks, tuple(QuestionInput(**q) for q in raw["questions"]))


def travel_graph(case):
    return GraphResult(
        freeze(load_graph(FIXTURES / "graph.json")), {b.source.id: b for b in case.corpus}
    )


def base_bundle():
    return KernelBundle(FIXTURES / "b0_assets")


def legal_candidate():
    return json.loads((FIXTURES / "legal_candidate.json").read_text())


def candidate_bundle(base, replacements, root):
    """替换 {asset_id: 新 content} 构造候选版本（走正式 AssetRevisionService 校验）。"""
    patches = [
        AssetPatch(
            replace(a, content=replacements[a.id]),
            a.fingerprint,
            "Red-repro repair of archived 2026-10-05 travel faults",
            (training_id("train:0", "0"),),
        )
        for a in base.assets.assets
        if a.id in replacements
    ]
    service = AssetRevisionService()
    return service.propose(
        base,
        patches,
        Path(root) / "candidate",
        (training_id("train:0", "0"),),
        (),
        tuple({a.kind for a in base.assets.assets if a.id in replacements}),
        (),
    )


FIXED_C = (FIXTURES / "c_answer_shape_fixed.py").read_text()

COMPLETE_C = FIXED_C.replace(
    "def check(candidate):\n    issues = []\n    ans = None\n",
    "def check(candidate):\n    issues = []\n    ans = None\n"
    "    if candidate.get('status') == 'abstained':\n"
    "        return {'ok': True, 'issues': []}\n",
)

IDEAL_C = COMPLETE_C.replace(
    "    return {'ok': len(issues) == 0, 'issues': issues}",
    "    numbers = [d.get('days') for d in ans]\n"
    "    if numbers != list(range(1, len(ans) + 1)):\n"
    "        issues.append('days sequence must be 1..N')\n"
    "    if any(not (d.get('current_city') or '') for d in ans):\n"
    "        issues.append('current_city must be non-empty')\n"
    "    return {'ok': len(issues) == 0, 'issues': issues}",
)

ATTEMPT2_C = (FIXTURES / "c_answer_shape_attempt2.py").read_text()

FIXED_F = (
    (FIXTURES / "b0_assets/assets/F/f_flight_pair.py")
    .read_text()
    .replace("out = {'outbound': None, 'inbound': None,", "out = {'outbound': {}, 'inbound': {},")
)

GROUND_C = COMPLETE_C.replace(
    "    return {'ok': len(issues) == 0, 'issues': issues}",
    "    ev = list(candidate.get('evidence') or []) + list(candidate.get('visible_evidence') or [])\n"
    "    names = {(r.get('name') or '') for r in ev if r.get('entity_type') in "
    "('restaurant', 'accommodation', 'attraction')}\n"
    "    flights = {str(r.get('flight_number') or '') for r in ev if r.get('entity_type') == 'flight'}\n"
    "    for d in ans:\n"
    "        for meal in ('breakfast', 'lunch', 'dinner'):\n"
    "            if not (d.get(meal) or ''):\n"
    "                issues.append('Meals must be present on non-transit days')\n"
    "        city = str(d.get('current_city') or '')\n"
    "        cities = [c.strip() for c in city.replace('from ', '').split(' to ')] if city else []\n"
    "        for field in ('breakfast', 'lunch', 'dinner', 'attraction', 'accommodation'):\n"
    "            value = str(d.get(field) or '')\n"
    "            for part in [p for p in value.split(';') if p.strip()]:\n"
    "                if cities and not any(c in part for c in cities):\n"
    "                    issues.append(field + ' not in day cities')\n"
    "                ref = part.split(', ')[0] if ', ' in part else part\n"
    "                if ref not in names:\n"
    "                    issues.append(field + ' references unknown entity')\n"
    "        transport = str(d.get('transportation') or '')\n"
    "        if 'Flight Number: ' in transport:\n"
    "            no = transport.split('Flight Number: ')[1].split(',')[0]\n"
    "            if no not in flights:\n"
    "                issues.append('flight not in evidence')\n"
    "    return {'ok': len(issues) == 0, 'issues': issues}",
)


def admit_travel_bundle(bundle, extra_checks=False):
    case = travel_case()
    spec = TaskSpec.load(TASK_YAML, bundle)
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "admission.json"
        try:
            admit_candidate(
                bundle,
                [case],
                {case.id: travel_graph(case)},
                RunConfig(),
                capability_names(spec.retrieval_floor),
                report_path,
                answer_contract=spec.answer_contract,
                answer_counterexamples=spec.answer_counterexamples,
                answer_examples=spec.answer_examples,
            )
            return json.loads(report_path.read_text())
        except AdmissionError as exc:
            return exc.report
