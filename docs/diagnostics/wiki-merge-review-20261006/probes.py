"""Independent offline probes. No model requests or campaign writes."""
import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.graph_rules import _session_dates, load_facts
from datasets.locomo.scripts.question_split import DATA, build_split
from oak.experiments.wiki import _lessons
from oak.runtime.artifacts import digest
from tests.integration.test_fastloop_mode import FastLoopExperiment, _run
from tests.integration.test_wiki_faults_repro import base_bundle

HERE = Path(__file__).resolve().parent
SOURCES = ("oak/experiments/runner.py", "oak/experiments/wiki.py",
           "datasets/locomo/graph_rules.py",
           "datasets/locomo/scripts/question_split.py", "oak/runtime/deadline.py",
           "oak/operators/sandbox.py", "oak/vector/embedder.py")


def hashes():
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in SOURCES}


class DelayedFormal(FastLoopExperiment):
    async def _stage(self, name, *args, **kwargs):
        if name == "R1":
            await asyncio.sleep(0.6)
        return await super()._stage(name, *args, **kwargs)


def deadline_probe():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runner = DelayedFormal(root, round_deadline_s=0.3, proposal_attempts=1)
        summary = _run(runner, rounds=1)
        decision = summary["rounds"][0]
        return {"budget_s": 0.3, "formal_delay_s": 0.6,
                "status": summary["status"], "accepted": decision["accepted"],
                "decision_status": decision.get("status"),
                "round_elapsed_s": decision.get("round_elapsed_s"),
                "formal_written": (root / "R1/stage.json").exists()}


def split_probe():
    questions = next(x for x in json.loads(DATA.read_text())
                     if x["sample_id"] == "conv-26")["qa"]
    split = build_split()

    def evidence_map(indices):
        return {json.dumps(ev, ensure_ascii=False, sort_keys=True): i
                for i in indices for ev in questions[i].get('evidence') or ()}
    train = evidence_map(split['train'])
    val = evidence_map(split['validation'])
    return {"seed": split["seed"], "train": split["train"],
            "validation": split["validation"],
            "shared_evidence_across_splits": [
                {"train_qid": train[k], "validation_qid": val[k],
                 "evidence": json.loads(k)} for k in sorted(set(train) & set(val))]}


def dates_probe():
    facts, _ = load_facts(Path("datasets/locomo/snapshots/gvtest_v1/conv-26"))
    case = LocomoAdapter(Path("datasets/locomo/data/locomo10_zh.json")).generation_input("conv-26")
    dates = _session_dates(facts, case.corpus)
    actual = {}
    for block in case.corpus:
        match = re.match(r"D(\d+):", block.source.location)
        if match:
            actual[int(match.group(1))] = str(block.metadata.get("date", ""))[:10]
    wrong = [{"session": n, "projected_session_date": d,
              "corpus_session_date": actual[n]}
             for n, d in dates.items() if actual.get(n) and actual[n] != d]
    return {"sessions": len(dates), "mismatched": len(wrong), "mismatches": wrong}


def wiki_probe():
    asset = next(a for a in base_bundle().assets.assets if a.id == "f_flight_pair")
    params_digest = digest({"org": "A", "dest": "B"})

    def row(scenario, status):
        result = {"asset_id": asset.id, "scenario_id": scenario, "status": status,
                  "required": True,
                  "input_ref": f"train:0:{scenario}:0:{params_digest}"}
        if status == "failed":
            result.update(error_type="ValueError", error="contract failure")
        return result

    old = {"id": "old", "kind": "attempt", "stage": "R1", "scope": "admission",
           "facts": {"status": "failed", "admission": {
               "graph_digests": {"train:0": "old-graph"},
               "scenarios": [row("stress", "failed")]}}}
    outputs = []
    for label, scenario, graph in (("different_scenario", "base", "old-graph"),
                                  ("different_graph", "stress", "new-graph")):
        new = {"id": "new", "kind": "attempt", "stage": "R2", "scope": "admission",
               "facts": {"status": "passed", "asset_changes": [{
                   "after": {**asset.to_dict(), "fingerprint": "new-fingerprint"}}],
                   "verification": {"verdict": "passed",
                                    "graph_digests": {"train:0": graph},
                                    "scenarios": [row(scenario, "passed")]}}}
        lessons = _lessons([old, new])
        outputs.append({"case_id": "train:0", "variant": label,
                        "incorrectly_marked_verified": any(
                            l["status"] == "admission_verified" for l in lessons),
                        "lessons": lessons})
    return outputs


if __name__ == "__main__":
    result = {"main_sha": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True).strip(), "sources_start": hashes(),
        "deadline": deadline_probe(), "split": split_probe(),
        "session_dates": dates_probe(), "wiki_binding": wiki_probe()}
    result["sources_end"] = hashes()
    result["source_unchanged"] = result["sources_start"] == result["sources_end"]
    (HERE / "probes.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"main_sha": result["main_sha"], "deadline": result["deadline"],
                     "split_overlaps": result["split"]["shared_evidence_across_splits"],
                     "session_date_mismatches": result["session_dates"]["mismatched"],
                     "wiki_binding": [{k: r[k] for k in ("case_id", "variant", "incorrectly_marked_verified")}
                                      for r in result["wiki_binding"]],
                     "source_unchanged": result["source_unchanged"]}, ensure_ascii=False))
