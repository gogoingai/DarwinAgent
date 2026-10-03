"""One runtime shared by the two fixed Agents."""
from __future__ import annotations

import json
from string import Template

from oak.contracts import plain
from oak.operators.sandbox import Limits
from .checks import CheckRegistry
from .functions import DataCapabilities, FunctionRegistry
from .validation import validate_bundle, validate_graph


class KernelRuntime:
    def __init__(self,bundle,config,forbidden_questions=()):
        self.bundle=bundle
        self.schema=validate_bundle(bundle,forbidden_questions)
        limits=Limits(config.function_steps,config.function_timeout_s,config.result_bytes)
        self.functions=FunctionRegistry(bundle,limits,forbidden_questions)
        self.checks=CheckRegistry(bundle,limits,forbidden_questions)
        self.prompts={a.role:a for a in bundle.assets.assets if a.kind=='P'}

    def prompt(self,role):
        self.bundle.verify()
        slots={'schema':self.schema.to_yaml(),'tools':json.dumps(self.functions.descriptions(),ensure_ascii=False)}
        return Template(self.prompts[role].content).substitute(slots)

    def call(self,asset_id,params,graph):
        return self.functions.call(asset_id,params,graph)

    def graph_snapshot(self,graph):
        return {'nodes':list(DataCapabilities(graph).rows.values()),'stage':'graph'}

    def validate_graph(self,graph):
        validate_graph(graph,self.schema)
        opinions=self.checks.run('graph',self.graph_snapshot(graph))
        failures=[x for x in opinions if not x['ok']]
        if failures: raise ValueError(f'Task graph checks rejected graph: {failures}')
        return opinions

    def check_candidate(self,question,candidate,graph,visible_ids):
        caps=DataCapabilities(graph)
        evidence=[caps.rows[x] for x in candidate['node_ids']]
        snapshot={'stage':'answer','question':question.text,'parameters':plain(question.parameters),
                  'status':candidate['status'],'answer':candidate['answer'],
                  'node_ids':candidate['node_ids'],'evidence':evidence,
                  'visible_evidence':[caps.rows[x] for x in sorted(visible_ids)]}
        # JSON tasks expose the already parsed candidate to C, never a mutable candidate object.
        try: snapshot['structured_answer']=json.loads(candidate['answer'])
        except ValueError: snapshot['structured_answer']=None
        return snapshot,self.checks.run('answer',snapshot)
