"""Three-set campaign controller.

B0 gate -> unbounded training rounds on the train split -> candidate lock (B0 plus every
adopted version) -> one full validation run per candidate -> frozen selection -> one-shot
test on B0 and the selected version. Validation and test diagnostics are recorded but never
feed proposals; every question run lands in an append-only ledger with an optional cap."""
from __future__ import annotations

import json
import time
from pathlib import Path

from oak.contracts import EvaluationResult
from oak.engine import Pipeline
from oak.kernel import KernelBundle
from oak.llm.client import LLMClient
from oak.runtime.artifacts import atomic_json
from oak.runtime.identity import assert_files, snapshot_files, transport_identity
from .runner import ExperimentRunner
from .spec import ExperimentSpec, aggregate_scores, precheck_identity


class CampaignController:
    def __init__(self, adapter, evaluator_factory, connection_config, run_config, spec: ExperimentSpec,
                 work_dir, frozen_files=(), client_factory=None, snapshot_root=None, bootstrap_context=None,
                 smoke_judge=None):
        if not isinstance(spec, ExperimentSpec):
            raise ValueError('Campaign requires a frozen ExperimentSpec')
        self.adapter, self.evaluator_factory = adapter, evaluator_factory
        self.connection_config, self.config, self.spec = connection_config, run_config, spec
        self.root = Path(work_dir)
        self.frozen_files = tuple(frozen_files)
        self.frozen = snapshot_files([Path(__file__).resolve().parents[1], *frozen_files])
        self._injected_client = client_factory
        # 快照结构样本与冻结快照根：由数据集装配层注入（两臂共用同一记忆面）。
        self.bootstrap_context = bootstrap_context
        # 任务层注入的冒烟子集判题器（透传给训练 runner 的冒烟门）。
        self.smoke_judge = smoke_judge
        self.snapshot_root = Path(snapshot_root) if snapshot_root is not None else None
        self.bootstrap_trial_graph = None  # 真图试跑图，由数据集装配层注入

    def verify(self):
        assert_files(self.frozen)

    # ---- question-run ledger: reserve before execution, settle by actual count -------
    def _ledger_path(self):
        return self.root / 'question_runs.jsonl'

    def _ledger_rows(self):
        if not self._ledger_path().exists():
            return {}
        rows = {}
        for line in self._ledger_path().read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            rows[row['phase_stage']] = row
        return rows

    def _ledger_total(self):
        return sum(row['questions'] for row in self._ledger_rows().values())

    def _write_ledger(self, rows):
        path = self._ledger_path()
        body = ''.join(json.dumps(rows[k], ensure_ascii=False, sort_keys=True) + '\n'
                       for k in sorted(rows))
        tmp = path.with_suffix('.tmp')
        tmp.write_text(body)
        tmp.replace(path)

    def _reserve(self, phase_stage, case_id, questions):
        rows = self._ledger_rows()
        if phase_stage in rows:
            return  # idempotent: checkpoint resumes never charge twice
        if self.spec.max_question_runs is not None and self._ledger_total() + questions > self.spec.max_question_runs:
            raise ValueError(f'Question-run cap exceeded before {phase_stage}: {self._ledger_total()}'
                             f'+{questions} > {self.spec.max_question_runs}')
        rows[phase_stage] = {'phase_stage': phase_stage, 'case_id': case_id,
                             'questions': questions, 'state': 'reserved'}
        self._write_ledger(rows)

    def _settle(self, phase_stage, questions):
        rows = self._ledger_rows()
        row = rows.get(phase_stage)
        if row is None:
            # A stage produced results without a reservation (crash before the gate):
            # count it honestly rather than letting it ride free.
            if self.spec.max_question_runs is not None and self._ledger_total() + questions > self.spec.max_question_runs:
                raise ValueError(f'Question-run cap exceeded while settling {phase_stage}: '
                                 f'{self._ledger_total()}+{questions} > {self.spec.max_question_runs}')
            rows[phase_stage] = {'phase_stage': phase_stage, 'questions': questions, 'state': 'settled'}
            self._write_ledger(rows)
        elif row.get('state') != 'settled':
            row['questions'] = questions
            row['state'] = 'settled'
            self._write_ledger(rows)

    def _settle_train_from_decisions(self):
        """Settle by decision order: a round whose admission failed never ran questions and
        must not stop the scan at later rounds."""
        train = self.root / 'train'
        stages = ['B0']
        n = 1
        while (train / f'R{n}' / 'decision.json').exists():
            stages.append(f'R{n}'); n += 1
        for name in stages:
            total = 0
            for case_id in self.spec.train:
                result_path = train / name / 'generation' / case_id / 'result.json'
                if result_path.exists():
                    total += len(json.loads(result_path.read_text())['answers'])
            if total:
                self._settle(f'train/{name}', total)

    # ---- one case under one bundle -------------------------------------------
    def _client(self, stage_dir):
        if self._injected_client is not None:
            return self._injected_client(stage_dir)
        import copy
        cfg = copy.deepcopy(self.connection_config)
        cfg.work_dir = Path(stage_dir) / 'runtime'
        return LLMClient(cfg)

    async def _case_stage(self, phase, case_id, task_spec, stage_dir):
        self.verify(); started = time.time(); stage_dir = Path(stage_dir)
        client = self._client(stage_dir)
        try:
            case = self.adapter.generation_input(case_id)
            result = await Pipeline(client, stage_dir / 'generation',
                                    frozen_snapshot=None if self.snapshot_root is None else self.snapshot_root / case_id
                                    ).run(case, task_spec, self.config)
            scores_path = stage_dir / 'evaluation.json'
            if scores_path.exists():
                saved = json.loads(scores_path.read_text())
                if saved['run_identity'] != result.identity or saved['asset_version'] != task_spec.bundle.version:
                    raise ValueError('Evaluation checkpoint identity mismatch')
                scores = EvaluationResult(**saved['scores'])
            else:
                evaluator = self.evaluator_factory(client, stage_dir / 'evaluation')
                scores = await evaluator.evaluate(result)
                atomic_json(scores_path, {'run_identity': result.identity,
                                          'asset_version': task_spec.bundle.version, 'scores': scores.to_dict()})
            self.verify()
            atomic_json(stage_dir / 'stage.json', {
                'phase': phase, 'case_id': case_id,
                'status': 'complete' if scores.completed == scores.total and not scores.evaluation_faults else 'failed',
                'run_identity': result.identity, 'asset_version': task_spec.bundle.version,
                'memory_count': result.memory_count, 'memory_fingerprint': result.memory_fingerprint,
                'graph_fingerprint': result.graph_fingerprint, 'scores': scores.to_dict(),
                'calls': client.ledger_summary(), 'elapsed_s': round(time.time() - started, 2)})
            return result, scores
        finally:
            await client.aclose()

    async def _split_stage(self, phase, case_ids, task_spec, stage_dir):
        """Run every case of a split on the same bundle; aggregate by the frozen sum rule."""
        results = []; scores = []
        for case_id in case_ids:
            result, case_scores = await self._case_stage(phase, case_id, task_spec, stage_dir / case_id)
            results.append(result); scores.append(case_scores)
        return results, aggregate_scores(scores)

    # ---- candidate chain from the training tree -------------------------------
    def _candidate_chain(self):
        train = self.root / 'train'
        b0_version = json.loads((train / 'B0' / 'assets' / 'manifest.json').read_text())['version']
        chain = [{'order': 0, 'version': b0_version, 'path': train / 'published' / 'versions' / b0_version}]
        n = 1
        while (train / f'R{n}' / 'decision.json').exists():
            decision = json.loads((train / f'R{n}' / 'decision.json').read_text())
            if decision.get('accepted') and decision.get('candidate_version'):
                version = decision['candidate_version']
                chain.append({'order': n, 'version': version, 'path': train / 'published' / 'versions' / version})
            n += 1
        for candidate in chain:
            if not (candidate['path'] / 'manifest.json').exists():
                raise ValueError(f"Adopted bundle missing from publication tree: {candidate['version']}")
        return chain

    def _register_train_runs(self):
        train = self.root / 'train'
        stages = ['B0']
        n = 1
        while (train / f'R{n}' / 'generation' / case_id / 'result.json').exists():
            stages.append(f'R{n}'); n += 1
        for name in stages:
            result_path = train / name / 'generation' / case_id / 'result.json'
            if result_path.exists():
                row = json.loads(result_path.read_text())
                self._register('train', case_id, row['asset_version'], len(row['answers']))

    # ---- orchestration ---------------------------------------------------------
    async def run(self, task_spec, resume=False, rounds=None, scope=(), b0_gate=None):
        self.root.mkdir(parents=True, exist_ok=True)
        declaration = {'experiment_spec': self.spec.declaration(), 'task': task_spec.declaration(),
                       'config': self.config.to_dict(),
                       'connection': transport_identity(type('Connection', (), {'cfg': self.connection_config})()),
                       'frozen_files': self.frozen}
        declaration = json.loads(json.dumps(declaration, ensure_ascii=False))
        state_path = self.root / 'campaign.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if not resume or state['declaration'] != declaration:
                raise ValueError('Existing campaign requires explicit resume with exactly the same identity')
            if state.get('phase') == 'done':
                if (self.root / 'campaign-summary.json').exists():
                    raise ValueError('Campaign sealed: the test split is unblinded and the campaign is final; start a new root')
                # 封存先于报告落盘的旧事故现场：只重建报告，不重新执行任何阶段
                return self._rebuild_summary()
            if state.get('phase') == 'blocked_b0':
                raise ValueError('Campaign blocked at the B0 gate; the run identity is unchanged so resume cannot change the outcome')
        else:
            state = {'declaration': declaration, 'phase': 'precheck'}
            atomic_json(state_path, state)
        # Phase discipline: once past training, the campaign never returns to proposals.
        if state.get('phase') in ('validation', 'selection', 'test'):
            return await self._resume_from(task_spec, state, state_path)
        precheck = self.root / 'precheck.json'
        if not precheck.exists() or not json.loads(precheck.read_text()).get('passed'):
            raise ValueError('Precheck missing or not passed: run datasets/locomo/scripts/precheck first')
        recorded = json.loads(precheck.read_text()).get('identity')
        expected_identity = precheck_identity(self.connection_config, self.config)
        if recorded != expected_identity:
            raise ValueError('Precheck identity mismatch: the record was produced by a different '
                             'routing/config/framework and cannot authorize this campaign')
        self.verify()

        def set_phase(phase):
            state = json.loads(state_path.read_text())
            atomic_json(state_path, {**state, 'phase': phase})

        train_cases = self.spec.train
        train_questions = sum(len(self.adapter.generation_input(c).questions) for c in train_cases)
        set_phase('train')
        runner = ExperimentRunner(self.adapter, self.evaluator_factory, self.connection_config,
                                  self.config, self.spec.adoption, self.root / 'train', self.frozen_files,
                                  client_factory=self._injected_client, bootstrap_context=self.bootstrap_context,
                                  snapshot_root=self.snapshot_root,
                                  bootstrap_trial_graph=self.bootstrap_trial_graph,smoke_judge=self.smoke_judge)
        if b0_gate is None:
            def b0_gate(scores):
                return scores.completed == scores.total and scores.generation_faults == 0 and scores.evaluation_faults == 0
        train_summary = await runner.run(train_cases, task_spec,
                                         rounds=self.spec.rounds if rounds is None else rounds,
                                         resume=resume, stop_file=self.root / 'STOP', b0_gate=b0_gate, scope=scope,
                                         stage_gate=lambda name: self._reserve(
                                             f'train/{name}', '+'.join(train_cases), train_questions))
        self._settle_train_from_decisions()
        if train_summary['status'] == 'blocked_b0':
            set_phase('blocked_b0')
            return {'status': 'blocked_b0', 'train': train_summary}
        return await self._finalize(task_spec, train_summary)

    async def _resume_from(self, task_spec, state, state_path):
        """Post-training phases only: validation, selection and test are final and never
        re-open proposals, whatever happened to the STOP marker meanwhile."""
        train_summary = json.loads((self.root / 'train' / 'summary.json').read_text())
        return await self._finalize(task_spec, train_summary)

    def _stage_statuses(self):
        """Validation/test keep per-case records at <phase>/<version>/<case>/stage.json;
        training keeps one aggregated record per round at train/<round>/stage.json. Both
        surface in the campaign status: a faulted training stage is not a normal rejection."""
        statuses = {}
        for path in sorted((self.root / 'train').glob('*/stage.json')):
            row = json.loads(path.read_text())
            scores = row.get('scores', {})
            failed = (row.get('status') != 'complete'
                      or scores.get('completed') != scores.get('total')
                      or scores.get('generation_faults') or scores.get('evaluation_faults'))
            statuses[f'train/{path.parent.name}'] = {
                'status': 'failed' if failed else 'complete',
                'version': row.get('asset_version'),
                'case': '+'.join(row.get('cases', [])), 'phase': 'train'}
        for phase in ('validation', 'test'):
            for path in sorted((self.root / phase).glob('*/*/stage.json')):
                row = json.loads(path.read_text())
                key = f"{phase}/{path.parents[1].name}/{path.parent.name}"
                statuses[key] = {'status': row.get('status'),
                                 'version': path.parents[1].name,
                                 'case': path.parent.name,
                                 'phase': phase}
        return statuses

    async def _finalize(self, task_spec, train_summary):
        def set_phase(phase):
            state = json.loads((self.root / 'campaign.json').read_text())
            atomic_json(self.root / 'campaign.json', {**state, 'phase': phase})

        chain = self._candidate_chain()
        validation_cases = self.spec.validation
        validation_questions = sum(len(self.adapter.generation_input(c).questions) for c in validation_cases)
        set_phase('validation')
        validation = {}
        for candidate in chain:
            self._reserve(f"validation/{candidate['version']}", '+'.join(validation_cases), validation_questions)
            results, scores = await self._split_stage(
                'validation', validation_cases, task_spec.with_bundle(KernelBundle(candidate['path'])),
                self.root / 'validation' / candidate['version'])
            self._settle(f"validation/{candidate['version']}", sum(len(r.answers) for r in results))
            validation[candidate['version']] = scores.to_dict()

        set_phase('selection')
        baseline_scores = EvaluationResult(**validation[chain[0]['version']])
        candidates = [{'order': c['order'], 'version': c['version'],
                       'scores': EvaluationResult(**validation[c['version']])} for c in chain]
        decision = self.spec.selection.decide(baseline_scores, candidates)
        selected = decision['selected'] or chain[0]['version']
        atomic_json(self.root / 'selected.json', {
            'selected': selected, 'baseline': chain[0]['version'], 'decision': decision,
            'fingerprints': {c['version']: KernelBundle(c['path']).version for c in chain},
            'sealed_before_test': True})

        test_cases = self.spec.test
        test_versions = [chain[0]['version']] + ([selected] if selected != chain[0]['version'] else [])
        test_questions = sum(len(self.adapter.generation_input(c).questions) for c in test_cases)
        set_phase('test')
        test = {}
        for version in test_versions:
            path = next(c['path'] for c in chain if c['version'] == version)
            self._reserve(f'test/{version}', '+'.join(test_cases), test_questions)
            results, scores = await self._split_stage('test', test_cases,
                                                      task_spec.with_bundle(KernelBundle(path)),
                                                      self.root / 'test' / version)
            self._settle(f'test/{version}', sum(len(r.answers) for r in results))
            test[version] = scores.to_dict()

        # 报告先持久化，封存后置：写失败时 phase 仍可恢复，不会出现「已封存但无报告」
        summary = self._assemble_summary(train_summary, chain, validation, decision, selected, test)
        set_phase('done')
        return summary

    def _assemble_summary(self, train_summary, chain, validation, decision, selected, test):
        """Build AND persist the final report; both the finalize path and the rebuild path
        land here so the two can never drift."""
        stage_statuses = self._stage_statuses()
        healthy = (train_summary.get('status') == 'complete'
                   and all(v['status'] == 'complete' for v in stage_statuses.values())
                   and all(EvaluationResult(**scores).completed == EvaluationResult(**scores).total
                           and EvaluationResult(**scores).evaluation_faults == 0
                           for scores in list(validation.values()) + list(test.values())))
        summary = {'status': 'complete' if healthy else 'failed',
                   'unhealthy_stages': {k: v for k, v in stage_statuses.items()
                                        if v['status'] != 'complete'},
                   'train': train_summary,
                   'candidates': [{'order': c['order'], 'version': c['version']} for c in chain],
                   'validation': validation, 'selection': decision, 'selected': selected,
                   'test': test, 'question_runs': self._ledger_total(),
                   'operator_stopped': train_summary.get('stopped_by_operator', False)}
        atomic_json(self.root / 'campaign-summary.json', summary)
        return summary

    def _rebuild_summary(self):
        """The campaign reached done but the report never landed: rebuild it from on-disk
        artifacts. Nothing re-executes — training stays locked and the test split stays
        sealed; only the missing report is recovered."""
        train_summary = json.loads((self.root / 'train' / 'summary.json').read_text())
        chain = self._candidate_chain()
        selected_record = json.loads((self.root / 'selected.json').read_text())

        def phase_scores(phase):
            out = {}
            for candidate in chain:
                version = candidate['version']
                split = self.root / phase / version
                if not split.exists():
                    continue
                scores = [EvaluationResult(**json.loads(p.read_text())['scores'])
                          for p in sorted(split.glob('*/stage.json'))]
                if scores:
                    out[version] = aggregate_scores(scores).to_dict()
            return out

        return self._assemble_summary(train_summary, chain, phase_scores('validation'),
                                      selected_record['decision'], selected_record['selected'],
                                      phase_scores('test'))
