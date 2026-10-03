"""Declared TravelPlanner input tables. All file access happens before generation."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from oak.contracts import CaseInput, CorpusBlock, QuestionInput, SourceRef
from .pipeline.data.queries import parse_dates
from .pipeline.data.corpus import reference_chunks

ROOT=Path(__file__).resolve().parents[2]
ENVIRONMENT_FILES=(
    'background/citySet_with_states.txt',
    'restaurants/clean_restaurant_2022.csv',
    'accommodations/clean_accommodations_2022.csv',
    'attractions/attractions.csv',
    'googleDistanceMatrix/distance.csv',
)


class TravelPlannerAdapter:
    def __init__(self,split='train',data_dir=None,tp_root=None):
        self.split=split
        self.data_dir=Path(data_dir or ROOT/'datasets/travelplanner/data')
        self.tp_root=Path(tp_root or ROOT/'third_party/TravelPlanner')

    @property
    def input_files(self):
        return [self.data_dir/f'{self.split}.queries.jsonl',*(self.tp_root/'database'/name for name in ENVIRONMENT_FILES)]

    def generation_input(self,case_id):
        idx=int(case_id.split(':')[-1])
        row=next(r for r in map(json.loads,(self.data_dir/f'{self.split}.queries.jsonl').read_text().splitlines()) if r['idx']==idx)
        params={key:row[key] for key in ('org','dest','days','people_number','local_constraint','budget','visiting_city_number')}
        params['date']=parse_dates(row.get('date'))
        name=f'{self.split}:{idx}';blocks=[]
        cities={row['org'],row['dest']}
        city_file=self.tp_root/'database'/ENVIRONMENT_FILES[0]
        city_state=[line.strip().split('\t',1) for line in city_file.read_text().splitlines() if '\t' in line]
        cities.update(c for c,s in city_state if s==row['dest'])
        for desc,content in row.get('reference_information',{}).items():
            # Parse city columns only to select the already allowed environment scope.
            try:
                for r in csv.DictReader(io.StringIO(content)):
                    cities.update(str(r[k]).strip() for k in ('City','city','OriginCityName','DestCityName') if r.get(k))
            except (csv.Error,TypeError): pass
        for index,chunk in enumerate(reference_chunks(row.get('reference_information',{}),max_chars=4000)):
            blocks.append(CorpusBlock(SourceRef('reference_table',name,f'{chunk.source}/{index}/{chunk.seq}'),chunk.header+'\n'+chunk.text,
                                      {'table':chunk.source,'scope':'per_request_reference'}))
        for line_no,(city,state) in enumerate(city_state):
            if city in cities:
                blocks.append(CorpusBlock(SourceRef('allowed_environment',name,f'{ENVIRONMENT_FILES[0]}:{line_no+1}'),
                    json.dumps({'city':city,'state':state},ensure_ascii=False),{'table':'cities','scope':'declared_environment'}))
        for rel in ENVIRONMENT_FILES[1:]:
            path=self.tp_root/'database'/rel
            with path.open(newline='') as stream:
                for line_no,r in enumerate(csv.DictReader(stream),2):
                    city=r.get('City') or r.get('city')
                    if 'distance.csv' in rel:
                        selected=r.get('origin') in cities and r.get('destination') in cities
                    else: selected=city in cities
                    # Match official dropna semantics for lodging/food/attraction entries.
                    required = {k:v for k,v in r.items() if k and not ('distance.csv' in rel and k=='cost')}
                    if not selected or any(v is None or str(v).strip() in {'','nan','NaN','NA','None','null'} for v in required.values()): continue
                    table={'restaurants':'restaurants','accommodations':'accommodations','attractions':'attractions','googleDistanceMatrix':'distances'}[rel.split('/')[0]]
                    blocks.append(CorpusBlock(SourceRef('allowed_environment',name,f'{rel}:{line_no}'),
                        json.dumps({k:v for k,v in r.items() if k and v not in (None,'')},ensure_ascii=False),{'table':table,'scope':'declared_environment'}))
        if not blocks: raise ValueError('No allowed generation tables')
        return CaseInput(name,tuple(blocks),(QuestionInput(str(idx),row['query'],params),))
