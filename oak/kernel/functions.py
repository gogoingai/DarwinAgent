"""F admission, actual-data trials and bounded invocation."""
from __future__ import annotations

from oak.operators.data import DataCapabilities
from oak.operators.sandbox import Interpreter, Limits, admit
from oak.contracts import plain
from .spec import validate_value


class FunctionRegistry:
    def __init__(self, bundle, limits=Limits(), forbidden_questions=()):
        self.bundle, self.limits = bundle, limits
        self.functions = {a.id:(a,admit(a.content,'F',forbidden_questions))
                          for a in bundle.assets.assets if a.kind=='F'}

    def descriptions(self):
        return [{'id':a.id,'description':a.description,'input_contract':plain(a.input_contract),
                 'output_contract':plain(a.output_contract)} for a,_ in self.functions.values()]

    def call(self, asset_id, params, graph_result):
        self.bundle.verify()
        if asset_id not in self.functions: raise ValueError('Unregistered tool')
        a,fn=self.functions[asset_id]
        validate_value(params,a.input_contract,'tool.params')
        caps=DataCapabilities(graph_result)
        result=Interpreter(fn,caps.registry(),self.limits).execute(params)
        validate_value(result,a.output_contract,'tool.result')
        node_ids=sorted(caps.read_ids)
        source_ids=sorted({s for rid in node_ids for s in caps.rows[rid]['source_ids']})
        return {'asset_id':a.id,'asset_fingerprint':a.fingerprint,'data':result,
                'node_ids':node_ids,'source_ids':source_ids,'read_operations':caps.read_operations,
                'capability_calls':dict(caps.capability_calls)}

    def trial(self, graph_result, samples):
        records=[]
        if set(samples)!=set(self.functions): raise ValueError('Every F requires an actual-data trial')
        for asset_id,params_list in samples.items():
            if not params_list: raise ValueError('Empty trial set')
            for params in params_list:
                records.append(self.call(asset_id,params,graph_result))
        return records
