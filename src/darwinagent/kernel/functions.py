"""F admission, actual-data trials and bounded invocation."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from darwinagent.operators.data import DataCapabilities
from darwinagent.operators.sandbox import Interpreter, Limits, admit
from darwinagent.contracts import plain
from .spec import validate_value


class FunctionRegistry:
    def __init__(self, bundle, limits=Limits(), forbidden_questions=()):
        self.bundle, self.limits = bundle, limits
        self.functions = {
            a.id: (a, admit(a.content, "F", forbidden_questions))
            for a in bundle.assets.assets
            if a.kind == "F"
        }

    def descriptions(self):
        return [
            {
                "id": a.id,
                "description": a.description,
                "input_contract": plain(a.input_contract),
                "output_contract": plain(a.output_contract),
            }
            for a, _ in self.functions.values()
        ]

    def call(self, asset_id, params, graph_result, _observation=None):
        self.bundle.verify()
        if asset_id not in self.functions:
            raise ValueError("Unregistered tool")
        a, fn = self.functions[asset_id]
        caps = DataCapabilities(graph_result)
        interpreter = Interpreter(fn, caps.registry(), self.limits)
        started = time.monotonic()
        try:
            validate_value(params, a.input_contract, "tool.params")
            result = interpreter.execute(params)
        finally:
            if _observation is not None:
                _observation.update(
                    steps_used=interpreter.steps,
                    step_budget=self.limits.steps,
                    elapsed_ms=round(1000 * (time.monotonic() - started), 1),
                    read_operations=caps.read_operations,
                    capability_calls=dict(caps.capability_calls),
                    traverse_observations=list(caps.traverse_observations),
                    traverse_directions=sorted(caps.traverse_directions),
                )
        validate_value(result, a.output_contract, "tool.result")
        # 证据边界（专家实锤＋用户批准）：作答可引用的证据面＝工具实际「返回」的行；
        # F 内部读过但未返回的行只进读血缘（read_node_ids，溯源用）。返回行里出现的
        # node_id 还必须真实读过（防伪造引用）。
        returned = set()

        def _collect(value):
            if isinstance(value, Mapping):
                nid = value.get("node_id")
                if isinstance(nid, str):
                    returned.add(nid)
                for item in value.values():
                    _collect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    _collect(item)

        _collect(result)
        node_ids = sorted(returned & caps.read_ids)
        read_node_ids = sorted(caps.read_ids)
        source_ids = sorted({s for rid in node_ids for s in caps.rows[rid]["source_ids"]})
        if _observation is not None:
            _observation["result_bytes"] = len(json.dumps(result, ensure_ascii=False).encode())
        return {
            "asset_id": a.id,
            "asset_fingerprint": a.fingerprint,
            "data": result,
            "node_ids": node_ids,
            "read_node_ids": read_node_ids,
            "source_ids": source_ids,
            "read_operations": caps.read_operations,
            "capability_calls": dict(caps.capability_calls),
            "traverse_directions": sorted(caps.traverse_directions),
        }

    def trial(self, graph_result, samples):
        records = []
        if set(samples) != set(self.functions):
            raise ValueError("Every F requires an actual-data trial")
        for asset_id, params_list in samples.items():
            if not params_list:
                raise ValueError("Empty trial set")
            for params in params_list:
                records.append(self.call(asset_id, params, graph_result))
        return records

    def trial_report(self, graph_result, samples):
        """Run independent F trials without discarding successes after the first failure."""
        rows = []
        for aid in sorted(set(samples) | set(self.functions)):
            asset = self.functions.get(aid, (None, None))[0]
            values = samples.get(aid, ())
            if not values:
                rows.append(
                    {
                        "asset_id": aid,
                        "asset_fingerprint": None if asset is None else asset.fingerprint,
                        "status": "incomplete",
                        "error": "Empty or unregistered trial set",
                    }
                )
            for index, params in enumerate(values):
                row = {
                    "asset_id": aid,
                    "asset_fingerprint": None if asset is None else asset.fingerprint,
                    "sample_index": index,
                    "status": "passed",
                }
                try:
                    row["result"] = self.call(aid, params, graph_result, _observation=row)
                except Exception as exc:
                    row.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                rows.append(row)
        return rows
