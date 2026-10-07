"""Experiment identity, bootstrap, baseline validation and lifecycle orchestration."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from darwinagent.kernel import KernelBundle
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import transport_identity

from .bootstrap import AssetBootstrapper
from .constants import ADMISSION_ATTEMPTS
from .feedback import (
    _per_case_feedback_facts,
    _wiki_training_evidence,
    pipeline_active_stages,
    question_identity,
    training_feedback,
)
from .wiki import WikiMaintainer
from .wiki_evidence import asset_evidence, safe_feedback


async def run(
    case_ids,
    spec,
    rounds=2,
    resume=False,
    stop_file=None,
    b0_gate=None,
    stage_gate=None,
    scope=(),
    *,
    client_factory,
    dynamic_trial_graphs_hook,
    preflight_hook,
    rebuild_trial_supply_hook,
    record_smoke_hook,
    smoke_gate_hook,
    stage_hook,
    wiki_bootstrap_trials_hook,
    wiki_report_valid_hook,
    adapter,
    bootstrap_context,
    bootstrap_trial_graph,
    config,
    connection_config,
    dynamic_trial,
    frozen,
    graph_builder,
    optimization_mode,
    policy,
    proposal_attempts,
    revisions,
    root,
    round_deadline_s,
    seed_assets,
    snapshot_root,
    validation_plan,
    verify,
    wiki_call_limit,
    coordinate_rounds,
):
    """case_ids: one conversation id or a tuple; every case runs fully each round on the
    same candidate bundle. rounds=None iterates until stop_file appears. scope limits
    which asset kinds a round may patch (P first; F/S open by attribution later)."""
    verify()
    cases = [adapter.generation_input(c) for c in case_ids]
    root.mkdir(parents=True, exist_ok=True)
    declaration = {
        "cases": list(case_ids),
        "case_fingerprint": digest([c.to_dict() for c in cases]),
        "aggregation": "sum",
        "task": spec.declaration(),
        "config": config.to_dict(),
        "connection": transport_identity(type("Connection", (), {"cfg": connection_config})()),
        "policy": asdict(policy),
        "frozen_files": frozen,
        "seed_assets": [str(seed_assets)] if seed_assets else [],
        "scope": list(scope or ()),
        "source_layers": sorted({b.source.kind for case in cases for b in case.corpus}),
        "snapshots": (
            {c.id: (snapshot_root / c.id / "manifest.json").read_text() for c in cases}
            if snapshot_root is not None
            else {}
        ),
        "dynamic_trial": dynamic_trial,
    }
    if optimization_mode == "wiki":
        declaration["optimization"] = {
            "mode": "wiki",
            "maintenance_call_limit": wiki_call_limit,
        }
    if round_deadline_s is not None or proposal_attempts != ADMISSION_ATTEMPTS:
        declaration["round_controls"] = {
            "deadline_s": round_deadline_s,
            "proposal_attempts": proposal_attempts,
        }
    if graph_builder is not None:
        declaration["graph_mode"] = "frozen-memory-rebuilt-graph"
    if validation_plan is not None:
        declaration["validation"] = {
            "case_fingerprint": digest(validation_plan["case"].to_dict()),
            "policy": asdict(validation_plan["policy"]),
        }
    declaration = json.loads(json.dumps(declaration, ensure_ascii=False))
    experiment_path = root / "experiment.json"
    if experiment_path.exists():
        recorded = json.loads(experiment_path.read_text())
        # 兼容旧声明：rounds 已移出身份（2026-10-06 缺口①修复——轮数是预算
        # 上限不是数据身份）；旧运行的 wiki/检查点身份仍按冻结纪律校验。
        recorded.pop("rounds", None)
        if not resume or recorded != declaration:
            raise ValueError(
                "Existing experiment requires explicit resume with exactly the same identity"
            )
        if "rounds" in json.loads(experiment_path.read_text()):
            # 旧格式迁移：校验通过后落盘新形态，旧键不再残留
            atomic_json(experiment_path, declaration)
    else:
        atomic_json(experiment_path, declaration)
    wiki = (
        WikiMaintainer(root, digest(declaration), client_factory, config, wiki_call_limit)
        if optimization_mode == "wiki"
        else None
    )
    if wiki is not None:
        wiki.reconcile()
    try:
        bundle_path = root / "B0" / "assets"
        if (bundle_path / "manifest.json").exists():
            bundle = KernelBundle(bundle_path)
        elif seed_assets is not None:
            # seed-assets 入口（缺口③修复，2026-10-06）：从锁定 bundle 直接锚定
            # B0（跳过冷启动引导），准入/冒烟/评分门槛原样作用——种子资产不豁免
            # 任何检查；Wiki 仍从零开始（跨运行经验不导入）。
            import shutil

            seed = Path(seed_assets)
            seeded = KernelBundle(seed)  # 构造即校验 manifest/资产
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(seed, bundle_path)
            bundle = KernelBundle(bundle_path)
            atomic_json(root / "B0" / "seed.json", {"source": str(seed), "version": seeded.version})
        else:
            client = client_factory("B0")

            def trial_record(report):
                path = root / "B0" / "bootstrap-trials" / f"{digest(report)}.json"
                atomic_json(path, report)

            try:
                bundle = await AssetBootstrapper().initialize(
                    cases,
                    spec,
                    client,
                    config,
                    bundle_path,
                    structure_sample=bootstrap_context,
                    trial_graph=bootstrap_trial_graph,
                    snapshot_root=snapshot_root,
                    trial_record=trial_record if wiki else None,
                    trial_supply=(
                        dynamic_trial_graphs_hook
                        if dynamic_trial
                        else (
                            rebuild_trial_supply_hook
                            if graph_builder is not None and snapshot_root is not None
                            else None
                        )
                    ),
                )
            except Exception as exc:
                if wiki is not None:
                    await wiki_bootstrap_trials_hook(wiki)
                    call_path = root / "B0" / "bootstrap-call.json"
                    saved = json.loads(call_path.read_text()) if call_path.exists() else {}
                    await wiki.record(
                        "B0",
                        "bootstrap_failure",
                        {
                            "error": f"{type(exc).__name__}: {exc}"[:1500],
                            "protocol_events": [
                                {k: e.get(k) for k in ("attempt", "status", "error")}
                                for e in saved.get("events", ())
                            ],
                        },
                        scope="trial",
                        source=str(call_path),
                        infer=True,
                    )
                raise
            finally:
                await client.aclose()
        if wiki is not None:
            await wiki_bootstrap_trials_hook(wiki)
        if snapshot_root is not None or bootstrap_trial_graph is not None or dynamic_trial:
            b0_report = root / "B0" / "admission.json"
            b0_snapshot = root / "B0" / "optimization" / "admission.json"
            prior = json.loads(b0_snapshot.read_text()) if b0_snapshot.exists() else {}
            try:
                if not (wiki is not None and wiki_report_valid_hook(prior, bundle)):
                    await preflight_hook(bundle, spec, cases[0].questions[0], cases)
            finally:
                if wiki is not None and b0_report.exists():
                    if not b0_snapshot.exists() or not wiki_report_valid_hook(prior, bundle):
                        fresh = json.loads(b0_report.read_text())
                        fresh.pop("smoke", None)
                        atomic_json(b0_snapshot, fresh)
                    await wiki.record(
                        "B0",
                        "admission",
                        json.loads(b0_snapshot.read_text()),
                        source=str(b0_snapshot),
                        scope="admission",
                    )
        if stage_gate is not None:
            stage_gate("B0")
        if snapshot_root is not None or dynamic_trial:
            recorded = (
                json.loads((root / "B0" / "admission.json").read_text())
                if (root / "B0" / "admission.json").exists()
                else {}
            )
            if wiki is not None and wiki_report_valid_hook(recorded, bundle, smoke=True):
                smoke_error = None
            else:
                smoke_started = time.monotonic()
                smoke_error = await smoke_gate_hook(cases, spec.with_bundle(bundle))
                record_smoke_hook(bundle, smoke_error, time.monotonic() - smoke_started)
            if wiki is not None:
                report = json.loads((root / "B0" / "admission.json").read_text())
                await wiki.record(
                    "B0",
                    "smoke",
                    report.get("smoke", {}),
                    source=str(root / "B0" / "admission.json"),
                    scope="smoke",
                )
            if smoke_error:
                summary = {
                    "status": "blocked_b0",
                    "reason": "smoke gate: 3 题全灭（确定性缺陷）",
                    "smoke_error": smoke_error,
                    "rounds": [],
                    "adopted_version": None,
                }
                atomic_json(root / "summary.json", summary)
                raise ValueError("冒烟门拒绝（全量提交前 3 题全灭）: " + smoke_error)
        results, baseline = await stage_hook("B0", cases, spec.with_bundle(bundle))
        if wiki is not None:
            raw = training_feedback(
                cases,
                results,
                _per_case_feedback_facts(root, "B0", cases),
                baseline,
                active_stages=pipeline_active_stages(snapshot_root),
            )
            await wiki.record(
                "B0",
                "formal",
                {
                    **safe_feedback(raw),
                    **asset_evidence(bundle),
                    **_wiki_training_evidence(
                        cases,
                        results,
                        diagnostics=_per_case_feedback_facts(root, "B0", cases),
                    ),
                },
                category="strategy",
                scope="formal",
                training_ids=[tid for case in cases for tid in question_identity(case)],
                source=str(root / "B0" / "stage.json"),
                infer=True,
            )
        if b0_gate is not None and not b0_gate(baseline):
            summary = {
                "status": "blocked_b0",
                "reason": "baseline gate rejected the B0 evaluation",
                "baseline": baseline.to_dict(),
                "rounds": [],
                "adopted_version": None,
            }
            atomic_json(root / "summary.json", summary)
            print(
                json.dumps({"stage": "B0", "status": "blocked_b0"}, ensure_ascii=False),
                flush=True,
            )
            return summary
        adopted = revisions.publish(
            bundle,
            root / "published",
            {"accepted": True, "reasons": ["initial_validated_baseline"]},
        )
        # 新模式验证基线（recheck4）：B0 对固定验证题建基线；候选轮的验证只回
        # 聚合选版指标进决策与 Wiki（逐题 gold/答案/诊断不进提案器）。
        val_case = None
        val_baseline = None
        val_history = {}
        if validation_plan is not None:
            val_case = validation_plan["case"]
            _, val_baseline = await stage_hook("B0-val", (val_case,), spec.with_bundle(bundle))
            val_history["B0"] = val_baseline.to_dict()
            atomic_json(root / "validation.json", val_history)
        return await coordinate_rounds(
            cases,
            spec,
            wiki,
            adopted,
            baseline,
            results,
            val_case,
            val_baseline,
            val_history,
            rounds,
            stop_file,
            stage_gate,
            scope,
        )
    except Exception as exc:
        atomic_json(
            root / "failure.json",
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
