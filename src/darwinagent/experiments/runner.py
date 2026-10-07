"""Public experiment facade: parameter validation, composition and extension hooks."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from darwinagent.kernel.revision import AssetRevisionService
from darwinagent.kernel.revision import training_id as training_id
from darwinagent.llm.client import LLMClient
from darwinagent.runtime.identity import assert_files, snapshot_files

from . import graph_trials, lifecycle, optimization, stages
from . import rounds as round_coordination
from .constants import ADMISSION_ATTEMPTS as ADMISSION_ATTEMPTS
from .constants import FEEDBACK_BUDGET_CHARS as FEEDBACK_BUDGET_CHARS
from .feedback import training_feedback as _training_feedback
from .optimization import WikiAdmissionExhausted as WikiAdmissionExhausted
from .recovery import batched_fault_retry as batched_fault_retry
from .recovery import promote_verified_check_replay as promote_verified_check_replay
from .trials import stress_trial_samples as stress_trial_samples
from .wiki_evidence import bounded_trace as bounded_trace


def training_feedback(*args, **kwargs):
    return _training_feedback(*args, **kwargs, budget=FEEDBACK_BUDGET_CHARS)


class ExperimentRunner:
    def __init__(
        self,
        adapter,
        evaluator_factory,
        connection_config,
        run_config,
        policy,
        work_dir,
        frozen_files=(),
        client_factory=None,
        bootstrap_context=None,
        snapshot_root=None,
        bootstrap_trial_graph=None,
        smoke_judge=None,
        optimization_mode="legacy",
        wiki_call_limit=30,
        dynamic_trial=False,
        graph_builder=None,
        proposal_attempts=None,
        round_deadline_s=None,
        validation_plan=None,
        seed_assets=None,
    ):
        if optimization_mode not in ("legacy", "wiki"):
            raise ValueError("Unknown optimization mode")
        if type(wiki_call_limit) is not int or wiki_call_limit < 1:
            raise ValueError("Wiki call limit must be positive")
        if dynamic_trial and (snapshot_root is not None or bootstrap_trial_graph is not None):
            raise ValueError("dynamic_trial is the no-snapshot dynamic-graph admission mode")
        if graph_builder is not None and snapshot_root is None:
            raise ValueError("graph_builder requires snapshot_root (frozen memory/vector)")
        if proposal_attempts is not None and (
            type(proposal_attempts) is not int or proposal_attempts < 1
        ):
            raise ValueError("proposal_attempts must be a positive int")
        if round_deadline_s is not None and (
            type(round_deadline_s) not in (int, float) or round_deadline_s <= 0
        ):
            raise ValueError("round_deadline_s must be positive seconds")
        self.adapter, self.evaluator_factory = adapter, evaluator_factory
        self.connection_config, self.config, self.policy = connection_config, run_config, policy
        self.root = Path(work_dir)
        self.frozen = snapshot_files([Path(__file__).resolve().parents[1], *frozen_files])
        self.revisions = AssetRevisionService()
        self._injected_client = client_factory
        # bootstrap_context: 冻结快照结构样本（无标签），随冷启动 bootstrap 载荷进提示词。
        self.bootstrap_context = bootstrap_context
        # snapshot_root: 每对话冻结记忆快照目录（<case_id>/ 子目录）；注入时臂间共享同一记忆面。
        self.snapshot_root = Path(snapshot_root) if snapshot_root is not None else None
        # bootstrap_trial_graph: 冷启动 bootstrap 反馈环内的真图试跑（冻结快照图）。
        self.bootstrap_trial_graph = bootstrap_trial_graph
        # dynamic_trial: 动态图任务（无冻结快照、无共享试验图）的真图准入模式——
        # 候选必须在同题真图上过完整准入电池（2026-10-05 Travel 空值契约/冻结容器
        # 事故：坏候选漏过准入在正式计分才炸）。任务装配层按任务形态选择注入。
        self.dynamic_trial = dynamic_trial
        # smoke_judge: 任务层注入的子集判题器（冻结判题原语；框架不依赖任务模块）。
        self.smoke_judge = smoke_judge
        self._fault_retried = set()
        self.optimization_mode = optimization_mode
        self.wiki_call_limit = wiki_call_limit
        # 新模式（recheck4「冻结记忆/向量、图可重建」）：graph_builder 注入时，
        # 记忆/向量仍取快照，正式/冒烟/准入的图全部按当前 S 从固定事实重建；
        # proposal_attempts/round_deadline_s 是新模式轮预算（旧模式默认 50/无时限，
        # 行为不变）；validation_plan 把同对话验证题接入选版（聚合指标进决策与
        # Wiki，逐题 gold/答案/诊断不进提案器）。
        self.graph_builder = graph_builder
        # seed-assets（缺口③）：给定锁定 bundle 目录时跳过冷启动、直接以其为 B0
        # （准入/冒烟/评分不豁免）；进声明 identity。
        self.seed_assets = Path(seed_assets) if seed_assets is not None else None
        self.proposal_attempts = proposal_attempts or ADMISSION_ATTEMPTS
        self.round_deadline_s = round_deadline_s
        self.validation_plan = validation_plan
        self._round_deadline = None
        self._rebuild_cache = {}
        if graph_builder is not None and dynamic_trial:
            raise ValueError("graph_builder and dynamic_trial are exclusive graph modes")
        self._base_config = run_config
        self._base_connection_config = copy.deepcopy(connection_config)
        self._base_policy = policy
        self._original_evaluator_factory = evaluator_factory
        self._controls_started = None
        self._refresh_controls("main", startup=True)

    def _refresh_controls(self, branch="main", *, startup=False):
        from dataclasses import fields, replace

        from .control import apply_wiki_controls, load_controls

        state = load_controls(self.root, branch=branch)
        if startup or self._controls_started is None:
            from darwinagent.runtime.workspace import Workspace

            database = self.root / "workspace/workspace.sqlite3"
            events = Workspace(database.parent).events() if database.exists() else []
            self._controls_started = events[-1]["seq"] if events else 0
        self.config = replace(self._base_config, **state["run_config"])
        self.connection_config = copy.deepcopy(self._base_connection_config)
        self.policy = self._base_policy
        for key, value in state["connection_config"].items():
            if not hasattr(self.connection_config, key):
                raise ValueError("Unknown persisted connection setting " + key)
            setattr(self.connection_config, key, value)
        if state["policy"]:
            changes = dict(state["policy"])
            tuple_fields = {
                field.name
                for field in fields(self.policy)
                if isinstance(getattr(self.policy, field.name), tuple)
            }
            changes = {
                key: tuple(value) if key in tuple_fields else value
                for key, value in changes.items()
            }
            self.policy = replace(self.policy, **changes)
        self.evaluation_controls = state["evaluation"]
        if state["evaluation"]:
            original = self._original_evaluator_factory
            settings = dict(state["evaluation"])

            def configured_evaluator(client, path):
                evaluator = original(client, path)
                configure = getattr(evaluator, "configure_evaluation", None)
                if configure is not None:
                    result = configure(settings)
                    return evaluator if result is None else result
                for key, value in settings.items():
                    if not hasattr(evaluator, key):
                        raise ValueError("Evaluator does not support persisted setting " + key)
                    setattr(evaluator, key, value)
                return evaluator

            from darwinagent.runtime.artifacts import digest

            base_criterion = getattr(original, "criterion_id", None)
            base_criterion = base_criterion() if callable(base_criterion) else base_criterion
            if base_criterion is not None:
                configured_evaluator.criterion_id = digest(
                    {"base": base_criterion, "settings": settings}
                )
            self.evaluator_factory = configured_evaluator
        else:
            self.evaluator_factory = self._original_evaluator_factory
        apply_wiki_controls(self.root, state)
        state["requires_restart"] = state["code_sequence"] > self._controls_started
        state["status"] = (
            "stopped"
            if state["stopped"]
            else "paused"
            if state["paused"] or state["requires_restart"]
            else "running"
        )
        self.control_state = state
        return state

    def _control_boundary(self, next_step="round"):
        from darwinagent.runtime.artifacts import atomic_json

        from .control import ControlSignal

        branch = getattr(getattr(self, "execution", None), "branch", "main")
        state = self._refresh_controls(branch)
        if state["status"] != "running":
            atomic_json(
                self.root / "control-boundaries" / (branch + ".json"),
                {
                    "branch": branch,
                    "sequence": state["sequence"],
                    "status": state["status"],
                    "next_step": next_step,
                    "requires_restart": state["requires_restart"],
                },
            )
            raise ControlSignal(state)
        return state

    async def _await_controlled(self, operation):
        from .control import ControlSignal

        try:
            return await operation
        except ControlSignal as signal:
            from darwinagent.runtime.artifacts import atomic_json

            decisions = [
                json.loads(path.read_text()) for path in sorted(self.root.glob("R*/decision.json"))
            ]
            summary = {
                "status": signal.state["status"],
                "rounds": decisions,
                "stage_results": [],
                "stopped_by_operator": True,
                "requires_restart": signal.state["requires_restart"],
            }
            atomic_json(self.root / "summary.json", summary)
            return summary

    def preview(self, case_ids, execution=None):
        from .control import preview

        if isinstance(case_ids, str):
            case_ids = (case_ids,)
        return preview(self.root, [self.adapter.generation_input(c) for c in case_ids], execution)

    def intervene(self, change, *, branch="main", expected_revision=None):
        from .control import intervene

        result = intervene(self.root, change, branch=branch, expected_revision=expected_revision)
        if change.get("connection_config"):
            for key, value in change["connection_config"].items():
                if not hasattr(self.connection_config, key):
                    raise ValueError("Unknown connection setting " + key)
                setattr(self.connection_config, key, value)
                if key.lower().endswith("api_key"):
                    setattr(self._base_connection_config, key, value)
        if change.get("kind") in ("configuration", "model") and change.get("run_config"):
            from dataclasses import replace

            self.config = replace(self.config, **change["run_config"])
        self._refresh_controls(branch)
        return result

    def _wiki_report_valid(self, report, candidate, *, smoke=False):
        return optimization._wiki_report_valid(
            report,
            candidate,
            smoke=smoke,
            config=self.config,
            dynamic_trial=self.dynamic_trial,
            snapshot_root=self.snapshot_root,
        )

    async def _wiki_bootstrap_trials(self, wiki):
        return await optimization._wiki_bootstrap_trials(wiki, root=self.root)

    async def _wiki_attempt(self, wiki, stage, name, cases, spec, adopted, results, scope):
        return await optimization._wiki_attempt(
            wiki,
            stage,
            name,
            cases,
            spec,
            adopted,
            results,
            scope,
            client_factory=self._client,
            preflight_hook=self._preflight,
            record_smoke_hook=self._record_smoke,
            get_round_deadline=self._get_round_deadline,
            smoke_gate_hook=self._smoke_gate,
            valid_proposal_raw_hook=self._valid_proposal_raw,
            wiki_report_valid_hook=self._wiki_report_valid,
            config=self.config,
            dynamic_trial=self.dynamic_trial,
            proposal_attempts=self.proposal_attempts,
            revisions=self.revisions,
            round_deadline_s=self.round_deadline_s,
            snapshot_root=self.snapshot_root,
            branch=getattr(getattr(self, "execution", None), "branch", "main"),
        )

    @staticmethod
    def _valid_proposal_raw(raw, base=None):
        return optimization._valid_proposal_raw(raw, base)

    @staticmethod
    def _wiki_decision_facts(decision):
        return optimization._wiki_decision_facts(decision)

    async def _wiki_formal_from_disk(self, wiki, name, cases, scores):
        return await optimization._wiki_formal_from_disk(
            wiki, name, cases, scores, root=self.root, snapshot_root=self.snapshot_root
        )

    async def _legacy_candidate(
        self,
        stage,
        name,
        cases,
        spec,
        adopted,
        results,
        baseline,
        evidence,
        scope,
        decision_path,
        decisions,
        n,
    ):
        return await optimization._legacy_candidate(
            stage,
            name,
            cases,
            spec,
            adopted,
            results,
            baseline,
            evidence,
            scope,
            decision_path,
            decisions,
            n,
            client_factory=self._client,
            preflight_hook=self._preflight,
            record_smoke_hook=self._record_smoke,
            smoke_gate_hook=self._smoke_gate,
            config=self.config,
            dynamic_trial=self.dynamic_trial,
            revisions=self.revisions,
            root=self.root,
            snapshot_root=self.snapshot_root,
            branch=getattr(getattr(self, "execution", None), "branch", "main"),
        )

    def _stage_health(self):
        return stages._stage_health(root=self.root)

    async def _preflight(self, candidate, spec, sample_question=None, cases=None, replay_inputs=()):
        return await stages._preflight(
            candidate,
            spec,
            sample_question,
            cases,
            replay_inputs,
            dynamic_trial_graphs_hook=self._dynamic_trial_graphs,
            rebuild_graph_cached_hook=self._rebuild_graph_cached,
            bootstrap_trial_graph=self.bootstrap_trial_graph,
            config=self.config,
            dynamic_trial=self.dynamic_trial,
            graph_builder=self.graph_builder,
            root=self.root,
            snapshot_root=self.snapshot_root,
        )

    def _rebuild_graph_cached(self, bundle, case):
        return graph_trials._rebuild_graph_cached(
            bundle,
            case,
            rebuild_cache=self._rebuild_cache,
            graph_builder=self.graph_builder,
            snapshot_root=self.snapshot_root,
        )

    async def _rebuild_trial_supply(self, bundle, cases):
        return await graph_trials._rebuild_trial_supply(
            bundle, cases, rebuild_graph_cached_hook=self._rebuild_graph_cached
        )

    async def _dynamic_trial_graphs(self, bundle, cases):
        return await graph_trials._dynamic_trial_graphs(
            bundle,
            cases,
            adopted_stage_graph_hook=self._adopted_stage_graph,
            extract_trial_graph_hook=self._extract_trial_graph,
        )

    def _adopted_stage_graph(self, case, base_version):
        return graph_trials._adopted_stage_graph(case, base_version, root=self.root)

    async def _extract_trial_graph(self, bundle, case):
        return await graph_trials._extract_trial_graph(
            bundle,
            case,
            client_factory=self._client,
            config=self.config,
            connection_config=self.connection_config,
            root=self.root,
        )

    def _client(self, stage):
        if self._injected_client is not None:
            client = self._injected_client(stage)
        else:
            cfg = copy.deepcopy(self.connection_config)
            cfg.work_dir = self.root / stage / "runtime"
            client = LLMClient(cfg)
        connection = copy.deepcopy(self.connection_config)
        run_config = self.config

        def boundary(next_step):
            from darwinagent.runtime.artifacts import atomic_json

            from .control import ControlSignal

            state = self._control_boundary(next_step)
            if connection != self.connection_config or run_config != self.config:
                state = {**state, "status": "paused", "requires_restart": True}
                atomic_json(
                    self.root / "control-boundaries" / (state["branch"] + ".json"),
                    {
                        "branch": state["branch"],
                        "sequence": state["sequence"],
                        "status": "paused",
                        "next_step": next_step,
                        "requires_restart": True,
                        "reason": "Execution connection or configuration changed",
                    },
                )
                raise ControlSignal(state)
            return state

        client._control_boundary = boundary
        return client

    def verify(self):
        assert_files(self.frozen)

    @staticmethod
    def _record_smoke(bundle, error, elapsed_s=0):
        return stages._record_smoke(bundle, error, elapsed_s)

    async def _stage(self, name, cases, spec):
        self._control_boundary("stage:" + name)
        if getattr(self, "execution", None) is not None:
            return await stages.selected_stage(
                name,
                cases,
                spec,
                root=self.root,
                client_factory=self._client,
                config=self.config,
                evaluator_factory=self.evaluator_factory,
                execution=self.execution,
                snapshot_root=self.snapshot_root,
                graph_builder=self.graph_builder,
            )
        return await stages._stage(
            name,
            cases,
            spec,
            client_factory=self._client,
            config=self.config,
            evaluator_factory=self.evaluator_factory,
            graph_builder=self.graph_builder,
            root=self.root,
            snapshot_root=self.snapshot_root,
            verify=self.verify,
        )

    async def _smoke_gate(self, cases, spec, questions_per_case=6, candidate=False):
        return await stages._smoke_gate(
            cases,
            spec,
            questions_per_case,
            candidate,
            client_factory=self._client,
            config=self.config,
            graph_builder=self.graph_builder,
            smoke_judge=self.smoke_judge,
            snapshot_root=self.snapshot_root,
        )

    async def run(
        self,
        case_ids,
        spec,
        rounds=2,
        resume=False,
        stop_file=None,
        b0_gate=None,
        stage_gate=None,
        scope=(),
        execution=None,
    ):
        """Run the training cases on one bundle per round; None rounds waits for STOP.

        scope limits the asset kinds available to each candidate proposal.
        """
        self.execution = execution
        from .control import ControlSignal

        try:
            self._control_boundary("run")
        except ControlSignal as signal:
            return {
                "status": signal.state["status"],
                "rounds": [],
                "stage_results": [],
                "stopped_by_operator": True,
                "requires_restart": signal.state["requires_restart"],
            }
        if execution is not None:
            from darwinagent.runtime.execution import ExecutionSelection

            if not isinstance(execution, ExecutionSelection):
                raise TypeError("execution must be ExecutionSelection")
            full_pipeline = {"facts", "graph", "retrieval", "answer", "check", "review", "score"}
            if set(execution.stages) != full_pipeline:
                from .selected_operations import run_selected_operations

                cases = [
                    self.adapter.generation_input(c)
                    for c in ((case_ids,) if isinstance(case_ids, str) else case_ids)
                ]
                return await self._await_controlled(
                    run_selected_operations(self, cases, spec, execution, scope)
                )
        if isinstance(case_ids, str):
            case_ids = (case_ids,)
        if not case_ids:
            raise ValueError("Training split needs at least one case")
        if rounds is not None and (type(rounds) is not int or rounds < 0):
            raise ValueError("Rounds must be a nonnegative integer or None for unbounded iteration")
        return await self._await_controlled(
            lifecycle.run(
                case_ids,
                spec,
                rounds,
                resume,
                stop_file,
                b0_gate,
                stage_gate,
                scope,
                client_factory=self._client,
                dynamic_trial_graphs_hook=self._dynamic_trial_graphs,
                preflight_hook=self._preflight,
                rebuild_trial_supply_hook=self._rebuild_trial_supply,
                record_smoke_hook=self._record_smoke,
                smoke_gate_hook=self._smoke_gate,
                stage_hook=self._stage,
                wiki_bootstrap_trials_hook=self._wiki_bootstrap_trials,
                wiki_report_valid_hook=self._wiki_report_valid,
                adapter=self.adapter,
                bootstrap_context=self.bootstrap_context,
                bootstrap_trial_graph=self.bootstrap_trial_graph,
                config=self.config,
                connection_config=self.connection_config,
                dynamic_trial=self.dynamic_trial,
                frozen=self.frozen,
                graph_builder=self.graph_builder,
                optimization_mode=self.optimization_mode,
                policy=self.policy,
                proposal_attempts=self.proposal_attempts,
                revisions=self.revisions,
                root=self.root,
                round_deadline_s=self.round_deadline_s,
                seed_assets=self.seed_assets,
                snapshot_root=self.snapshot_root,
                validation_plan=self.validation_plan,
                verify=self.verify,
                wiki_call_limit=self.wiki_call_limit,
                coordinate_rounds=self._coordinate_rounds,
                execution=execution,
            )
        )

    async def _coordinate_rounds(
        self,
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
    ):
        return await round_coordination.run_rounds(
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
            legacy_candidate_hook=self._legacy_candidate,
            preflight_hook=self._preflight,
            record_smoke_hook=self._record_smoke,
            get_round_deadline=self._get_round_deadline,
            smoke_gate_hook=self._smoke_gate,
            stage_hook=self._stage,
            stage_health_hook=self._stage_health,
            wiki_attempt_hook=self._wiki_attempt,
            wiki_decision_facts_hook=self._wiki_decision_facts,
            wiki_formal_from_disk_hook=self._wiki_formal_from_disk,
            wiki_report_valid_hook=self._wiki_report_valid,
            dynamic_trial=self.dynamic_trial,
            graph_builder=self.graph_builder,
            selection_policy=self.policy,
            revisions=self.revisions,
            root=self.root,
            round_deadline_s=self.round_deadline_s,
            snapshot_root=self.snapshot_root,
            validation_plan=self.validation_plan,
            verify=self.verify,
            set_round_deadline=self._set_round_deadline,
            branch=getattr(getattr(self, "execution", None), "branch", "main"),
            control_hook=self._control_boundary,
            policy_hook=lambda: self.policy,
        )

    def _get_round_deadline(self):
        return self._round_deadline

    def _set_round_deadline(self, deadline):
        self._round_deadline = deadline
