"""Generic extraction; dataset identity and evaluation references are unavailable."""
from __future__ import annotations

import asyncio
from types import MappingProxyType

import networkx as nx

from oak.contracts import GraphResult
from oak.kg.graph import EntityCandidate, RelationCandidate, build_graph, node_id
from .protocol import EXTRACT_PROTOCOL, ModelSession, ProtocolError


class ExtractionAgent:
    def __init__(self,runtime,client,config,namespace):
        self.runtime,self.client,self.config,self.namespace=runtime,client,config,namespace

    async def extract(self,corpus):
        sources={b.source.id:b for b in corpus}
        batches=[]; batch=[]; chars=0
        for block in corpus:
            if batch and chars+len(block.text)>self.config.extraction_batch_chars:
                batches.append(batch);batch=[];chars=0
            batch.append(block);chars+=len(block.text)
        if batch: batches.append(batch)
        sem=asyncio.Semaphore(self.config.concurrency)
        async def one(index,blocks):
            async with sem:
                session=ModelSession(self.client,self.config,f'{self.namespace}_extract_{index}')
                allowed={b.source.id:b for b in blocks}
                def validate(obj):
                    if set(obj)!={'entities','relations'} or not isinstance(obj['entities'],list) or not isinstance(obj['relations'],list):
                        raise ValueError('Invalid extraction shape')
                    ents=[];rels=[];claims=[]
                    for e in obj['entities']:
                        if not isinstance(e,dict) or set(e)!={'type','key','properties','source_id','quote'}:
                            raise ValueError('Invalid entity shape')
                        block=allowed.get(e['source_id'])
                        if block is None or not isinstance(e['quote'],str) or not e['quote'].strip() or e['quote'] not in block.text:
                            raise ValueError('Unregistered source or quotation')
                        ent=EntityCandidate(e['type'],e['key'],e['properties'],e['source_id'])
                        # Normalize keys before associating lineage with merged nodes.
                        single=build_graph([ent],[],self.runtime.schema)
                        nid=next(iter(single))
                        claims.append((nid,{'source_id':e['source_id'],'quote':e['quote'],
                                            'key':e['key'],'properties':e['properties']}))
                        ents.append(ent)
                    for r in obj['relations']:
                        if set(r)!={'relation','head','tail'} or any(set(r[x])!={'type','key'} for x in ('head','tail')):
                            raise ValueError('Invalid relation shape')
                        rels.append(RelationCandidate(r['relation'],(r['head']['type'],r['head']['key']),
                                                        (r['tail']['type'],r['tail']['key'])))
                    build_graph(ents,rels,self.runtime.schema)
                    return ents,rels,claims
                parsed=await session.request(self.config.extraction_role,
                    EXTRACT_PROTOCOL+'\n任务抽取指引：\n'+self.runtime.prompt('extract'),
                    {'schema':self.runtime.schema.to_yaml(),'sources':[b.to_dict() for b in blocks]},
                    validate,max_tokens=max(self.config.max_tokens,8000))
                return parsed,session.raw,session.events
        outcomes=await asyncio.gather(*(one(i,bs) for i,bs in enumerate(batches)),return_exceptions=True)
        entities=[];relations=[];claims=[];raw=[];diagnostics=[];faults=[]
        for i,outcome in enumerate(outcomes):
            if isinstance(outcome,Exception):
                raw.extend(getattr(outcome,'raw_outputs',()))
                faults.append(f'batch {i}: {outcome}')
                diagnostics.append({'batch':i,'status':'execution_error','error':str(outcome)})
            else:
                (es,rs,cs),rsraw,events=outcome
                entities.extend(es);relations.extend(rs);claims.extend(cs);raw.extend(rsraw)
                diagnostics.append({'batch':i,'status':'ok','entities':len(es),'events':events})
        if faults: raise ProtocolError('Extraction failed: '+'; '.join(faults),raw)
        graph=build_graph(entities,relations,self.runtime.schema)
        for nid,claim in claims:
            graph.nodes[nid].setdefault('__claims__',[]).append(claim)
        result=GraphResult(nx.freeze(graph),MappingProxyType(sources),tuple(raw),tuple(diagnostics))
        try: self.runtime.validate_graph(result)
        except Exception as exc: raise ProtocolError(f'Graph validation failed: {exc}',raw) from exc
        return result
