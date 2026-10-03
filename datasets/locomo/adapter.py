"""LoCoMo generation conversion only. No reference-answer dataclasses or model calls."""
from __future__ import annotations

import json
from pathlib import Path

from oak.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from .pipeline.dates import parse_session_datetime, parse_cn_date


class LocomoAdapter:
    def __init__(self,path: Path):
        self.path=Path(path)

    def generation_input(self,case_id):
        raw=next(c for c in json.loads(self.path.read_text()) if c['sample_id']==case_id)
        convo=raw['conversation'];blocks=[];dates={}
        sessions=sorted(int(k[8:]) for k in convo if k.startswith('session_') and k[8:].isdigit())
        for n in sessions:
            dt=parse_session_datetime(convo.get(f'session_{n}_date_time',''))
            iso=dt.isoformat() if dt else ''
            for turn in convo[f'session_{n}']:
                if not turn.get('text'): continue
                dia=str(turn['dia_id']);dates[dia]=iso
                blocks.append(CorpusBlock(SourceRef('message_text',case_id,dia),turn['text'],
                    {'speaker':str(turn.get('speaker','')),'date':iso,'reliability':'original_message'}))
        for group,speakers in (raw.get('observation') or {}).items():
            for speaker,items in (speakers or {}).items():
                for index,item in enumerate(items or []):
                    if not isinstance(item,(list,tuple)) or len(item)<2 or not item[0]: continue
                    blocks.append(CorpusBlock(SourceRef('observation',case_id,f'{group}/{speaker}/{index}'),str(item[0]),
                        {'speaker':str(speaker),'date':dates.get(str(item[1]),''),'reliability':'dataset_annotation'}))
        for group,speakers in (raw.get('event_summary') or {}).items():
            dt=parse_session_datetime(str(speakers.get('date') or ''))
            if dt is None:
                from datetime import date
                ymd=parse_cn_date(str(speakers.get('date') or ''))
                if ymd and ymd[0] and ymd[1] and ymd[2]: dt=date(*ymd)
            iso=dt.isoformat() if dt else ''
            for speaker,items in speakers.items():
                if speaker=='date': continue
                for index,text in enumerate(items or []):
                    if str(text).strip():
                        blocks.append(CorpusBlock(SourceRef('event_summary',case_id,f'{group}/{speaker}/{index}'),str(text),
                            {'speaker':str(speaker),'date':iso,'reliability':'dataset_annotation'}))
        questions=tuple(QuestionInput(str(i),str(q['question'])) for i,q in enumerate(raw['qa']))
        return CaseInput(case_id,tuple(blocks),questions)
