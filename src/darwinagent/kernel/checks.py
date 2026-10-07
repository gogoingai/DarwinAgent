"""Task C returns opinions. Fixed validation and publication do not live here."""

from __future__ import annotations

from darwinagent.operators.sandbox import Interpreter, Limits, admit


class CheckRegistry:
    def __init__(self, bundle, limits=Limits(), forbidden_questions=()):
        self.bundle, self.limits = bundle, limits
        self.checks = {
            a.id: (a, admit(a.content, "C", forbidden_questions))
            for a in bundle.assets.assets
            if a.kind == "C"
        }

    def budget(self, snapshot):
        """检查步数预算随受检快照规模伸缩（图阶段 C 必须能遍历全图），但保持受限上限。
        agentic_v3 首轮事故：1112 节点的图检查需 41,706 步 > function_steps=30,000。"""
        nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
        scale = 80 * len(nodes) if isinstance(nodes, (list, tuple)) else 0
        steps = min(self.limits.steps * 20, self.limits.steps + scale)
        from dataclasses import replace

        return replace(self.limits, steps=steps)

    def run(self, stage, snapshot):
        self.bundle.verify()
        opinions = []
        limits = self.budget(snapshot)
        for a, fn in self.checks.values():
            if a.stage != stage:
                continue
            import time as _t

            started = _t.monotonic()
            interp = Interpreter(fn, {}, limits)
            result = interp.execute(snapshot)
            if (
                not isinstance(result, dict)
                or set(result) != {"ok", "issues"}
                or type(result["ok"]) is not bool
                or not isinstance(result["issues"], list)
                or not all(isinstance(x, str) and x.strip() for x in result["issues"])
            ):
                raise ValueError("C must return {ok: bool, issues: [nonempty string]}")
            if result["ok"] != (not result["issues"]):
                raise ValueError("Inconsistent C opinion")
            opinions.append(
                {
                    "check_id": a.id,
                    "fingerprint": a.fingerprint,
                    "steps_used": interp.steps,
                    "step_budget": limits.steps,
                    "elapsed_ms": round(1000 * (_t.monotonic() - started), 1),
                    **result,
                }
            )
        return opinions

    def trial_report(self, stage, snapshot):
        """Keep independently executable C opinions and failures in one admission report."""
        rows = []
        for aid, (asset, fn) in sorted(self.checks.items()):
            if asset.stage != stage:
                continue
            import time

            started = time.monotonic()
            limits = self.budget(snapshot)
            interp = Interpreter(fn, {}, limits)
            row = {"check_id": aid, "fingerprint": asset.fingerprint, "stage": stage}
            try:
                result = interp.execute(snapshot)
                if (
                    not isinstance(result, dict)
                    or set(result) != {"ok", "issues"}
                    or type(result["ok"]) is not bool
                    or not isinstance(result["issues"], list)
                    or not all(isinstance(x, str) and x.strip() for x in result["issues"])
                    or result["ok"] != (not result["issues"])
                ):
                    raise ValueError(
                        "C must return consistent {ok: bool, issues: [nonempty string]}"
                    )
                row.update(result, status="passed" if result["ok"] else "failed")
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            row.update(
                steps_used=interp.steps,
                step_budget=limits.steps,
                elapsed_ms=round(1000 * (time.monotonic() - started), 1),
            )
            rows.append(row)
        return rows


def enforce_opinions(opinions, context=""):
    """图阶段 C 的否决必须被采纳（评审二）：任一 ok=False 即拒绝准入，check_id、
    issues 与预算信息随错误反馈给资产生成模型修订。冷启动、候选预检、外测同一条规则。"""
    failures = [o for o in opinions if not o.get("ok")]
    if failures:
        detail = [
            {
                "check_id": o.get("check_id"),
                "issues": o.get("issues"),
                "steps_used": o.get("steps_used"),
                "step_budget": o.get("step_budget"),
            }
            for o in failures
        ]
        raise ValueError(f"{context}图检查否决: {detail}")
    return opinions


def _contract_instance(contract, seed=0):
    """契约声明语言（封闭关键字集，见 spec.contract_errors）的最小合法实例：
    array→[items]；object→{required 属性}；enum→首值；null→None。seed 使整数叶
    递增——多元素实例得到内部一致的天序（days=[1,2]）而非复制的 days=[1,1]
    （2026-10-05 审查 P1：占位实例不得逼模型删语义检查）。生成失败返回
    None——调用方退回字符串电池，绝不因生成器局限误判良好候选。"""
    from collections.abc import Mapping

    if not isinstance(contract, Mapping):
        return None
    if "enum" in contract:
        values = list(contract["enum"])
        return values[0] if values else None
    kind = contract.get("type")
    if kind == "array":
        if "items" in contract:
            item = _contract_instance(contract["items"], seed)
            return None if item is None else [item]
        return []
    if kind == "object":
        props = contract.get("properties", {})
        out = {}
        for key in contract.get("required", ()):
            if key not in props:
                continue
            value = _contract_instance(props[key], seed)
            if value is None and props[key].get("type") not in ("null", "any", "string"):
                return None
            out[key] = value
        return out
    if kind == "integer":
        return 1 + seed
    if kind == "number":
        return 1.0 + seed
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return ""  # string / any / 未声明


def synthetic_answer_battery(rows, question_text, parameters=None, answer_contract=None):
    """答案阶段 C 电池：[(期望, 快照)]。期望三档——
    must_pass：合法形态，任何 C 必须接受（真实行/拒答形态；契约占位实例不在此列）；
    structure：契约最小占位实例——只验证冻结容器下的可执行与意见良构，C 接受或
      语义拒绝都合法（2026-10-05 审查 P1：结构合法≠语义正确，不得以占位实例
      逼模型删除有效语义检查；语义 must_pass 只来自经复现验证的真实候选）；
    must_reject：畸形候选（answered 空答案零证据）——存在答案阶段 C 时必须被拒。
    string/any 契约路径＝原三形态（单事实/列举/拒答全 must_pass），与既有会话任务
    准入语义逐字节一致。"""
    from collections.abc import Mapping

    root = (answer_contract or {}).get("type") if isinstance(answer_contract, Mapping) else None
    battery = []
    if root not in (None, "string", "any"):
        minimal = _contract_instance(answer_contract)
        if minimal is not None:
            import json as _json

            evidence = list(rows[:1])
            battery.append(
                (
                    "structure",
                    {
                        "stage": "answer",
                        "question": question_text,
                        "parameters": parameters or {},
                        "status": "answered",
                        "answer": _json.dumps(minimal, ensure_ascii=False),
                        "node_ids": [r["node_id"] for r in evidence],
                        "evidence": evidence,
                        "visible_evidence": list(rows[:3]),
                        "structured_answer": minimal,
                    },
                )
            )
            if isinstance(minimal, list):
                # 多元素实例在 items 层取 seed：天序内部一致（days=[1,2]），
                # 遍历冻结 tuple 的路径被真实走到。
                items = [
                    _contract_instance(answer_contract.get("items", {}), seed) for seed in range(2)
                ]
                items = [x for x in items if x is not None]
                if len(items) > 1:
                    battery.append(
                        (
                            "structure",
                            {
                                "stage": "answer",
                                "question": question_text,
                                "parameters": parameters or {},
                                "status": "answered",
                                "answer": _json.dumps(items, ensure_ascii=False),
                                "node_ids": [r["node_id"] for r in evidence],
                                "evidence": evidence,
                                "visible_evidence": list(rows[:3]),
                                "structured_answer": items,
                            },
                        )
                    )
        abstain = {
            "stage": "answer",
            "question": question_text,
            "parameters": parameters or {},
            "status": "abstained",
            "answer": "记忆中没有支持该问题的记录，无法回答。",
            "node_ids": [],
            "evidence": [],
            "visible_evidence": list(rows[:3]),
            "structured_answer": None,
        }
        return battery + [("must_pass", abstain)]
    single = synthetic_answer_snapshot(rows, question_text, parameters)
    listed = synthetic_answer_snapshot(rows[1:3] or rows, question_text, parameters)
    listed["answer"] = f"根据记忆：1) {single['answer']}；2) 另一条相关记录。"
    listed["node_ids"] = [r["node_id"] for r in (rows[1:3] or rows)]
    listed["evidence"] = list(rows[1:3] or rows)
    abstain = {
        "stage": "answer",
        "question": question_text,
        "parameters": parameters or {},
        "status": "abstained",
        "answer": "记忆中没有支持该问题的记录，无法回答。",
        "node_ids": [],
        "evidence": [],
        "visible_evidence": list(rows[:3]),
        "structured_answer": None,
    }
    return [("must_pass", single), ("must_pass", listed), ("must_pass", abstain)]


def counterexample_snapshot(counterexample, rows, question_text, parameters=None):
    """任务适配层反例 → must-reject 快照：候选片段（status/answer[/node_ids]）与
    真实行/问题组合，语义上必须被答案阶段 C 拒绝。
    answer_examples 复用同一构造，但优先使用夹具自带的问题/参数（三次复查 P1：
    正例必须独立一致，不得把旧行程错装进当前 case 的题目）；自带 evidence_rows
    的小型独立证据图整集进入快照（四次复查反例 A：正例的证据不得取活案例图
    首行——接地语义 C 需要看到夹具引用的真实实体）。"""
    import json as _json

    candidate = counterexample.get("candidate") or {}
    fixture_rows = list(counterexample.get("evidence_rows") or ())
    evidence_rows = fixture_rows or list(rows)
    answer = str(candidate.get("answer", ""))
    structured = None
    try:
        structured = _json.loads(answer)
    except Exception:
        structured = None
    node_ids = list(candidate.get("node_ids") or [r.get("node_id") for r in evidence_rows[:1]])
    evidence = evidence_rows if fixture_rows else list(rows[:1])
    visible = evidence_rows if fixture_rows else list(rows[:3])
    return {
        "stage": "answer",
        "question": counterexample.get("question") or question_text,
        "parameters": counterexample.get("parameters")
        if counterexample.get("parameters") is not None
        else (parameters or {}),
        "status": candidate.get("status", "answered"),
        "answer": answer,
        "node_ids": node_ids,
        "evidence": evidence,
        "visible_evidence": visible,
        "structured_answer": structured,
    }


def synthetic_answer_variants(rows, question_text, parameters=None, answer_contract=None):
    """兼容旧签名：返回电池快照列表（期望档见 synthetic_answer_battery）。"""
    return [
        snapshot
        for _, snapshot in synthetic_answer_battery(
            rows, question_text, parameters, answer_contract
        )
    ]


def synthetic_invalid_answer_snapshot(question_text, parameters=None):
    """畸形候选（answered 但答案为空、零证据）：若存在答案阶段 C，它必须拒绝——
    只放行良好成形答案的 C 是装饰品（专家缺口：缺非法候选用例）。"""
    return {
        "stage": "answer",
        "question": question_text,
        "parameters": parameters or {},
        "status": "answered",
        "answer": "",
        "node_ids": [],
        "evidence": [],
        "visible_evidence": [],
        "structured_answer": None,
    }


def enforce_rejection(opinions, context):
    if opinions and all(o.get("ok") for o in opinions):
        raise ValueError(f"{context}: 畸形候选未被任何 C 拒绝（装饰性 C，准入拒绝）")


def synthetic_answer_snapshot(rows, question_text, parameters=None, answer_contract=None):
    """良好成形的答案阶段检查快照（真实记忆行＋真实问题文本）。冷启动准入用它真实执行
    答案阶段 C（agentic_v6 G1 B0 全灭事故：模型自写 C 结构不兼容、全盘否决每个候选，
    而答案阶段 C 此前只有静态 admit、从未被执行过）。answer_contract 根类型非 string
    时答案负载按契约合成最小合法实例（2026-10-05 Travel：字符串答案对数组契约是畸形输入）。"""
    import json as _json

    evidence = list(rows[:1])
    from collections.abc import Mapping

    root = (answer_contract or {}).get("type") if isinstance(answer_contract, Mapping) else None
    structured = None
    if root not in (None, "string", "any"):
        structured = _contract_instance(answer_contract)
    if structured is not None:
        return {
            "stage": "answer",
            "question": question_text,
            "parameters": parameters or {},
            "status": "answered",
            "answer": _json.dumps(structured, ensure_ascii=False),
            "node_ids": [r["node_id"] for r in evidence],
            "evidence": evidence,
            "visible_evidence": list(rows[:3]),
            "structured_answer": structured,
        }
    answer = str(evidence[0].get("陈述") or evidence[0].get("statement") or "记忆支持的陈述")
    try:
        structured = _json.loads(answer)
    except Exception:
        structured = None
    return {
        "stage": "answer",
        "question": question_text,
        "parameters": parameters or {},
        "status": "answered",
        "answer": answer,
        "node_ids": [r["node_id"] for r in evidence],
        "evidence": evidence,
        "visible_evidence": list(rows[:3]),
        "structured_answer": structured,
    }
