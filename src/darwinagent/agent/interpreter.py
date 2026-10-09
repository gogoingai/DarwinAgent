"""Async additive interpreter; legacy synchronous semantics remain unchanged."""

import ast
import asyncio
import inspect
import json
import time
from collections.abc import Mapping

from darwinagent.contracts import freeze, plain
from darwinagent.operators.sandbox import (
    BUILTINS,
    COMPARE,
    METHODS,
    Interpreter,
    SandboxError,
    _Break,
    _Continue,
    _Return,
)


class AsyncInterpreter(Interpreter):
    async def execute(self, value):
        self.deadline = time.monotonic() + self.limits.timeout_s
        env = {self.fn.args.args[0].arg: freeze(value)}
        try:
            async with asyncio.timeout(self.limits.timeout_s):
                await self.block(self.fn.body, env)
        except _Return as ret:
            result = plain(ret.value)
            try:
                blob = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise SandboxError("Result must be JSON data") from exc
            if len(blob.encode()) > self.limits.result_bytes:
                raise SandboxError("Result byte limit exceeded") from None
            return json.loads(blob)
        except TimeoutError:
            raise SandboxError("Restricted execution time budget exhausted") from None
        except (SandboxError,):
            raise
        except Exception as exc:
            from darwinagent.runtime.steps import UnknownRequest

            if getattr(exc, "agent_signal", False) or isinstance(exc, UnknownRequest):
                raise
            raise SandboxError(f"Restricted execution failed: {type(exc).__name__}: {exc}") from exc
        raise SandboxError("Code did not return a result")

    async def target(self, node, value, env):
        self.tick()
        if isinstance(node, ast.Name):
            env[node.id] = self.bound(value)
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == len(value):
            for n, v in zip(node.elts, value):
                await self.target(n, v, env)
        elif isinstance(node, ast.Subscript):
            container = await self.expr(node.value, env)
            # 冻结输入（mappingproxy/tuple）在这里自然抛 TypeError，由沙箱统一转为 SandboxError。
            try:
                container[await self.expr(node.slice, env)] = self.bound(value)
            except TypeError as exc:
                raise SandboxError(f"Immutable container item assignment: {exc}") from None
        else:
            raise SandboxError("Invalid local target")

    async def block(self, stmts, env):
        for node in stmts:
            self.tick()
            if isinstance(node, ast.Return):
                raise _Return(await self.expr(node.value, env))
            elif isinstance(node, ast.Assign):
                value = await self.expr(node.value, env)
                for target in node.targets:
                    await self.target(target, value, env)
            elif isinstance(node, ast.AugAssign):
                value = self.binary(
                    node.op, await self.expr(node.target, env), await self.expr(node.value, env)
                )
                await self.target(node.target, value, env)
            elif isinstance(node, ast.If):
                await self.block(node.body if await self.expr(node.test, env) else node.orelse, env)
            elif isinstance(node, ast.For):
                values = await self.expr(node.iter, env)
                self.bound(values)
                for value in values:
                    self.tick()
                    await self.target(node.target, value, env)
                    try:
                        await self.block(node.body, env)
                    except _Break:
                        break
                    except _Continue:
                        continue
            elif isinstance(node, ast.Expr):
                await self.expr(node.value, env)
            elif isinstance(node, ast.Break):
                raise _Break()
            elif isinstance(node, ast.Continue):
                raise _Continue()
            elif not isinstance(node, ast.Pass):
                raise SandboxError("Unsupported statement")

    async def expr(self, n, env):
        self.tick()
        if n is None:
            return None
        if isinstance(n, ast.Constant):
            return self.bound(n.value)
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            if n.id in BUILTINS:
                return BUILTINS[n.id]  # type objects as values, e.g. isinstance(x, str)
            raise SandboxError(f"Unresolved name: {n.id}")
        if isinstance(n, ast.List):
            return self.bound([await self.expr(x, env) for x in n.elts])
        if isinstance(n, ast.Tuple):
            return self.bound(tuple([await self.expr(x, env) for x in n.elts]))
        if isinstance(n, ast.Set):
            return self.bound({await self.expr(x, env) for x in n.elts})
        if isinstance(n, ast.Dict):
            return self.bound(
                {await self.expr(k, env): await self.expr(v, env) for k, v in zip(n.keys, n.values)}
            )
        if isinstance(n, ast.Subscript):
            return (await self.expr(n.value, env))[await self.expr(n.slice, env)]
        if isinstance(n, ast.Slice):
            return slice(*[await self.expr(x, env) for x in (n.lower, n.upper, n.step)])
        if isinstance(n, ast.BinOp):
            return self.binary(n.op, await self.expr(n.left, env), await self.expr(n.right, env))
        if isinstance(n, ast.UnaryOp):
            v = await self.expr(n.operand, env)
            return self.bound(
                not v if isinstance(n.op, ast.Not) else -v if isinstance(n.op, ast.USub) else +v
            )
        if isinstance(n, ast.IfExp):
            return await self.expr(n.body if await self.expr(n.test, env) else n.orelse, env)
        if isinstance(n, ast.BoolOp):
            value = await self.expr(n.values[0], env)
            for x in n.values[1:]:
                if isinstance(n.op, ast.And) and not value or isinstance(n.op, ast.Or) and value:
                    break
                value = await self.expr(x, env)
            return value
        if isinstance(n, ast.Compare):
            left = await self.expr(n.left, env)
            for op, right in zip(n.ops, n.comparators):
                value = await self.expr(right, env)
                if not COMPARE[type(op)](left, value):
                    return False
                left = value
            return True
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            output = []

            async def visit(depth, scope):
                self.tick()
                if depth == len(n.generators):
                    output.append(
                        (await self.expr(n.key, scope), await self.expr(n.value, scope))
                        if isinstance(n, ast.DictComp)
                        else await self.expr(n.elt, scope)
                    )
                    self.bound(output)
                    return
                g = n.generators[depth]
                for v in await self.expr(g.iter, scope):
                    child = dict(scope)
                    await self.target(g.target, v, child)
                    if all([await self.expr(x, child) for x in g.ifs]):
                        await visit(depth + 1, child)

            await visit(0, dict(env))
            return (
                dict(output)
                if isinstance(n, ast.DictComp)
                else set(output)
                if isinstance(n, ast.SetComp)
                else output
            )
        if isinstance(n, ast.Call):
            args = [await self.expr(x, env) for x in n.args]
            kwargs = {x.arg: await self.expr(x.value, env) for x in n.keywords}
            if isinstance(n.func, ast.Name):
                name = n.func.id
                if name in self.caps:
                    result = self.caps[name](*args, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                    return freeze(self.bound(result))
                if name == "range":
                    r = range(*args)
                    self.bound(r) if len(r) <= self.limits.container_items else self.fail(
                        "Range limit exceeded"
                    )
                    return list(r)
                if name == "str" and args and isinstance(args[0], (Mapping, list, tuple, set)):
                    self.fail("Container-to-string conversion is unsupported")
                result = BUILTINS[name](*args, **kwargs)
                if name in {"enumerate", "zip"}:
                    result = list(result)
                return self.bound(result)
            obj = await self.expr(n.func.value, env)
            method = n.func.attr
            safe = (
                (isinstance(obj, Mapping) and method in {"get", "keys", "values", "items"})
                or (
                    isinstance(obj, str)
                    and method
                    in METHODS - {"get", "keys", "values", "items", "append", "extend", "add"}
                )
                or (type(obj) is list and method in {"append", "extend", "count"})
                or (type(obj) is set and method == "add")
                or (type(obj) is tuple and method == "count")
            )
            if not safe:
                self.fail("Method not available on this immutable value")
            if isinstance(obj, str) and method == "replace" and len(args) >= 2:
                count = obj.count(args[0]) if args[0] else len(obj) + 1
                if len(args) > 2:
                    count = min(count, max(args[2], 0))
                if (
                    len(obj) + count * max(0, len(args[1]) - len(args[0]))
                    > self.limits.result_bytes
                ):
                    self.fail("Replacement limit exceeded")
            if isinstance(obj, str) and method == "join" and args:
                seq = args[0]
                if (
                    not isinstance(seq, (list, tuple))
                    or sum(len(x) for x in seq) + len(obj) * max(0, len(seq) - 1)
                    > self.limits.result_bytes
                ):
                    self.fail("Join limit exceeded")
            # Methods are selected here, never exposed as first-class callable values.
            result = getattr(obj, method)(*args, **kwargs)
            self.bound(obj)
            if method in {"keys", "values", "items"}:
                result = list(result)
            return self.bound(result)
        self.fail(f"Unsupported expression: {type(n).__name__}")
