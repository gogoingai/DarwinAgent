import ast
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oak.config import RunConfig
from oak.contracts import freeze
from oak.kernel import Asset, KernelAssets, KernelBundle, TaskSpec
from oak.kernel.revision import AssetPatch, AssetRevisionService
from oak.kernel.validation import validate_bundle
from oak.operators.sandbox import Interpreter, Limits, SandboxError, admit
from oak.kernel.registration import load_assets
from tests.fixtures import ROOT, TASK


class AssetBoundary(unittest.TestCase):
    def test_only_sfcp(self):
        for kind in ['H','harness','pipeline','functions']:
            with self.assertRaises(ValueError): Asset('x',kind,'value')

    def test_fixed_check_namespace(self):
        with self.assertRaises(ValueError): Asset('fixed.source','C','def check(candidate):\n return {"ok":True,"issues":[]}',stage='answer')

    def test_no_declared_permission_grant(self):
        with self.assertRaises(TypeError): Asset('x','P','hi',role='answer',permissions=['model'])
        with self.assertRaises(TypeError): TaskSpec('x','desc',('text',),builder=lambda:None)

    def test_prompt_slots_closed(self):
        for name in ['config','model_client','budget','pipeline']:
            with self.assertRaises(ValueError): Asset('p','P','${'+name+'}',role='answer')
        self.assertEqual(Asset('p','P','Skip checks and expand budget.',role='answer').kind,'P')

    def test_atomic_revision_and_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=load_assets(TASK).export(root/'base')
            a=base.get('answer_prompt')
            patch=AssetPatch(replace(a,content='Be precise.'),a.fingerprint,'training feedback',('q1',))
            svc=AssetRevisionService()
            candidate=svc.propose(base,[patch],root/'candidate',['q1'])
            self.assertEqual(base.get('answer_prompt').content,a.content)
            self.assertNotEqual(candidate.version,base.version)
            with self.assertRaises(ValueError): svc.publish(candidate,root/'published',{'accepted':False})
            published=svc.publish(candidate,root/'published',{'accepted':True,'reasons':['test']})
            published.path('answer_prompt').write_text('tampered')
            with self.assertRaises(ValueError): published.verify()

    def test_stale_patch_and_non_train_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);base=load_assets(TASK).export(root/'base');a=base.get('answer_prompt')
            for fp,ids in [('bad',('q1',)),(a.fingerprint,('test-q',))]:
                with self.assertRaises(ValueError):
                    AssetRevisionService().propose(base,[AssetPatch(a,fp,'reason',ids)],root/'candidate',['q1'])
            self.assertFalse((root/'candidate').exists())

    def test_manifest_path_escape(self):
        with tempfile.TemporaryDirectory() as td:
            b=load_assets(TASK).export(Path(td)/'bundle')
            m=json.loads((b.root/'manifest.json').read_text());m['assets'][0]['path']='../../outside.py'
            from oak.runtime.artifacts import digest
            m['version']=digest({k:v for k,v in m.items() if k!='version'})
            (b.root/'manifest.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError): KernelBundle(b.root)

    def test_runconfig_is_frozen(self):
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError): RunConfig().tool_steps=90


class RestrictedExecution(unittest.TestCase):
    def test_permissions_and_unsupported_syntax(self):
        samples=['import os\ndef run(params):\n return 1',
                 'def run(params):\n return open("x")',
                 'def run(params):\n return extract_runtime_slots("x", [])',
                 'def run(params):\n return model.chat()',
                 'def run(params):\n return __import__("os")',
                 'def run(params):\n params["x"]=1\n return params',
                 'def run(params):\n while True:\n  pass\n return 1',
                 'def run(params):\n return (lambda: 1)()',
                 'def run(params):\n global x\n return 1',
                 'def run(params):\n return params.__class__',
                 'def run(params):\n f = nodes\n return f()',
                 'def run(params):\n return params.get("idx")',
                 'def run(params):\n return "The preset correct answer"']
        for source in samples:
            with self.subTest(source=source),self.assertRaises(SandboxError): admit(source)

    def test_c_cannot_query_or_launch(self):
        for call in ['nodes()', 'search(["x"])','Pipeline()','publish()','client()']:
            with self.assertRaises(SandboxError): admit('def check(candidate):\n return '+call,'C')

    def test_literal_complete_question_rejected(self):
        with self.assertRaises(SandboxError): admit('def run(params):\n return params.get("text") == "谁修了这台设备？"','F',['谁修了这台设备？'])

    def test_immutable_input_alias(self):
        source='def run(params):\n xs = params["values"]\n xs.append(2)\n return xs'
        fn=admit(source)
        with self.assertRaises(SandboxError): Interpreter(fn,{}).execute({'values':[1]})

    def test_local_computation(self):
        fn=admit('def run(params):\n result = []\n for x in params["values"]:\n  if x > 1:\n   result.append(x * 2)\n return result')
        self.assertEqual(Interpreter(fn,{}).execute({'values':[1,2,3]}),[4,6])

    def test_step_range_size_limits(self):
        for source in ['def run(params):\n return list(range(1000000000))',
                       'def run(params):\n return "x" * 1000000000',
                       'def run(params):\n return [x for x in range(500)]']:
            with self.assertRaises(SandboxError): Interpreter(admit(source),{},Limits(30,.1,100)).execute({})

    def test_primitive_output_only(self):
        with self.assertRaises(SandboxError): Interpreter(admit('def run(params):\n return {1,2}'),{}).execute({})

    def test_no_cpython_exec(self):
        import oak.operators.sandbox as sandbox
        tree=ast.parse(Path(sandbox.__file__).read_text())
        self.assertFalse(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in {'exec','eval','compile'} for n in ast.walk(tree)))
