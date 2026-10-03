"""Apply declared S axioms to graph instances, independently of optimizable C."""
from collections import defaultdict
from oak.kg.graph import node_view


def instance_checks(graph,schema):
    parents=defaultdict(set)
    for ax in schema.axioms:
        if ax.kind=='subclass': parents[ax.params['sub']].add(ax.params['sup'])
    def closure(name):
        seen=set();pending=[name]
        while pending:
            current=pending.pop()
            if current in seen: continue
            seen.add(current);pending.extend(parents[current])
        return seen
    errors=[]
    for nid,nd in graph.nodes(data=True):
        classes=closure(nd['etype'])
        for ax in schema.axioms:
            p=ax.params
            if ax.kind=='disjoint' and len(classes & set(p.get('classes',[])))>1:
                errors.append(f'Disjoint classes hold for {nid}')
            elif ax.kind=='cardinality' and p['class'] in classes:
                targets={t for _,t,ed in graph.out_edges(nid,data=True) if ed.get('relation')==p['relation']}
                count=len(targets)
                if p.get('min') is not None and count<p['min'] or p.get('max') is not None and count>p['max']:
                    errors.append(f'Cardinality violated for {nid}/{p["relation"]}: {count}')
    for h,t,ed in graph.edges(data=True):
        for ax in schema.axioms:
            p=ax.params
            if ax.kind in {'domain','range'} and p['relation']==ed['relation']:
                node=h if ax.kind=='domain' else t
                if p['class'] not in closure(graph.nodes[node]['etype']):
                    errors.append(f'Axiom {ax.kind} violated for {ed["relation"]}')
    for ax in schema.axioms:
        if ax.kind!='key_functional': continue
        p=ax.params;groups={}
        for nid,nd in graph.nodes(data=True):
            if p['entity'] not in closure(nd['etype']): continue
            row=node_view(nd)
            key=tuple(row.get(k) for k in p['key'])
            if None in key or '' in key: errors.append(f'Functional key missing on {nid}')
            elif key in groups and groups[key]!=nid: errors.append(f'Functional key collision on {p["entity"]}')
            else: groups[key]=nid
    return errors
