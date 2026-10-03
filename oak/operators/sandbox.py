"""A positive AST interpreter, not CPython exec or a general Python sandbox.

Only primitive immutable input values and explicit data capabilities are reachable.
Every expression/iteration consumes a step; native primitives have bounded input/output.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from oak.contracts import freeze, plain


class SandboxError(ValueError):
    pass


DATA_CAPABILITIES = frozenset({'nodes', 'search', 'traverse', 'project', 'aggregate', 'order_by', 'date_difference'})
BUILTINS = {'len': len, 'min': min, 'max': max, 'sum': sum, 'sorted': sorted, 'set': set,
            'dict': dict, 'list': list, 'tuple': tuple, 'str': str, 'int': int, 'float': float,
            'round': round, 'abs': abs, 'enumerate': enumerate, 'zip': zip, 'range': range,
            'bool': bool, 'any': any, 'all': all, 'ceil': math.ceil}
METHODS = {'get', 'keys', 'values', 'items', 'lower', 'upper', 'strip', 'split', 'rsplit', 'splitlines',
           'join', 'startswith', 'endswith', 'replace', 'isdigit', 'count', 'append', 'extend', 'add'}
ALLOWED_NODES = {ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
                 ast.AugAssign, ast.If, ast.For, ast.Break, ast.Continue, ast.Pass, ast.Expr,
                 ast.Name, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Subscript,
                 ast.Slice, ast.Call, ast.Attribute, ast.keyword, ast.IfExp, ast.BinOp,
                 ast.UnaryOp, ast.BoolOp, ast.Compare, ast.ListComp, ast.SetComp, ast.DictComp,
                 ast.GeneratorExp, ast.comprehension, ast.Load, ast.Store,
                 ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.USub, ast.UAdd,
                 ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                 ast.In, ast.NotIn, ast.Is, ast.IsNot}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
           ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a,b: a in b,
           ast.NotIn: lambda a,b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}


def admit(source: str, kind='F', forbidden_questions=()):
    if kind not in {'F', 'C'} or len(source.encode()) > 60000:
        raise SandboxError('Invalid restricted code kind or size')
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SandboxError(str(exc)) from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise SandboxError('Exactly one function definition required')
    fn = tree.body[0]
    expected = 'params' if kind == 'F' else 'candidate'
    if fn.name != ('run' if kind == 'F' else 'check') or fn.decorator_list or fn.returns:
        raise SandboxError('Expected run(params) or check(candidate), without decorators or annotations')
    a = fn.args
    if len(a.args) != 1 or a.args[0].arg != expected or a.args[0].annotation or a.defaults or a.kw_defaults or a.kwonlyargs or a.posonlyargs or a.vararg or a.kwarg:
        raise SandboxError('Fixed one-argument calling contract required')
    caps = DATA_CAPABILITIES if kind == 'F' else frozenset()
    for n in ast.walk(tree):
        if type(n) not in ALLOWED_NODES:
            raise SandboxError(f'Unsupported syntax: {type(n).__name__}')
        if isinstance(n, (ast.FunctionDef, ast.arguments)) and n not in (fn, a):
            raise SandboxError('Nested functions are forbidden')
        if isinstance(n, ast.Name) and (n.id.startswith('_') or (isinstance(n.ctx, ast.Store) and n.id in {expected, *caps, *BUILTINS})):
            raise SandboxError('Reserved input/capability cannot be rebound')
        if isinstance(n, ast.Attribute):
            if n.attr not in METHODS:
                raise SandboxError('Attribute/reflection access is forbidden')
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                if n.func.id not in set(BUILTINS) | caps:
                    raise SandboxError(f'Unregistered call: {n.func.id}')
            elif not (isinstance(n.func, ast.Attribute) and n.func.attr in METHODS):
                raise SandboxError('Dynamic calls are forbidden')
            if any(k.arg is None for k in n.keywords) or any(isinstance(x, ast.Starred) for x in n.args):
                raise SandboxError('Dynamic call arguments are forbidden')
        if isinstance(n, (ast.Assign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            def local(t):
                return isinstance(t, ast.Name) or isinstance(t, (ast.Tuple, ast.List)) and all(local(x) for x in t.elts)
            if not all(local(x) for x in targets):
                raise SandboxError('Only local variable assignment is allowed')
        if isinstance(n, ast.For) and n.orelse:
            raise SandboxError('For-else is unsupported')
        if isinstance(n, ast.comprehension) and n.is_async:
            raise SandboxError('Async code is forbidden')
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if n.value in {'question_id', 'case_id', 'idx'} or re.fullmatch(r'(?:conv-\d+|q\d{2,}|\d+-\d{4})', n.value):
                raise SandboxError('Question/case lookup keys are forbidden')
            if any(q.strip() and q.strip() in n.value for q in forbidden_questions):
                raise SandboxError('Complete-question matching is forbidden')
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            raise SandboxError('Preset answer strings are forbidden')
    return fn


@dataclass(frozen=True)
class Limits:
    steps: int = 30000
    timeout_s: float = 2.0
    result_bytes: int = 180000
    container_items: int = 12000


class _Return(Exception):
    def __init__(self, value): self.value = value
class _Break(Exception): pass
class _Continue(Exception): pass


class Interpreter:
    def __init__(self, fn, capabilities, limits=Limits()):
        self.fn, self.caps, self.limits = fn, capabilities, limits
        self.steps, self.deadline = 0, 0.0

    def tick(self):
        self.steps += 1
        if self.steps > self.limits.steps or time.monotonic() > self.deadline:
            raise SandboxError('Restricted execution budget exhausted')

    def bound(self, value):
        if isinstance(value, (str, list, tuple, dict, set, Mapping)) and len(value) > self.limits.container_items:
            # Strings get a separate byte budget.
            if not isinstance(value, str) or len(value.encode()) > self.limits.result_bytes:
                raise SandboxError('Container limit exceeded')
        if isinstance(value, str) and len(value.encode()) > self.limits.result_bytes:
            raise SandboxError('String limit exceeded')
        if type(value) is int and value.bit_length() > 512:
            raise SandboxError('Integer limit exceeded')
        if type(value) is float and not math.isfinite(value):
            raise SandboxError('Nonfinite number')
        return value

    def execute(self, value):
        self.deadline = time.monotonic() + self.limits.timeout_s
        env = {self.fn.args.args[0].arg: freeze(value)}
        try:
            self.block(self.fn.body, env)
        except _Return as ret:
            result = plain(ret.value)
            try:
                blob = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise SandboxError('Result must be JSON data') from exc
            if len(blob.encode()) > self.limits.result_bytes:
                raise SandboxError('Result byte limit exceeded')
            return json.loads(blob)
        except (SandboxError,):
            raise
        except Exception as exc:
            raise SandboxError(f'Restricted execution failed: {type(exc).__name__}: {exc}') from exc
        raise SandboxError('Code did not return a result')

    def target(self, node, value, env):
        self.tick()
        if isinstance(node, ast.Name):
            env[node.id] = self.bound(value)
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == len(value):
            for n, v in zip(node.elts, value): self.target(n, v, env)
        else:
            raise SandboxError('Invalid local target')

    def block(self, stmts, env):
        for node in stmts:
            self.tick()
            if isinstance(node, ast.Return): raise _Return(self.expr(node.value, env))
            elif isinstance(node, ast.Assign):
                value = self.expr(node.value, env)
                for target in node.targets: self.target(target, value, env)
            elif isinstance(node, ast.AugAssign):
                value = self.binary(node.op, self.expr(node.target, env), self.expr(node.value, env))
                self.target(node.target, value, env)
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test, env) else node.orelse, env)
            elif isinstance(node, ast.For):
                values = self.expr(node.iter, env)
                self.bound(values)
                for value in values:
                    self.tick(); self.target(node.target, value, env)
                    try: self.block(node.body, env)
                    except _Break: break
                    except _Continue: continue
            elif isinstance(node, ast.Expr): self.expr(node.value, env)
            elif isinstance(node, ast.Break): raise _Break()
            elif isinstance(node, ast.Continue): raise _Continue()
            elif not isinstance(node, ast.Pass): raise SandboxError('Unsupported statement')

    def binary(self, op, a, b):
        if isinstance(op, ast.Mult):
            seq, count = (a,b) if isinstance(a, (str,list,tuple)) else (b,a)
            if isinstance(seq, (str,list,tuple)) and type(count) is int and len(seq) * max(count,0) > self.limits.container_items:
                raise SandboxError('Repetition limit exceeded')
        if isinstance(op, ast.Mod) and isinstance(a, str):
            raise SandboxError('String interpolation is unsupported')
        return self.bound(BINARY[type(op)](a,b))

    def expr(self, n, env):
        self.tick()
        if n is None: return None
        if isinstance(n, ast.Constant): return self.bound(n.value)
        if isinstance(n, ast.Name): return env[n.id]
        if isinstance(n, ast.List): return self.bound([self.expr(x,env) for x in n.elts])
        if isinstance(n, ast.Tuple): return self.bound(tuple(self.expr(x,env) for x in n.elts))
        if isinstance(n, ast.Set): return self.bound({self.expr(x,env) for x in n.elts})
        if isinstance(n, ast.Dict): return self.bound({self.expr(k,env):self.expr(v,env) for k,v in zip(n.keys,n.values)})
        if isinstance(n, ast.Subscript): return self.expr(n.value,env)[self.expr(n.slice,env)]
        if isinstance(n, ast.Slice): return slice(*(self.expr(x,env) for x in (n.lower,n.upper,n.step)))
        if isinstance(n, ast.BinOp): return self.binary(n.op,self.expr(n.left,env),self.expr(n.right,env))
        if isinstance(n, ast.UnaryOp):
            v = self.expr(n.operand,env)
            return self.bound(not v if isinstance(n.op,ast.Not) else -v if isinstance(n.op,ast.USub) else +v)
        if isinstance(n, ast.IfExp): return self.expr(n.body if self.expr(n.test,env) else n.orelse,env)
        if isinstance(n, ast.BoolOp):
            value = self.expr(n.values[0],env)
            for x in n.values[1:]:
                if isinstance(n.op,ast.And) and not value or isinstance(n.op,ast.Or) and value: break
                value = self.expr(x,env)
            return value
        if isinstance(n, ast.Compare):
            left = self.expr(n.left,env)
            for op, right in zip(n.ops,n.comparators):
                value = self.expr(right,env)
                if not COMPARE[type(op)](left,value): return False
                left = value
            return True
        if isinstance(n, (ast.ListComp,ast.SetComp,ast.GeneratorExp,ast.DictComp)):
            output = []
            def visit(depth, scope):
                self.tick()
                if depth == len(n.generators):
                    output.append((self.expr(n.key,scope),self.expr(n.value,scope)) if isinstance(n,ast.DictComp) else self.expr(n.elt,scope))
                    self.bound(output); return
                g = n.generators[depth]
                for v in self.expr(g.iter,scope):
                    child = dict(scope); self.target(g.target,v,child)
                    if all(self.expr(x,child) for x in g.ifs): visit(depth+1,child)
            visit(0,dict(env))
            return dict(output) if isinstance(n,ast.DictComp) else set(output) if isinstance(n,ast.SetComp) else output
        if isinstance(n, ast.Call):
            args = [self.expr(x,env) for x in n.args]
            kwargs = {x.arg:self.expr(x.value,env) for x in n.keywords}
            if isinstance(n.func, ast.Name):
                name = n.func.id
                if name in self.caps:
                    return freeze(self.bound(self.caps[name](*args,**kwargs)))
                if name == 'range':
                    r = range(*args)
                    self.bound(r) if len(r) <= self.limits.container_items else self.fail('Range limit exceeded')
                    return list(r)
                if name == 'str' and args and isinstance(args[0],(Mapping,list,tuple,set)):
                    self.fail('Container-to-string conversion is unsupported')
                result = BUILTINS[name](*args,**kwargs)
                if name in {'enumerate','zip'}: result = list(result)
                return self.bound(result)
            obj = self.expr(n.func.value,env)
            method = n.func.attr
            safe = ((isinstance(obj,Mapping) and method in {'get','keys','values','items'}) or
                    (isinstance(obj,str) and method in METHODS - {'get','keys','values','items','append','extend','add'}) or
                    (type(obj) is list and method in {'append','extend','count'}) or
                    (type(obj) is set and method == 'add') or
                    (type(obj) is tuple and method == 'count'))
            if not safe: self.fail('Method not available on this immutable value')
            if isinstance(obj,str) and method=='replace' and len(args)>=2:
                count=obj.count(args[0]) if args[0] else len(obj)+1
                if len(args)>2: count=min(count,max(args[2],0))
                if len(obj)+count*max(0,len(args[1])-len(args[0])) > self.limits.result_bytes:
                    self.fail('Replacement limit exceeded')
            if isinstance(obj,str) and method=='join' and args:
                seq=args[0]
                if not isinstance(seq,(list,tuple)) or sum(len(x) for x in seq)+len(obj)*max(0,len(seq)-1)>self.limits.result_bytes:
                    self.fail('Join limit exceeded')
            # Methods are selected here, never exposed as first-class callable values.
            result = getattr(obj,method)(*args,**kwargs)
            self.bound(obj)
            if method in {'keys','values','items'}: result = list(result)
            return self.bound(result)
        self.fail(f'Unsupported expression: {type(n).__name__}')

    @staticmethod
    def fail(message): raise SandboxError(message)
