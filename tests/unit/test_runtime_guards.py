import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from oak.agents import ExtractionAgent
from oak.config import RunConfig
from oak.kernel.assets import KernelAssets
from oak.kernel.execution import KernelRuntime
from oak.kernel.counterexamples import run_probes
from oak.kernel.validation import validate_graph
from oak.operators.sandbox import Interpreter, Limits, SandboxError, admit
from tests.fixtures import case,client,spec


class RuntimeGuardTests(unittest.TestCase):
    def setup_graph(self):
        td=tempfile.TemporaryDirectory();self.addCleanup(td.cleanup)
        root=Path(td.name);c=case();s=spec(root/'assets');config=RunConfig()
        runtime=KernelRuntime(s.bundle,config)
        graph=asyncio.run(ExtractionAgent(runtime,client(c),config,'test').extract_entities(c.corpus))
        return root,c,s,runtime,graph

    def test_wrong_function_result_type(self):
        root,c,s,runtime,graph=self.setup_graph()
        assets=[replace(a,output_contract={'type':'number'}) if a.kind=='F' else a for a in s.bundle.assets.assets]
        rt=KernelRuntime(KernelAssets(tuple(assets)).export(root/'wrong'),RunConfig())
        with self.assertRaises(ValueError): rt.call('device_lookup',{'serial':'D-17'},graph)

    def test_check_cannot_mutate_snapshot(self):
        source='def check(candidate):\n items = candidate["evidence"]\n items.append({})\n return {"ok":True,"issues":[]}'
        with self.assertRaises(SandboxError): Interpreter(admit(source,'C'),{}).execute({'evidence':[{}]})

    def test_check_must_not_return_candidate(self):
        root,c,s,runtime,graph=self.setup_graph()
        assets=[replace(a,content='def check(candidate):\n return {"ok":True,"issues":[],"answer":"override"}') if a.kind=='C' else a for a in s.bundle.assets.assets]
        rt=KernelRuntime(KernelAssets(tuple(assets)).export(root/'wrong'),RunConfig())
        with self.assertRaises(ValueError): rt.checks.run('answer',{'status':'abstained'})

    def test_actual_graph_type_contract_and_source_checks(self):
        root,c,s,runtime,graph=self.setup_graph()
        nd=next(iter(graph.graph.nodes.values()))
        for key,value in [('__key__','{"serial":"D-17","date":"invalid-date"}'),('__sources__',['forged'])]:
            old=nd[key];nd[key]=value
            with self.assertRaises(ValueError): validate_graph(graph,runtime.schema)
            nd[key]=old

    def test_behavior_probe_rejects_subject_lookup_constant(self):
        root,c,s,runtime,graph=self.setup_graph()
        assets=[replace(a,content="def run(params):\n return nodes('Maintenance', {'serial':'D-17'}, limit=20)") if a.kind=='F' else a for a in s.bundle.assets.assets]
        rt=KernelRuntime(KernelAssets(tuple(assets)).export(root/'constant'),RunConfig())
        with self.assertRaises(ValueError): run_probes(rt,graph)

    def test_timeout_bound_and_native_output_bound(self):
        fn=admit('def run(params):\n return [x for x in range(10000)]')
        start=time.monotonic()
        with self.assertRaises(SandboxError): Interpreter(fn,{},Limits(1000000,.0001,500000)).execute({})
        self.assertLess(time.monotonic()-start,1)
        fn=admit('def run(params):\n return params["x"].replace("", params["x"])')
        with self.assertRaises(SandboxError): Interpreter(fn,{},Limits(result_bytes=1000)).execute({'x':'a'*900})

    def test_prompt_does_not_expand_visible_inputs(self):
        root,c,s,runtime,graph=self.setup_graph()
        from oak.kernel.assets import Asset
        with self.assertRaises(ValueError): Asset('prompt','P','${evaluator}',role='answer')
