"""Offline task, artifact evaluator and isolated backend for generic experiments."""

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from darwinagent.agent import AgentTask, ExecutionBackend, file_tools
from darwinagent.experiments import EpisodeEvaluation
from darwinagent.runtime.artifacts import digest

from .agent_runtime import schema


class TaskDataset:
    suffix = ""

    def identity(self):
        return {"dataset": "offline-files", "suffix": self.suffix}

    def task(self, task_id):
        return AgentTask(
            task_id, "Write a report " + self.suffix, ({"content": "first", "corrected": "second"},)
        )


class IsolatedFileBackend(ExecutionBackend):
    def for_episode(self, root, task):
        if not isinstance(task, AgentTask):
            raise TypeError("Isolation factory requires only the public AgentTask")
        return type(self)(self.adapter, file_tools(root), self.profile, self.client, self.run_root)


class SharedWorkspaceBackend(IsolatedFileBackend):
    def for_episode(self, root, task):
        return self


class ChangedProfileBackend(IsolatedFileBackend):
    def for_episode(self, root, task):
        from dataclasses import replace

        return type(self)(
            self.adapter,
            file_tools(root),
            replace(self.profile, max_leaf_calls=999),
            self.client,
            self.run_root,
        )


class AsyncIsolatedFileBackend(IsolatedFileBackend):
    async def for_episode(self, root, task):
        return super().for_episode(root, task)


class ArtifactEvaluator:
    def __init__(self, gold="PRIVATE-GOLD-SENTINEL", fail_validation=False, holdout_feedback=False):
        self.gold, self.fail_validation = gold, fail_validation
        self.holdout_feedback = holdout_feedback

    def declaration(self):
        return {
            "criterion": "artifact-v1",
            "references": digest(self.gold),
            "fail_validation": self.fail_validation,
            "holdout_feedback": self.holdout_feedback,
        }

    async def evaluate(self, task, episode):
        if self.fail_validation and task.id == "val":
            raise RuntimeError(self.gold)
        content = Path(episode.artifacts[0]["path"]).read_text() if episode.artifacts else ""
        correct = content == "SECOND"
        return EpisodeEvaluation(
            int(correct),
            int(correct),
            public_feedback=(
                self.gold
                if self.holdout_feedback and task.id == "val"
                else "Inspect the delivered report.",
            ),
            correction_turns=0,
        )


class ProposalClient:
    def __init__(self, responses):
        self.responses, self.messages, self.calls = list(responses), [], 0

    async def chat(self, **kwargs):
        self.messages.append(kwargs["messages"])
        value = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(
            content=json.dumps(value),
            usage={"prompt_tokens": 12, "completion_tokens": 5},
            model="offline",
        )

    async def aclose(self):
        pass


def candidate_action(kernel):
    assets = [a.to_dict() for a in kernel.assets.assets]
    for asset in assets:
        if asset["kind"] == "S":
            asset["content"] = yaml.safe_dump(schema("corrected"))
        elif asset["kind"] == "F":
            asset["content"] = asset["content"].replace('"content":text', '"content":text.upper()')
        elif asset["kind"] == "P":
            asset["content"] = "Deliver a verified report. $instruction"
    assets.append(
        {
            "id": "final",
            "kind": "C",
            "content": "def check(candidate):\n    return candidate['status'] == 'completed'\n",
            "stage": "final_delivery",
            "schema_dependencies": ["schema"],
        }
    )
    return {"action": "submit_patch", "patches": {"assets": assets}}
