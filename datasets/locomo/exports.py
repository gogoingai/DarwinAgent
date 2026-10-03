"""Serialization bridge only; published content is never amended."""
import json
from oak.runtime.artifacts import atomic_json


def legacy_rows(result):
    return [{'idx':int(a.question_id),'answer':a.answer,'status':'answer_error' if a.status=='execution_error' else 'ok',
             'generation_status':a.status,'refused':a.status=='abstained','evidence':[s.to_dict() for s in a.evidence],
             'raw_outputs':list(a.raw_outputs),'error':a.error} for a in result.answers]


def write(result,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in legacy_rows(result)))
