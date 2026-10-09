"""Backend dispatch shared by the existing experiment and campaign facades."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from yaml import YAMLError

from darwinagent.agent import AgentKernelBundle, AgentTask, EpisodeResult
from darwinagent.contracts import freeze, plain
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import transport_identity
from darwinagent.runtime.leases import execution_lease

from .episode_contracts import (
    AgentExperimentSpec,
    EpisodeEvaluation,
    episode_metrics,
    select_episode_candidate,
)
from .episode_proposal import WikiBudgetClient, decode_candidate, propose
from .wiki import WikiMaintainer


def source_identity(value):
    target = value if inspect.isfunction(value) else type(value)
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError) as exc:
        raise ValueError("Experiment extensions must have inspectable source") from exc
    source_file = inspect.getsourcefile(target)
    return digest(
        {
            "definition": source,
            "module": digest(Path(source_file).read_text()) if source_file else None,
        }
    )


def read_record(path):
    record = json.loads(Path(path).read_text())
    if digest(record["value"]) != record["digest"]:
        raise ValueError("Experiment checkpoint changed: " + str(path))
    return record["value"]


def save_record(path, value):
    atomic_json(path, {"value": value, "digest": digest(value)})


class EpisodePending(RuntimeError):
    def __init__(self, state, key):
        self.state, self.key = state, key


class EpisodeExperiment:
    def __init__(self, owner, spec, baseline, rounds):
        if not isinstance(spec, AgentExperimentSpec) or not isinstance(baseline, AgentKernelBundle):
            raise TypeError("Generic experiments require AgentExperimentSpec and AgentKernelBundle")
        if type(rounds) is not int or rounds < 0:
            raise ValueError("Generic experiments require a finite nonnegative round count")
        self.owner, self.spec, self.baseline, self.rounds = owner, spec, baseline, rounds
        if type(owner.wiki_call_limit) is not int or owner.wiki_call_limit < 1:
            raise ValueError("Wiki model call limit must be positive")
        self.root, self.backend = owner.root, owner.backend
        self.tasks = {}
        for split in (spec.train, spec.validation, spec.admission, spec.test):
            for task_id in split:
                task = owner.adapter.task(task_id)
                if not isinstance(task, AgentTask) or task.id != task_id:
                    raise ValueError("Dataset.task must return the requested AgentTask")
                self.tasks[task_id] = task
        self.evaluator = owner.evaluator_factory(self.backend.client, self.root / "evaluation")
        declaration = self.evaluator.declaration()
        if not isinstance(declaration, dict):
            raise ValueError("Evaluator must declare its criterion and private reference identity")
        self.evaluator_id = digest(
            {"source": source_identity(self.evaluator), "declaration": declaration}
        )
        if any(t.effect == "write" for t in self.backend.tools.values()) and not callable(
            getattr(self.backend, "for_episode", None)
        ):
            raise ValueError(
                "Write tools require backend.for_episode(episode_root, task) workspace isolation"
            )
        self.identity = plain(
            freeze(
                {
                    "protocol": "agent-experiment-v1",
                    "campaign_root": str(self.root.resolve()),
                    "spec": spec.declaration(),
                    "rounds": rounds,
                    "baseline": baseline.content_id,
                    "tasks": {k: t.to_dict() for k, t in self.tasks.items()},
                    "dataset": {
                        "source": source_identity(owner.adapter),
                        "declaration": owner.adapter.identity(),
                    },
                    "backend": self.backend.identity(),
                    "backend_source": source_identity(self.backend),
                    "evaluator": self.evaluator_id,
                    "evaluator_factory": source_identity(owner.evaluator_factory),
                    "config": owner.config.to_dict(),
                    "wiki_call_limit": owner.wiki_call_limit,
                    "proposal_transport": transport_identity(
                        type("Connection", (), {"cfg": owner.connection_config})()
                    ),
                    "proposal_client_source": source_identity(owner._injected_client)
                    if owner._injected_client
                    else None,
                    "frozen_files": owner.frozen,
                }
            )
        )
        self.campaign_id = digest(self.identity)

    def verify(self):
        self.owner.verify()
        if self.owner.config.to_dict() != self.identity["config"]:
            raise ValueError("Experiment configuration changed during campaign")
        if source_identity(self.owner.adapter) != self.identity["dataset"]["source"]:
            raise ValueError("Dataset source changed during campaign")
        if self.owner.adapter.identity() != self.identity["dataset"]["declaration"]:
            raise ValueError("Dataset configuration changed during campaign")
        if source_identity(self.backend) != self.identity["backend_source"]:
            raise ValueError("Backend source changed during campaign")
        if self.backend.identity() != self.identity["backend"]:
            raise ValueError("Backend identity changed during campaign")
        if (
            digest(
                {
                    "source": source_identity(self.evaluator),
                    "declaration": self.evaluator.declaration(),
                }
            )
            != self.evaluator_id
        ):
            raise ValueError("Evaluator identity changed during campaign")
        for task_id, expected in self.tasks.items():
            if self.owner.adapter.task(task_id).to_dict() != expected.to_dict():
                raise ValueError("Dataset task changed during campaign")

    def retain(self, bundle):
        bundle.verify()
        path = self.root / "versions" / bundle.content_id
        if path.exists():
            retained = AgentKernelBundle(path)
        else:
            retained = bundle.assets.export(path)
        if retained.content_id != bundle.content_id:
            raise ValueError("Retained kernel content differs")
        return retained

    async def episode_backend(self, key, task):
        factory = getattr(self.backend, "for_episode", None)
        if factory is None:
            return self.backend
        derived = factory(self.root / "episodes" / key / "environment", task)
        if inspect.isawaitable(derived):
            derived = await derived
        before, after = self.backend.identity(), derived.identity()
        if any(
            before.get(k) != after.get(k)
            for k in ("profile_id", "adapter_id", "runtime_id", "transport")
        ):
            raise ValueError("Episode factory changed fixed runtime, adapter, profile or model")
        if source_identity(derived) != source_identity(self.backend):
            raise ValueError("Episode factory changed backend implementation")
        if set(derived.tools) != set(self.backend.tools):
            raise ValueError("Episode factory changed primitive permissions")
        for name, tool in self.backend.tools.items():
            a, b = tool.declaration(), derived.tools[name].declaration()
            if tool.effect == "write":
                locations = ("workspace", "location", "root")
                if not any(b["identity"].get(k) != a["identity"].get(k) for k in locations):
                    raise ValueError(
                        "Write primitive must declare an isolated workspace/location identity"
                    )
                expected_root = (self.root / "episodes" / key / "environment").resolve()
                for field in ("workspace", "root"):
                    location = b["identity"].get(field)
                    if location is not None and not Path(location).resolve().is_relative_to(
                        expected_root
                    ):
                        raise ValueError(
                            "Write workspace must be inside the allocated episode directory"
                        )
            # Only explicit workspace/location declarations can vary across isolated copies.
            for row in (a, b):
                row["identity"] = {
                    k: v
                    for k, v in row["identity"].items()
                    if k not in {"workspace", "location", "root"}
                }
            if a != b or source_identity(tool.handler) != source_identity(
                derived.tools[name].handler
            ):
                raise ValueError("Episode factory changed primitive contract/source")
        return derived

    async def episode(self, phase, label, task_id, bundle):
        self.verify()
        key = digest(
            {
                "campaign": self.campaign_id,
                "phase": phase,
                "label": label,
                "task": task_id,
                "kernel": bundle.content_id,
            }
        )
        root = self.root / "episodes" / key
        path = root / "evaluation.json"
        if path.exists():
            saved = read_record(path)
            return EpisodeResult(**read_record(root / "episode.json")), saved
        ledger_path = self.root / "episode-ledger.json"
        ledger = read_record(ledger_path) if ledger_path.exists() else {}
        if key not in ledger:
            if len(ledger) >= self.spec.max_episode_runs:
                raise EpisodePending("budget_exhausted", key)
            ledger[key] = {
                "phase": phase,
                "task_id": task_id,
                "kernel": bundle.content_id,
                "state": "reserved",
            }
            save_record(ledger_path, ledger)
        backend = await self.episode_backend(key, self.tasks[task_id])
        result_path = root / "episode.json"
        if result_path.exists():
            result = EpisodeResult(**read_record(result_path))
        else:
            result = await backend.run(self.tasks[task_id], bundle, run_id=key)
            if not isinstance(result, EpisodeResult):
                raise TypeError("Backend must return EpisodeResult")
            if result.status == "unknown":
                ledger[key]["state"] = "unknown"
                save_record(ledger_path, ledger)
                raise EpisodePending("unknown", key)
            save_record(result_path, result.to_dict())
        try:
            evaluation = await self.evaluator.evaluate(self.tasks[task_id], result)
            if not isinstance(evaluation, EpisodeEvaluation):
                raise TypeError("EpisodeEvaluator must return EpisodeEvaluation")
            error = None
        except Exception as exc:
            # Exception text may include private gold. Keep only the failure class.
            evaluation = EpisodeEvaluation(0, 0, healthy=False)
            error = type(exc).__name__
        self.verify()
        saved = {
            "metrics": episode_metrics(result, evaluation),
            "evaluation": evaluation.to_dict(),
            "error_type": error,
        }
        save_record(path, saved)
        ledger[key]["state"] = "complete" if saved["metrics"]["healthy"] else "failed"
        save_record(ledger_path, ledger)
        return result, saved

    async def split(self, phase, label, bundle, wiki=None):
        metrics = {}
        for task_id in getattr(self.spec, phase):
            result, scored = await self.episode(phase, label, task_id, bundle)
            metrics[task_id] = scored["metrics"]
            if wiki is not None:
                if phase != "train":
                    raise PermissionError("Only training episodes can enter proposal Wiki")
                original = {
                    "task": self.tasks[task_id].to_dict(),
                    "episode": result.to_dict(),
                    "public_feedback": scored["evaluation"]["public_feedback"],
                }
                ref = wiki.service.register(
                    original,
                    scope={"case_ids": [task_id], "stage_ids": [label]},
                    source_kind="training",
                )
                await wiki.record(
                    label,
                    "episode",
                    {
                        "task_id": task_id,
                        "kernel_id": bundle.content_id,
                        "training_original": ref,
                        "public_feedback": scored["evaluation"]["public_feedback"],
                    },
                    scope="training",
                    source="episode",
                    infer=False,
                )
        return metrics

    async def run(self, resume):
        self.root.mkdir(parents=True, exist_ok=True)
        with execution_lease(self.root / "campaign.lock"):
            state_path = self.root / "agent-campaign.json"
            if state_path.exists():
                if not resume or read_record(state_path) != self.identity:
                    raise ValueError(
                        "Existing campaign requires explicit resume with identical inputs/source/evaluator"
                    )
            else:
                save_record(state_path, self.identity)
            self.verify()
            final_path = self.root / "agent-summary.json"
            if final_path.exists():
                summary = read_record(final_path)
                selected = AgentKernelBundle(self.root / "selected")
                if selected.content_id != summary["selected"]:
                    raise ValueError("Selected version changed")
                for content_id in summary["accepted_versions"]:
                    if (
                        AgentKernelBundle(self.root / "versions" / content_id).content_id
                        != content_id
                    ):
                        raise ValueError("Accepted version changed")
                read_record(self.root / "selection.json")
                return summary
            try:
                return await self.evolve()
            except EpisodePending as pending:
                state = {"status": pending.state, "pending_episode": pending.key}
                save_record(self.root / "agent-progress.json", state)
                return state

    async def evolve(self):
        baseline = self.retain(self.baseline)
        admission = self.backend.admit(baseline)
        save_record(self.root / "baseline-admission.json", admission)
        if not admission.get("accepted") or admission.get("kernel_id") != baseline.content_id:
            raise ValueError("Baseline static admission did not confirm its content id")
        admitted = await self.split("admission", "K0", baseline)
        if not all(row["healthy"] for row in admitted.values()):
            return {"status": "blocked_baseline", "admission": admitted}
        wiki = WikiMaintainer(
            self.root,
            self.campaign_id,
            lambda stage: WikiBudgetClient(
                self.owner._client, stage, self.root / "wiki-calls.json", self.owner.wiki_call_limit
            ),
            self.owner.config,
            self.owner.wiki_call_limit,
        )
        await self.split("train", "K0", baseline, wiki)
        selected = baseline
        selected_scores = await self.split("validation", "K0", selected)
        decisions, accepted = [], [baseline.content_id]
        for number in range(1, self.rounds + 1):
            label = f"R{number}"
            root = self.root / "rounds" / label
            decision_path = root / "decision.json"
            if decision_path.exists():
                decision = read_record(decision_path)
                if decision["base"] != selected.content_id:
                    raise ValueError("Saved candidate baseline changed")
                decisions.append(decision)
                if decision["accepted"]:
                    selected = AgentKernelBundle(self.root / "versions" / decision["candidate"])
                    accepted.append(selected.content_id)
                    selected_scores = await self.split("validation", label, selected)
                if decision["reason"] == "no_change":
                    break
                continue
            action = await propose(self.owner, wiki, selected, root, self.spec.proposal_call_limit)
            save_record(root / "candidate-payload.json", action)
            decision = {"base": selected.content_id, "candidate": None, "accepted": False}
            if action["action"] == "no_change":
                decision.update(reason="no_change", explanation=action["reason"])
            else:
                try:
                    assets = decode_candidate(
                        action,
                        selected,
                        self.backend.profile,
                        {"base": selected.content_id, "round": label},
                    )
                    candidate_path = root / "candidate"
                    candidate = (
                        AgentKernelBundle(candidate_path)
                        if candidate_path.exists()
                        else assets.export(candidate_path)
                    )
                    if candidate.content_id != assets.content_id:
                        raise ValueError("Saved candidate differs from proposal")
                    decision["candidate"] = candidate.content_id
                    static = self.backend.admit(candidate)
                    if (
                        not static.get("accepted")
                        or static.get("kernel_id") != candidate.content_id
                    ):
                        raise ValueError("Static admission did not confirm candidate content id")
                    save_record(root / "admission.json", static)
                except (ValueError, TypeError, KeyError, YAMLError) as exc:
                    decision.update(reason="static_admission_failed", error=str(exc))
                else:
                    admission_scores = await self.split("admission", label, candidate)
                    if not all(row["healthy"] for row in admission_scores.values()):
                        decision.update(
                            reason="behavioral_admission_failed", admission=admission_scores
                        )
                    else:
                        await self.split("train", label, candidate, wiki)
                        candidate_scores = await self.split("validation", label, candidate)
                        decision.update(select_episode_candidate(selected_scores, candidate_scores))
                        if decision["accepted"]:
                            selected = self.retain(candidate)
                            selected_scores = candidate_scores
                            accepted.append(selected.content_id)
            save_record(decision_path, decision)
            decisions.append(decision)
            if decision["reason"] == "no_change":
                break
        selection = {
            "baseline": baseline.content_id,
            "selected": selected.content_id,
            "accepted_versions": accepted,
            "decisions": decisions,
            "sealed_before_test": True,
        }
        selection_path = self.root / "selection.json"
        if selection_path.exists():
            if read_record(selection_path) != selection:
                raise ValueError("Frozen selection changed")
        else:
            save_record(selection_path, selection)
        target = self.root / "selected"
        exported = AgentKernelBundle(target) if target.exists() else selected.assets.export(target)
        if exported.content_id != selected.content_id:
            raise ValueError("Exported selected kernel differs")
        test = {}
        if self.spec.test:
            for bundle in {baseline.content_id: baseline, selected.content_id: selected}.values():
                test[bundle.content_id] = await self.split("test", "sealed", bundle)
        healthy = all(
            row["healthy"] for rows in [selected_scores, *test.values()] for row in rows.values()
        )
        ledger = read_record(self.root / "episode-ledger.json")
        failures = {key: row for key, row in ledger.items() if row["state"] == "failed"}
        summary = {
            "status": "complete" if healthy and not failures else "failed",
            **selection,
            "validation": selected_scores,
            "test": test,
            "selected_path": str(target.resolve()),
            "episode_runs": len(ledger),
            "unhealthy_episodes": failures,
        }
        save_record(self.root / "agent-summary.json", summary)
        return summary


async def run_episodes(owner, spec, baseline, *, rounds=None, resume=False):
    return await EpisodeExperiment(
        owner, spec, baseline, spec.rounds if rounds is None else rounds
    ).run(resume)
