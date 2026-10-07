"""Experiment statistics helpers; independent of the controller."""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from collections.abc import Mapping
from darwinagent.contracts import plain
from darwinagent.kernel.revision import training_id
from darwinagent.runtime.artifacts import atomic_json, digest


def stability_metrics(root):
    """Summarize only persisted attempts and evaluated stages from this run."""
    root=Path(root)
    paths=([root/'B0'/'admission.json']
           +list(root.glob('R*/.candidate-attempt-*/admission.json'))
           +list(root.glob('R*/candidate/admission.json')))
    reports=[json.loads(p.read_text()) for p in paths if p.exists()]
    stages=[json.loads(p.read_text()) for p in root.glob('*/stage.json')]
    retry_rows=[]
    for path in root.glob('*/fault-retry/*.json'):
        retry_rows.extend(json.loads(path.read_text()).get('questions',{}).values())
    count=len(reports)
    passed=sum(r.get('verdict')=='passed' for r in reports)
    smoke_passed=sum(r.get('verdict')=='passed' and
                     r.get('smoke',{}).get('status')=='passed' for r in reports)
    return {'admission_submitted':count,'admission_passed':passed,
            'admission_pass_rate':passed/count if count else None,
            'smoke_passed':smoke_passed,
            'admission_elapsed_s':round(sum(r.get('elapsed_s',0) for r in reports),3),
            'smoke_elapsed_s':round(sum(r.get('smoke',{}).get('elapsed_s',0)
                                         for r in reports),3),
            'formal_execution_faults':sum(
                s.get('scores',{}).get('generation_faults',0) or 0 for s in stages),
            'formal_elapsed_s':round(sum(s.get('elapsed_s',0) for s in stages),3),
            'retry_elapsed_s':round(sum(
                sum(r.get('elapsed_s',0) for r in s.get('fault_retries',{}).values())
                for s in stages),3),
            'retry_reserved':len(retry_rows),
            'retry_same_class_failures':sum(
                x.get('final_error_type')==x.get('initial_error_type')
                for x in retry_rows if x.get('initial_error_type')
                and 'final_error_type' in x)}

