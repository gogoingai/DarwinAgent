"""Fixed behavioral admission probes, independent of candidate-authored trial logic."""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from types import MappingProxyType

import networkx as nx

from oak.contracts import CorpusBlock, GraphResult, plain
from oak.kg.graph import node_id, node_view
from oak.operators.data import DataCapabilities
from oak.runtime.artifacts import digest


def run_probes(runtime,graph):
    """Rename key values and shift dates. Parameterized F must transform with the input.

    This checks dependence on actual data, not semantic correctness of arbitrary tasks;
    independent C fixtures and source review cover those separately.
    """
    replacements={}
    for _,nd in graph.graph.nodes(data=True):
        spec=runtime.schema.entity(nd['etype']); values=node_view(nd)
        dtypes={a.name:a.dtype for a in spec.attributes}
        for key in spec.primary_key:
            value=values.get(key)
            if dtypes[key]=='string' and isinstance(value,str) and 1<=len(value)<=48:
                replacements[value]='cf_'+digest(value)[:12]
        for key,kind in dtypes.items():
            value=values.get(key)
            if kind=='date' and value:
                replacements[value]=(date.fromisoformat(value)+timedelta(days=17)).isoformat()
    def change(value):
        if isinstance(value,str):
            if value in replacements: return replacements[value]
            for old,new in sorted(replacements.items(),key=lambda x:-len(x[0])):
                value=value.replace(old,new)
            return value
        if isinstance(value,dict): return {k:change(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)): return [change(v) for v in value]
        return value
    altered=nx.MultiDiGraph(); nid_map={}
    for nid,nd in graph.graph.nodes(data=True):
        attrs=change(copy.deepcopy(nd))
        key=change(json.loads(nd['__key__']))
        attrs['__key__']=json.dumps(key,ensure_ascii=False)
        new_id=node_id(nd['etype'],key);nid_map[nid]=new_id
        altered.add_node(new_id,**attrs)
    for h,t,k,attrs in graph.graph.edges(keys=True,data=True):
        altered.add_edge(nid_map[h],nid_map[t],key=k,**change(attrs))
    sources={sid:CorpusBlock(b.source,change(b.text),change(plain(b.metadata))) for sid,b in graph.sources.items()}
    cf=GraphResult(nx.freeze(altered),MappingProxyType(sources))
    old=DataCapabilities(graph);new=DataCapabilities(cf)
    alias_by_nid={nid:alias for alias,nid in new.actual_ids.items()}
    alias_map={alias:alias_by_nid[nid_map[nid]] for alias,nid in old.actual_ids.items()}
    def normalize(value):
        value=change(value)
        def aliases(x):
            if isinstance(x,str) and x in alias_map: return alias_map[x]
            if isinstance(x,list): return [aliases(v) for v in x]
            if isinstance(x,dict): return {k:aliases(v) for k,v in x.items()}
            return x
        return aliases(value)
    records=[]
    for asset,_ in runtime.functions.functions.values():
        for params in asset.trial_inputs:
            params=plain(params)
            before=runtime.call(asset.id,params,graph)
            after=runtime.call(asset.id,normalize(params),cf)
            expected=normalize(before['data'])
            # Query traversal order may change under renamed primary keys; compare list data as a multiset.
            def canonical(v):
                if isinstance(v,list): return sorted((json.dumps(x,sort_keys=True,ensure_ascii=False) for x in v))
                return v
            if canonical(expected)!=canonical(after['data']):
                raise ValueError(f'F counterexample failed for {asset.id}: output did not follow renamed/date-shifted data')
            records.append({'asset_id':asset.id,'probe':'renamed_keys_shifted_dates','status':'passed',
                            'before_digest':digest(before['data']),'after_digest':digest(after['data'])})
    # Graph C must survive a pure renaming/date shift of the same well-formed graph.
    original=runtime.checks.run('graph',runtime.graph_snapshot(graph))
    shifted=runtime.checks.run('graph',runtime.graph_snapshot(cf))
    if [(c['check_id'],c['ok']) for c in original]!=[(c['check_id'],c['ok']) for c in shifted]:
        raise ValueError('Graph C depends on training names/dates rather than a declared invariant')
    records.append({'probe':'graph_check_renaming','status':'passed'})
    return records
