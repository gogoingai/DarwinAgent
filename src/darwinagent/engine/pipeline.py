"""The single fixed generation pipeline; datasets do not supply execution callbacks.

Extraction and assembly are two separate stages: the ExtractionAgent yields an
independent MemoryResult of atomic facts, and the GraphAssembler deterministically
builds the fact-anchored graph from that memory without model calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType

import networkx as nx

from darwinagent.agents import AnswerAgent, ExtractionAgent
from darwinagent.config import RunConfig
from darwinagent.contracts import AnswerResult, GraphResult, MemoryResult, RunResult, plain
from darwinagent.kernel.execution import KernelRuntime
from darwinagent.kernel.validation import validate_case, validate_published
from darwinagent.kg.assembler import GraphAssembler
from darwinagent.kg.graph import load_graph, save_graph
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import assert_files, snapshot_files, transport_identity

# 搬运字节断言的显式豁免：检查点装载逻辑本身（不参与答案计算，回归全绿护航）。
# 豁免必须逐一列名——除此之外任何答案路径文件漂移都会拒绝搬运。
CARRY_NEUTRAL_SUFFIXES = ("darwinagent/engine/pipeline.py", "darwinagent/llm/client.py")


def carried_acceptor(root, identity, framework):
    """经验证的答案检查点搬运（用户指令：不要从头跑）：CARRIED.json 旁车记录被接受的
    源身份与源框架映射。当且仅当「答案路径文件」（darwinagent/ 全部；experiments 编排/评测层与
    CARRY_NEUTRAL 豁免表除外）与源运行逐字节一致时，源身份的检查点被接受并惰性重锚到
    当前身份。管道在此自行复核哈希，旁车手写无法混入未验证的外来检查点。"""
    sidecar = Path(root) / "CARRIED.json"
    if not sidecar.exists():
        return lambda stored: stored == identity
    note = json.loads(sidecar.read_text())
    accepted = set(note.get("accepted_identities", ()))
    for path, sha in note.get("source_framework", {}).items():
        if "/darwinagent/experiments/" in path or any(
            path.endswith(sfx) for sfx in CARRY_NEUTRAL_SUFFIXES
        ):
            continue  # 编排/评测层与显式豁免：差异不参与答案计算
        if framework.get(path) != sha:
            raise ValueError(f"答案路径文件与搬运源不一致，检查点不可复用: {path}")
    return lambda stored: stored == identity or stored in accepted


def answer_source_record(row, question_id, workspace=None):
    """Read the checkpoint's producer source; never replace missing history with current input."""
    result = {
        "question_id": question_id,
        **{
            key: row.get(key)
            for key in (
                "identity",
                "asset_version",
                "graph_fingerprint",
                "graph_path",
                "case_path",
                "case_ref",
                "journal_path",
                "question_version",
                "transport",
            )
        },
    }
    result["source_status"] = "unknown"
    try:
        if row.get("case_ref") and workspace is not None:
            original_case = workspace.read_json(row["case_ref"])
        elif row.get("case_path"):
            original_case = json.loads(Path(row["case_path"]).read_text())
        else:
            return result
        if row.get("case_digest") and digest(original_case) != row["case_digest"]:
            raise ValueError("Original case checksum mismatch")
        result["original_case"] = original_case
        result["source_status"] = "recorded" if row.get("case_digest") else "legacy_unverified"
    except (OSError, ValueError, KeyError) as exc:
        result["source_error"] = f"{type(exc).__name__}: {exc}"
    return result


class Pipeline:
    def __init__(
        self,
        client,
        work_dir,
        frozen_snapshot=None,
        embedder_factory=None,
        graph_builder=None,
        workspace=None,
        preparation_root=None,
    ):
        # frozen_snapshot: 共享冻结记忆快照目录（graph/facts/vector＋manifest）。注入时
        # 抽取与构图全部跳过——图是指纹校验的冻结输入数据，臂间唯一差异是资产。
        # embedder_factory: 查询嵌入端点注入（默认读 EMBEDDING_* 环境变量）。
        # graph_builder keeps the snapshot memory/vector plane immutable while rebuilding
        # the graph under current assets. Model builders receive runtime/client/config;
        # legacy deterministic callbacks remain supported for explicit reproduction.
        self.client, self.work_dir = client, Path(work_dir)
        self.workspace = workspace
        self.preparation_root = Path(preparation_root) if preparation_root is not None else None
        self.frozen_snapshot = Path(frozen_snapshot) if frozen_snapshot is not None else None
        self.embedder_factory = embedder_factory
        self.graph_builder = graph_builder

    def _graph_mode_identity(self):
        """运行身份中的图模式标记：rebuild 模式与同输入的 frozen 运行不得共享身份
        （图派生规则不同＝不同实验）；builder 源码摘要进身份（投影规则实现变化
        ⇒ 身份变化 ⇒ 旧检查点不可搬运）。旧模式返回空（身份逐字节不变）。"""
        if self.graph_builder is None:
            return {}
        from darwinagent.kg.builders import builder_identity

        return builder_identity(self.graph_builder)

    async def run(self, case, spec, config: RunConfig, *, execution=None, retry_answers=None):
        # Only an explicit settled-failure retry bypasses generation cache. Healthy
        # checkpoints, facts and graphs retain their normal identity and reuse path.
        if execution is None:
            return await self._run(case, spec, config, execution=None, retry_answers=retry_answers)
        from darwinagent.runtime.leases import execution_lease

        lease_root = self.preparation_root or self.work_dir
        # Shared preparation has one writer; a second process does not queue an external call.
        with execution_lease(lease_root / ".leases" / (digest(case.id) + ".lock")):
            return await self._run(
                case, spec, config, execution=execution, retry_answers=retry_answers
            )

    async def _run(self, case, spec, config: RunConfig, *, execution=None, retry_answers=None):
        retry_answers = retry_answers or {}
        if not isinstance(config, RunConfig) or spec.bundle is None:
            raise ValueError("Pipeline requires frozen RunConfig and a published bundle")
        validate_case(case, spec)
        runtime = KernelRuntime(spec.bundle, config, tuple(q.text for q in case.questions))
        framework = snapshot_files([Path(__file__).resolve().parents[1]])
        transport = transport_identity(self.client)
        from darwinagent.experiments.snapshots import snapshot_manifest

        snapshot_identity = (
            snapshot_manifest(self.frozen_snapshot) if self.frozen_snapshot is not None else None
        )
        identity = digest(
            {
                "case": case.to_dict(),
                "task": spec.declaration(),
                "config": config.to_dict(),
                "assets": runtime.bundle.version,
                "framework": framework,
                "transport": transport,
                "snapshot": None
                if snapshot_identity is None
                else snapshot_identity["snapshot_digest"],
                **self._graph_mode_identity(),
            }
        )
        from darwinagent.runtime.execution import ExecutionSelection
        from darwinagent.runtime.steps import StepJournal

        if execution is not None and not isinstance(execution, ExecutionSelection):
            raise TypeError("execution must be ExecutionSelection")
        daily = execution is not None and not execution.strict
        workspace = self.workspace
        if daily and workspace is None:
            from darwinagent.runtime.workspace import Workspace

            workspace = Workspace(self.work_dir / "workspace")
        root = self.work_dir / case.id
        if daily:
            # Facts and graphs have independent content identities; transport is provenance.
            preparation = digest(
                {
                    "corpus": [b.to_dict() for b in case.corpus],
                    "schema": next(
                        a.content for a in runtime.bundle.assets.assets if a.kind == "S"
                    ),
                    "snapshot": snapshot_identity,
                    **(
                        {
                            "extract_prompt": runtime.prompt("extract"),
                            "graph_config": config.to_dict(),
                            "graph_transport": transport,
                        }
                        if getattr(self.graph_builder, "uses_extract_prompt", False)
                        else {}
                    ),
                    **self._graph_mode_identity(),
                }
            )
            preparation_case_root = (
                (self.preparation_root / case.id)
                if self.preparation_root is not None
                else self.work_dir / case.id
            )
            root = preparation_case_root / "preparations" / preparation
            if execution.mode == "retry_failed" and (
                ("facts" in execution.stages and (root / "memory.failure.json").exists())
                or ("graph" in execution.stages and (root / "graph.failure.json").exists())
            ):
                from uuid import uuid4

                failed_preparation = root
                root = preparation_case_root / "preparation-attempts" / uuid4().hex
                from darwinagent.runtime.continuation import reuse_preparation

                reuse_preparation(
                    failed_preparation,
                    root,
                    include_graph=not (failed_preparation / "graph.failure.json").exists(),
                )
            if execution.mode in ("rerun", "rerun_all") and not (
                set(execution.stages) & {"answer", "retrieval", "check", "review"}
            ):
                from uuid import uuid4

                root = preparation_case_root / "preparation-attempts" / uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
        if daily:
            from darwinagent.runtime.continuation import register_legacy_case, reuse_preparation

            case_root = self.work_dir / case.id
            try:
                selected_branch = workspace.branch(execution.branch)
            except KeyError:
                if execution.mode == "fork" and execution.branch != "main":
                    try:
                        selected_branch = workspace.create_branch(execution.branch, parent="main")
                    except KeyError:
                        raise ValueError(
                            "Fork source branch main is unavailable; register the source branch first"
                        ) from None
                else:
                    selected_branch = workspace.create_branch(
                        execution.branch, working=runtime.bundle.version
                    )
            if selected_branch.get("parent") and execution.includes(case.id):
                from darwinagent.runtime.continuation import fork_case_progress

                inherited = fork_case_progress(
                    case_root,
                    selected_branch["parent"],
                    execution.branch,
                    tuple(q for q in case.questions if execution.includes(case.id, q.id)),
                    workspace=workspace,
                )
                workspace.append_event(
                    "fork_progress",
                    {
                        "case_id": case.id,
                        "branch": execution.branch,
                        "parent": selected_branch["parent"],
                        "registered": inherited["registered"],
                        "not_registered": inherited["not_registered"],
                    },
                )
            register_legacy_case(case_root, case, branch=execution.branch, workspace=workspace)
            for previous in sorted((preparation_case_root / "preparations").glob("*")):
                case_path = previous / "case.json"
                if previous != root and case_path.exists():
                    prior = json.loads(case_path.read_text())
                    if (
                        prior.get("corpus") == case.to_dict()["corpus"]
                        and not (root / "memory.complete.json").exists()
                        and not (
                            execution.mode in ("rerun", "rerun_all")
                            and "facts" in execution.stages
                            and "answer" not in execution.stages
                        )
                    ):
                        reuse_preparation(previous, root)
            if not (root / "case.json").exists():
                atomic_json(root / "case.json", case.to_dict())
        elif not (root / "case.json").exists():
            atomic_json(root / "case.json", case.to_dict())
        carried_ok = (lambda stored: True) if daily else carried_acceptor(root, identity, framework)
        identity_path = root / "identity.json"
        if identity_path.exists():
            recorded = json.loads(identity_path.read_text())
            if recorded["identity"] != identity and not carried_ok(recorded["identity"]):
                raise ValueError(
                    "Checkpoint belongs to a different run identity; use a new run directory"
                )
        if daily and identity_path.exists():
            prior_identity = json.loads(identity_path.read_text())
            atomic_json(
                root / "provenance" / (prior_identity["identity"] + ".json"), prior_identity
            )
            atomic_json(
                root / "provenance" / (identity + ".json"),
                {
                    "identity": identity,
                    "assets": runtime.bundle.version,
                    "config": config.to_dict(),
                    "framework": framework,
                    "transport": transport,
                },
            )
        atomic_json(
            identity_path,
            {
                "identity": identity,
                "case_id": case.id,
                "assets": runtime.bundle.version,
                "config": config.to_dict(),
                "framework": framework,
                "transport": transport,
            },
        )

        def verify():
            runtime.bundle.verify()
            assert_files(framework)
            if transport_identity(self.client) != transport:
                raise ValueError("Frozen model route/config changed")

        verify()
        if daily and not execution.includes(case.id):
            return RunResult(case.id, identity, runtime.bundle.version, (), 0, ())
        if daily and not (
            set(execution.stages) & {"facts", "graph", "retrieval", "answer", "check", "review"}
        ):
            raise ValueError(
                "Selected stage requires saved inputs; use runner preview and stage execution"
            )
        corpus = {b.source.id: b for b in case.corpus}
        if daily and execution.mode in ("continue", "fork") and "answer" in execution.stages:
            requested = [q for q in case.questions if execution.includes(case.id, q.id)]
            saved_paths = [
                self.work_dir
                / case.id
                / "branches"
                / execution.branch
                / "answers"
                / (digest(q.to_dict()) + ".json")
                for q in requested
            ]
            if requested and all(path.exists() for path in saved_paths):
                saved = [json.loads(path.read_text()) for path in saved_paths]
                for row in saved:
                    if digest(row["result"]) != row["digest"]:
                        raise ValueError("Saved answer checksum mismatch")
                    if row.get("graph_path") and row["result"]["status"] != "execution_error":
                        if (
                            digest(json.loads(Path(row["graph_path"]).read_text()))
                            != row["graph_fingerprint"]
                        ):
                            raise ValueError("Original answer graph changed")
                reused = tuple(AnswerResult.from_dict(row["result"]) for row in saved)
                provenance = [
                    answer_source_record(row, answer.question_id, workspace)
                    for row, answer in zip(saved, reused, strict=True)
                ]
                result = RunResult(
                    case.id,
                    identity,
                    runtime.bundle.version,
                    reused,
                    0,
                    (
                        {
                            "status": "reused",
                            "answer_sources": provenance,
                            "graph_statistics": "not_recomputed",
                        },
                    ),
                    answer_provenance=tuple(provenance),
                )
                atomic_json(root / "result.json", result.to_dict())
                atomic_json(self.work_dir / case.id / "result.json", result.to_dict())
                ref = workspace.put_json(result.to_dict())
                workspace.add_provenance(
                    ref, {"execution_identity": identity, "answer_sources": provenance}
                )
                return result
        # The hook before graph building: tasks declaring meta.anchoring insert the atomic-memory
        # stage here; every other task goes straight to typed entity extraction.
        anchored = bool(runtime.schema.meta.get("anchoring"))

        # Stage one (anchored tasks only): atomic-fact memory, checkpointed independently.
        # A frozen snapshot replaces BOTH extraction stages: memory and graph are frozen input.
        memory = None
        memory_failure = None
        raw = ()
        if anchored and self.frozen_snapshot is None:
            if (root / "memory.failure.json").exists():
                failure = json.loads((root / "memory.failure.json").read_text())
                memory_failure = failure["error"]
                raw = tuple(failure.get("raw_outputs", []))
            elif (root / "memory.complete.json").exists():
                completion = json.loads((root / "memory.complete.json").read_text())
                payload = json.loads((root / "memory.json").read_text())
                if digest(payload) != completion["digest"]:
                    raise ValueError("Saved memory changed")
                memory = MemoryResult.from_dict(payload, corpus)
                if memory.fingerprint != completion["fingerprint"]:
                    raise ValueError("Saved memory fingerprint mismatch")
            else:
                if daily and "facts" not in execution.stages:
                    raise ValueError("Missing facts input; facts stage is not selected")
                try:
                    memory = await ExtractionAgent(
                        runtime,
                        self.client,
                        config,
                        identity[:16],
                        journal_root=root / "extraction-steps" if daily else None,
                        workspace=workspace,
                    ).extract(case.corpus)
                    verify()
                    payload = memory.to_dict()
                    atomic_json(root / "memory.json", payload)
                    atomic_json(
                        root / "memory.complete.json",
                        {
                            "digest": digest(payload),
                            "fingerprint": memory.fingerprint,
                            "facts": len(memory.facts),
                            "raw_outputs": len(memory.raw_outputs),
                            "diagnostics": plain(memory.diagnostics),
                        },
                    )
                except Exception as exc:
                    from darwinagent.runtime.steps import (
                        AwaitingBudget,
                        RequestAbandoned,
                        UnknownRequest,
                    )

                    if getattr(exc, "continuation_signal", False) or isinstance(
                        exc, (UnknownRequest, AwaitingBudget, RequestAbandoned)
                    ):
                        raise
                    verify()
                    memory_failure = f"{type(exc).__name__}: {exc}"
                    raw = tuple(getattr(exc, "raw_outputs", ()))
                    atomic_json(
                        root / "memory.failure.json",
                        {"error": memory_failure, "raw_outputs": list(raw)},
                    )

        if daily and not (
            set(execution.stages) & {"graph", "retrieval", "answer", "check", "review"}
        ):
            result = RunResult(
                case.id,
                identity,
                runtime.bundle.version,
                (),
                0,
                (
                    {
                        "status": "failed"
                        if memory_failure
                        else ("complete" if anchored else "missing_inputs"),
                        "stage": "facts",
                        "error": memory_failure,
                        "note": None
                        if anchored
                        else "Task does not declare an independent atomic-facts stage",
                    },
                ),
                0 if memory is None else len(memory.facts),
                "" if memory is None else memory.fingerprint,
            )
            atomic_json(root / "result.json", result.to_dict())
            result_ref = workspace.put_json(result.to_dict())
            if (root / "memory.json").exists():
                workspace.add_reference(
                    result_ref,
                    "facts",
                    workspace.put_bytes((root / "memory.json").read_bytes(), format="legacy-json"),
                )
            return result

        # Stage two: the graph — assembled from memory when anchored, extracted directly otherwise.
        graph = None
        graph_failure = None
        graph_fingerprint = ""
        if memory_failure:
            graph_failure = f"extraction failed: {memory_failure}"
        elif (root / "graph.failure.json").exists():
            failure = json.loads((root / "graph.failure.json").read_text())
            graph_failure = failure["error"]
            raw = tuple(failure.get("raw_outputs", [])) or raw
        elif (root / "graph.complete.json").exists():
            completion = json.loads((root / "graph.complete.json").read_text())
            graph_path = root / "graph.json"
            graph_payload = json.loads(graph_path.read_text())
            if digest(graph_payload) != completion["digest"]:
                raise ValueError("Saved graph changed")
            graph = GraphResult(
                nx.freeze(load_graph(graph_path)),
                MappingProxyType(corpus),
                (),
                tuple(completion["diagnostics"]),
            )
            graph_fingerprint = completion["digest"]
            if self.frozen_snapshot is None:
                runtime.validate_graph(graph, memory.fingerprint if memory is not None else None)
            else:
                from darwinagent.experiments.snapshots import attach_vector

                attach_vector(graph, self.frozen_snapshot, embedder_factory=self.embedder_factory)
        else:
            if daily and "graph" not in execution.stages:
                raise ValueError("Missing graph input; graph stage is not selected")
            try:
                if self.graph_builder is not None and self.frozen_snapshot is not None:
                    # 图按当前 S 构建（builder 负责生成＋出处落
                    # 真实证据块＋挂冻结向量＋命中映射校验，返回已包 GraphResult）；
                    # 记忆/向量仍取快照。sources 在此接 case 语料（builder 无 case 上下文
                    # 时兜底）。质量门与冻结路径同构：F 试跑＋反例探针＋任务图 C。
                    from darwinagent.kg.builders import build_snapshot_graph

                    graph = await build_snapshot_graph(
                        self.graph_builder,
                        self.frozen_snapshot,
                        runtime,
                        corpus,
                        self.client,
                        config,
                        embedder_factory=self.embedder_factory,
                        workspace=workspace,
                    )
                    if not dict(getattr(graph, "sources", {}) or {}):
                        object.__setattr__(graph, "sources", MappingProxyType(corpus))
                elif self.frozen_snapshot is not None:
                    # 冻结快照图：指纹校验的输入数据。质量门＝F 试跑（真图实参）＋反例探针＋
                    # 任务图 C；类型重查/claims/锚定不变量不适用（词汇由 bootstrap 依结构样本生成）。
                    from darwinagent.experiments.snapshots import attach_vector, load_frozen_graph

                    graph = load_frozen_graph(self.frozen_snapshot, case.corpus)
                    attach_vector(
                        graph, self.frozen_snapshot, embedder_factory=self.embedder_factory
                    )
                elif anchored:
                    graph = GraphAssembler.build(memory, spec, runtime.schema)
                    # Unified validation on first assembly too, not only on resume:
                    # typed schema, instance axioms, task graph C and the anchoring invariants.
                    runtime.validate_graph(graph, memory.fingerprint)
                else:
                    graph = await ExtractionAgent(
                        runtime,
                        self.client,
                        config,
                        identity[:16],
                        journal_root=root / "extraction-steps" if daily else None,
                        workspace=workspace,
                    ).extract_entities(case.corpus)
                verify()
                # Trial every admitted F against the actual graph before inference can call it.
                trials = runtime.functions.trial(
                    graph,
                    {
                        a.id: list(a.trial_inputs)
                        for a in runtime.bundle.assets.assets
                        if a.kind == "F"
                    },
                )
                atomic_json(root / "function-trials.json", trials)
                from darwinagent.kernel.counterexamples import run_probes

                if runtime.bundle.assets.origin.get("kind") == "cold_bootstrap":
                    # 冷启动 bundle 由无标签结构样本生成，无训练名接触面：字面量准入＋真图试跑已覆盖；
                    # 改名探针对生成式检索 F 的截断/惯用法敏感，误伤多于收益——提案轮（proposal
                    # origin）恢复全量探针。动态图任务（无冻结快照，2026-10-05 loop5 B0 冒烟
                    # 被 cold_bootstrap 探针全灭拦下）与冻结快照路径同规则。
                    atomic_json(
                        root / "counterexamples.json",
                        {
                            "probe": "renamed_keys_shifted_dates",
                            "status": "skipped",
                            "reason": "cold_bootstrap: literal admission + real-graph trials cover name independence; probes resume on proposals",
                        },
                    )
                else:
                    atomic_json(root / "counterexamples.json", run_probes(runtime, graph, memory))
                if self.frozen_snapshot is not None:
                    opinions = runtime.checks.run("graph", runtime.graph_snapshot(graph))
                    failures = [x for x in opinions if not x["ok"]]
                    if failures:
                        raise ValueError(f"Task graph checks rejected graph: {failures}")
                else:
                    runtime.validate_graph(
                        graph, memory.fingerprint if memory is not None else None
                    )
                save_graph(graph.graph, root / "graph.json")
                if self.graph_builder is not None and self.frozen_snapshot is not None:
                    from darwinagent.experiments.graph_evidence import graph_evidence

                    evidence = {
                        "stage": "graph_evidence",
                        "case_id": case.id,
                        "graph_mode": self._graph_mode_identity()["graph_mode"],
                        **graph_evidence(graph, runtime.schema),
                        "artifacts": {
                            "graph": str((root / "graph.json").resolve()),
                            "facts": str((self.frozen_snapshot / "facts.jsonl").resolve()),
                        },
                    }
                    atomic_json(root / "graph.evidence.json", evidence)
                    object.__setattr__(graph, "diagnostics", (*graph.diagnostics, evidence))
                graph_fingerprint = digest(json.loads((root / "graph.json").read_text()))
                atomic_json(
                    root / "graph.complete.json",
                    {
                        "digest": graph_fingerprint,
                        "memory_fingerprint": ("" if memory is None else memory.fingerprint)
                        if self.frozen_snapshot is None
                        else snapshot_identity["facts_digest"],
                        "raw_outputs": [],
                        "diagnostics": list(graph.diagnostics),
                    },
                )
            except Exception as exc:
                from darwinagent.runtime.steps import (
                    AwaitingBudget,
                    RequestAbandoned,
                    UnknownRequest,
                )

                if getattr(exc, "continuation_signal", False) or isinstance(
                    exc, (UnknownRequest, AwaitingBudget, RequestAbandoned)
                ):
                    raise
                verify()
                graph_failure = f"{type(exc).__name__}: {exc}"
                raw = tuple(getattr(exc, "raw_outputs", ())) or (() if graph is None else ())
                atomic_json(
                    root / "graph.failure.json", {"error": graph_failure, "raw_outputs": list(raw)}
                )

        agent = AnswerAgent(runtime, self.client, config, spec, identity[:16])
        sem = asyncio.Semaphore(config.concurrency)
        answer_source_records = {}

        async def one(question):
            async with sem:
                verify()
                checkpoint = root / "answers" / f"{digest(question.id)}.json"
                attempt_root = (
                    self.work_dir
                    / case.id
                    / "steps"
                    / execution.branch
                    / runtime.bundle.assets.content_id
                    / digest(question.to_dict())
                    if daily
                    else root / "steps" / digest(question.to_dict())
                )
                prior_selected = checkpoint.exists()
                if daily:
                    checkpoint = (
                        self.work_dir
                        / case.id
                        / "branches"
                        / execution.branch
                        / "answers"
                        / f"{digest(question.to_dict())}.json"
                    )
                    prior_selected = checkpoint.exists()
                    if checkpoint.exists() and execution.mode in (
                        "rerun",
                        "rerun_all",
                        "retry_failed",
                    ):
                        old = json.loads(checkpoint.read_text())
                        if (
                            execution.mode != "retry_failed"
                            or old["result"]["status"] == "execution_error"
                        ):
                            archive = root / "history" / checkpoint.name / digest(old)
                            archive.parent.mkdir(parents=True, exist_ok=True)
                            if not archive.exists():
                                atomic_json(archive, old)
                            # New attempts never remove old records or reuse old failure responses.
                            from uuid import uuid4

                            attempt_root = (
                                root / "attempts" / digest(question.to_dict()) / uuid4().hex
                            )
                            checkpoint = attempt_root / "answer.json"
                if daily and execution.mode == "retry_failed" and not prior_selected:
                    return None
                reused = checkpoint.exists()
                stage_only = daily and "answer" not in execution.stages
                if daily and "retrieval" not in execution.stages:
                    from uuid import uuid4

                    latest_answer_path = (
                        self.work_dir
                        / case.id
                        / "branches"
                        / execution.branch
                        / "answers"
                        / (digest(question.to_dict()) + ".json")
                    )
                    latest_answer = (
                        json.loads(latest_answer_path.read_text())
                        if latest_answer_path.exists()
                        else {}
                    )
                    source_journal = Path(latest_answer.get("journal_path") or attempt_root)
                    source_state = StepJournal(source_journal, workspace=workspace).state()
                    attempt_root = (
                        root / "stage-attempts" / digest(question.to_dict()) / uuid4().hex
                    )
                    seeded = StepJournal(
                        attempt_root, workspace=workspace, bypass_cache=execution.mode != "continue"
                    )
                    seeded.save_state(
                        {
                            k: v
                            for k, v in source_state.items()
                            if k not in ("position", "content_ref")
                        }
                    )
                if stage_only:
                    reused = False
                    checkpoint = (
                        root
                        / "stage-results"
                        / digest(question.to_dict())
                        / (digest(execution.to_dict()) + ".json")
                    )
                if checkpoint.exists() and not stage_only:
                    stored = json.loads(checkpoint.read_text())
                    if (
                        stored.get("identity") != identity
                        and not carried_ok(stored.get("identity"))
                    ) or digest(stored["result"]) != stored["digest"]:
                        raise ValueError("Answer checkpoint changed or belongs to another run")
                    answer = AnswerResult.from_dict(stored["result"])
                    original_graph = graph
                    if daily and stored.get("graph_path"):
                        original_path = Path(stored["graph_path"])
                        payload = json.loads(original_path.read_text())
                        if digest(payload) != stored["graph_fingerprint"]:
                            raise ValueError("Original answer graph changed")
                        original_sources = corpus
                        if stored.get("case_path"):
                            from darwinagent.contracts import CorpusBlock, SourceRef

                            original_case = json.loads(Path(stored["case_path"]).read_text())
                            original_sources = {}
                            for block in original_case["corpus"]:
                                source = SourceRef(**block["source"])
                                original_sources[source.id] = CorpusBlock(
                                    source, block["text"], block.get("metadata", {})
                                )
                        original_graph = GraphResult(
                            nx.freeze(load_graph(original_path)),
                            MappingProxyType(original_sources),
                            (),
                            (),
                        )
                    if original_graph is not None:
                        validate_published(answer, question, original_graph)
                elif graph_failure:
                    answer = AnswerResult(
                        question.id, "execution_error", "", error=graph_failure, raw_outputs=raw
                    )
                else:
                    journal = (
                        StepJournal(
                            attempt_root,
                            bypass_cache=execution.mode in ("retry_failed", "rerun", "rerun_all"),
                            workspace=workspace,
                            provenance={
                                "identity": identity,
                                "question_version": digest(question.to_dict()),
                                "graph_fingerprint": graph_fingerprint,
                                "asset_version": runtime.bundle.version,
                                "framework": framework,
                                "transport": transport,
                            },
                        )
                        if daily
                        else None
                    )
                    from darwinagent.runtime.steps import RequestAbandoned

                    try:
                        answer = await agent.answer(
                            question,
                            graph,
                            journal=journal,
                            stages=None if execution is None else execution.stages,
                            **(
                                {"recovery_feedback": retry_answers[question.id]}
                                if question.id in retry_answers
                                else {}
                            ),
                        )
                    except RequestAbandoned:
                        workspace.append_event(
                            "question_abandoned",
                            {
                                "case_id": case.id,
                                "question_id": question.id,
                                "branch": execution.branch,
                                "journal_path": str(attempt_root),
                            },
                        )
                        return None
                    if answer is None:
                        return None
                    validate_published(answer, question, graph)
                verify()
                value = answer.to_dict()
                if not reused:
                    atomic_json(
                        checkpoint,
                        {
                            "identity": identity,
                            "digest": digest(value),
                            "question_version": digest(question.to_dict()),
                            "graph_fingerprint": graph_fingerprint,
                            "graph_path": str(root / "graph.json"),
                            "case_path": str(root / "case.json"),
                            "case_digest": digest(case.to_dict()),
                            "case_ref": workspace.put_json(case.to_dict())
                            if workspace is not None
                            else None,
                            "transport": transport,
                            "journal_path": str(attempt_root),
                            "asset_version": runtime.bundle.version,
                            "result": value,
                        },
                    )
                    if (
                        daily
                        and "answer" in execution.stages
                        and checkpoint.parent
                        != self.work_dir / case.id / "branches" / execution.branch / "answers"
                    ):
                        atomic_json(
                            self.work_dir
                            / case.id
                            / "branches"
                            / execution.branch
                            / "answers"
                            / f"{digest(question.to_dict())}.json",
                            json.loads(checkpoint.read_text()),
                        )
                answer_source_records[question.id] = answer_source_record(
                    json.loads(checkpoint.read_text()), question.id, workspace
                )
                return answer

        selected = (
            case.questions
            if execution is None
            else tuple(q for q in case.questions if execution.includes(case.id, q.id))
        )
        if daily and not (set(execution.stages) & {"retrieval", "answer", "check", "review"}):
            selected = ()
        outcomes = await asyncio.gather(*(one(q) for q in selected), return_exceptions=True)
        # Let already submitted siblings save their receipts before exposing a pause/unknown.
        # Their next request observes the same control boundary; no paid response is cancelled.
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        answers = tuple(answer for answer in outcomes if answer is not None)
        verify()
        result = RunResult(
            case.id,
            identity,
            runtime.bundle.version,
            answers,
            0 if graph is None else graph.graph.number_of_nodes(),
            (
                {
                    "stage": "graph",
                    "case_id": case.id,
                    **self._graph_mode_identity(),
                    "status": "execution_error",
                    "error": graph_failure,
                    "artifacts": {"graph_failure": str((root / "graph.failure.json").resolve())},
                },
            )
            if graph_failure
            else graph.diagnostics,
            (0 if memory is None else len(memory.facts))
            if self.frozen_snapshot is None
            else snapshot_identity["n_facts"],
            ("" if memory is None else memory.fingerprint)
            if self.frozen_snapshot is None
            else snapshot_identity["facts_digest"],
            graph_fingerprint,
            tuple(answer_source_records[a.question_id] for a in answers),
        )
        atomic_json(root / "result.json", result.to_dict())
        if daily:
            view = self.work_dir / case.id
            for name in (
                "graph.json",
                "graph.complete.json",
                "memory.json",
                "memory.complete.json",
            ):
                if (root / name).exists():
                    if (view / name).exists():
                        workspace.put_bytes((view / name).read_bytes(), format="legacy-json")
                    atomic_json(view / name, json.loads((root / name).read_text()))
            atomic_json(view / "result.json", result.to_dict())
        if workspace is not None:
            result_ref = workspace.put_json(result.to_dict())
            workspace.add_provenance(
                result_ref,
                {
                    "identity": identity,
                    "transport": transport,
                    "asset_version": runtime.bundle.version,
                    "selection": execution.to_dict(),
                },
            )
            for name in ("graph.json", "memory.json"):
                if (root / name).exists():
                    ref = workspace.put_bytes((root / name).read_bytes(), format="legacy-json")
                    workspace.add_reference(result_ref, name, ref)
            for block in case.corpus:
                ref = workspace.put_json(block.to_dict())
                workspace.add_reference(result_ref, "source", ref)
        return result
