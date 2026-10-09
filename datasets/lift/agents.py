"""Host ChatAgents; worker sees only the projected task and native feedback."""

import json
import time
from pathlib import Path

from src.lift.eval.chat_agent import ChatAgent

from darwinagent.agent import AgentTask, ExecutionBackend, ExecutionProfile, ReActAgent
from darwinagent.llm.client import BudgetExceeded
from darwinagent.runtime.artifacts import atomic_json, digest

from . import tracing
from .tools import NAMES, DockerTools


def worker_profile():
    return ExecutionProfile("lift-tools-v1", NAMES, max_model_calls=8, max_leaf_calls=64)


class NativeReAct(ReActAgent):
    def identity(self):
        return {
            "runtime": "darwinagent",
            "max_turns": self.max_turns,
            "role": self.role,
            "system_prompt": self.system_prompt,
            "worker_session_cap": 8,
            "native_correction_rounds": 3,
        }


class LocalTaskFailure(RuntimeError):
    """Settled task-local terminal failure; independent tasks may continue."""


class DarwinChatAgent(ChatAgent):
    def __init__(
        self,
        public_task,
        tools,
        shared,
        root,
        kernel,
        *,
        role,
        run_id,
        session,
        trace=True,
        cell_id=None,
        arm=None,
        phase=None,
    ):
        self.task, self.shared, self.root, self.kernel = public_task, shared, Path(root), kernel
        self.role, self.run_id, self.session, self.trace = role, run_id, session, trace
        self.root.mkdir(parents=True, exist_ok=True)
        self.cursor = 0
        atomic_json(
            self.root / "session-identity.json",
            {
                "task_id": public_task.id,
                "cell_id": cell_id,
                "arm": arm,
                "phase": phase,
                "session": session,
                "run_id": run_id,
                "role": role,
                "kernel_id": kernel.content_id if kernel is not None else None,
            },
        )
        for tool in tools:
            tool.handler.__self__.audit_path = self.root / "tool-timings"
        profile = (
            worker_profile()
            if role == "worker"
            else ExecutionProfile(
                "lift-judge-read-v1",
                tuple(t.name for t in tools),
                prompt_roles={"judge": ("instruction", "tools", "view")},
                max_model_calls=8,
                max_leaf_calls=64,
            )
        )
        prompt = ReActAgent.system_prompt
        if role == "judge":
            prompt += " Inspect delivered files using read tools. final_response must be a JSON string with success, reason, score as requested by the native judge prompt."
        self.backend = ExecutionBackend(
            NativeReAct(max_turns=16, role=role, system_prompt=prompt),
            tools,
            profile,
            shared.scoped(session, role),
            self.root / "episodes",
        )

    @property
    def agent_name(self):
        return "darwinagent"

    def augment_work_prompt(self, task, prompt):
        if task.query != self.task.instruction:
            raise ValueError("Worker public query mismatch")
        return prompt

    async def chat(self, message, *, session_id):
        if session_id != self.session:
            raise ValueError("Session identity changed")
        path = self.root / "chat.json"
        state = json.loads(path.read_text()) if path.exists() else {"history": [], "turns": []}
        # Native retries repeat the same message. Replay the completed receipt, not a send.
        key = digest(message)
        if self.cursor < len(state["turns"]):
            old = state["turns"][self.cursor]
            if old["message_hash"] != key:
                raise ValueError("Native replay message changed")
            self.cursor += 1
            if old["episode"]["status"] != "completed":
                self.shared.check()
                raise LocalTaskFailure(old["episode"]["status"])
            return old["episode"]["final_response"]
        terminal = self.root / "terminal-failure.json"
        if terminal.exists():
            self.shared.check()
            raise LocalTaskFailure(json.loads(terminal.read_text())["status"])
        started = time.time()
        turn_path = self.root / "turns" / (str(len(state["turns"])) + ".json")
        receipt = {"message_hash": key, "status": "running", "started": started, "episode": None}
        if turn_path.exists():
            receipt = json.loads(turn_path.read_text())
            started = receipt["started"]
        atomic_json(turn_path, receipt)
        try:
            self.shared.check()
            history = state["history"] + [{"role": "user", "content": message}]
            task = AgentTask(
                self.task.id,
                self.task.instruction,
                self.task.attachments,
                {"native_messages": history},
            )
            result = await self.backend.run(
                task, self.kernel, run_id="turn-" + str(len(state["turns"]))
            )
        except Exception as exc:
            receipt.update(
                status="paused" if isinstance(exc, BudgetExceeded) else "failed",
                error_type=type(exc).__name__,
                elapsed_s=time.time() - started,
            )
            atomic_json(turn_path, receipt)
            raise
        receipt.update(
            status=result.status, episode=result.to_dict(), elapsed_s=time.time() - started
        )
        atomic_json(turn_path, receipt)
        atomic_json(self.root / "last-episode.json", result.to_dict())
        if result.status == "unknown":
            self.shared.pause("unknown")
        # A shared global gate always wins over local task termination.
        self.shared.check()
        state["history"] = history + [{"role": "assistant", "content": result.final_response}]
        state["turns"].append(
            {"message_hash": key, "episode": result.to_dict(), "elapsed_s": time.time() - started}
        )
        atomic_json(path, state)
        self.cursor += 1
        try:
            if self.trace:
                tracing.emit(
                    self.root,
                    self.session,
                    self.run_id,
                    self.role,
                    state["history"],
                    [r["episode"] for r in state["turns"]],
                    self.shared,
                    self.trace,
                    started,
                )
        except Exception as exc:
            atomic_json(
                self.root / "trace-errors" / (str(self.cursor) + ".json"),
                {"error_type": type(exc).__name__, "completed_turn": self.cursor},
            )
            self.shared.pause("trace_incomplete")
        self.shared.check()
        if result.status != "completed":
            atomic_json(terminal, {"status": result.status, "turn": self.cursor - 1})
            raise LocalTaskFailure(result.status)
        return result.final_response

    def episodes(self):
        path = self.root / "chat.json"
        return (
            [r["episode"] for r in json.loads(path.read_text())["turns"]] if path.exists() else []
        )

    def count_tools(self):
        return sum(r["usage"]["leaf_calls"] for r in self.episodes())


def tools_for(session, workspace, *, judge=False):
    return DockerTools(session.container_name, workspace, readonly=judge).specs()
