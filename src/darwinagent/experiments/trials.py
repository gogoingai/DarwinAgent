"""Experiment trials helpers; independent of the controller."""

from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from collections.abc import Mapping
from darwinagent.contracts import plain
from darwinagent.kernel.revision import training_id
from darwinagent.runtime.artifacts import atomic_json, digest


def stress_trial_samples(base_inputs, graph):
    """F 单元测试——穷尽版（用户标准：单测应避免所有故障；历史回归驱动）。
    触发面穷尽：A 行集形态（空/签名穷尽/全量/缺字段）＋B 标量边界（全空最宽＋图内真值）。
    历史回归两大根因修复：①真实 trial_inputs 是冻结映射与元组——按 Mapping 判定并
    plain() 解包，否则样本恒空（电池空转事故）；②自扫描型 F 不收 rows——必须扫标量
    宽值（subject='' → 内部扫描命中全图 → 预算爆/列表字段流过 str() 当场触发）。"""
    from collections.abc import Mapping
    from darwinagent.contracts import plain
    from darwinagent.operators.data import DataCapabilities

    rows = list(DataCapabilities(graph).rows.values())

    def signature(row):
        return tuple(sorted((k, type(v).__name__) for k, v in row.items() if k != "node_id"))

    shape_rows = []
    seen_sig = set()
    for r in rows:
        sig = signature(r)
        if sig not in seen_sig:
            seen_sig.add(sig)
            shape_rows.append(r)

    real_scalars = {}
    for field in ("主体", "类型", "主题", "编号"):
        vals = [str(r[field]) for r in rows[:120] if isinstance(r.get(field), str) and r[field]]
        if vals:
            real_scalars[field] = list(dict.fromkeys(vals))[:4]

    out = []
    for raw_base in base_inputs[:3]:
        if not isinstance(raw_base, Mapping):
            continue
        base = plain(raw_base)
        takes_rows = isinstance(base.get("rows"), (list, tuple))
        if takes_rows:
            out.append({**base, "rows": []})
            out.append({**base, "rows": [dict(r) for r in shape_rows]})
            out.append({**base, "rows": [dict(r) for r in rows]})
            # 中等规模（历史回归：全量先撞 traverse 100 节点上限报 Invalid traversal，
            # 运行期预算爆真实发生在 30-100 节点档——两档都要扫）
            out.append({**base, "rows": [dict(r) for r in rows[:60]]})
            out.append({**base, "rows": [dict(r) for r in rows[:100]]})
            stripped = [
                {k: x for k, x in r.items() if k not in ("日期", "主题", "类型")}
                for r in shape_rows
            ]
            out.append({**base, "rows": stripped})
            out.append(
                {
                    **base,
                    "rows": [dict(r) for r in rows],
                    **{k: "" for k, v in base.items() if isinstance(v, str) and k != "rows"},
                }
            )
        else:
            out.append({k: ("" if isinstance(v, str) else v) for k, v in base.items()})
            # 逐字段空串（保留其余真实值）：历史故障形态＝真实主体＋空可选过滤＋
            # 大 limit（conv-30 f_filter_facts 30001/30000 步，二次复查 P1）。
            for k, v in base.items():
                if isinstance(v, str) and v:
                    variant = dict(base)
                    variant[k] = ""
                    out.append(variant)
            # 同类实参互换：同键同型保证契约合法，真值变化仍走不同数据路径
            # （2026-10-05 Travel：table/city 类枚举参数的空串变体非法，压力覆盖空转）。
            for other in base_inputs[:3]:
                if not isinstance(other, Mapping) or other is raw_base:
                    continue
                variant = dict(base)
                for k, v in plain(other).items():
                    if k in variant and type(v) is type(variant[k]) and v != variant[k]:
                        variant[k] = v
                if variant != base:
                    out.append(variant)
            # 字符串字段的清空子集（≤16 个变体）：真实故障常是「真实主体＋多个可选
            # 过滤同时为空＋大 limit」（conv-30，二次复查 P1）——单字段清空不够。
            import itertools as _it

            string_keys = [k for k, v in base.items() if isinstance(v, str) and v]
            added = 0
            for r in range(len(string_keys) + 1):
                for sub in _it.combinations(string_keys, r):
                    if added >= 16:
                        break
                    variant = dict(base)
                    for k in sub:
                        variant[k] = ""
                    if variant not in out:
                        out.append(variant)
                        added += 1
                if added >= 16:
                    break
            # 数值放大×全部已有变体的组合：可放大读/返行数的数值参数（limit 类）
            # 必须与宽过滤/真实字段变体一起试跑，只放大原 base 会漏掉「真实主体＋
            # 空过滤＋大 limit」的真实故障形态（二次复查 P1）。不放大预算。
            numeric_keys = [
                k for k, v in base.items() if type(v) is int and not isinstance(v, bool) and v >= 0
            ]
            combined = []
            for variant in out:
                for k in numeric_keys:
                    amplified = dict(variant)
                    amplified[k] = max(500, base[k] * 25)
                    if amplified != variant:
                        combined.append(amplified)
            out.extend(combined)
        for field, vals in real_scalars.items():
            for v in vals[:2]:
                variant = {k: (v if k == field else x) for k, x in base.items() if k != "rows"}
                if takes_rows:
                    variant["rows"] = [dict(r) for r in shape_rows]
                out.append(variant)
    return out
