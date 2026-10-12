"""Opt-in full-LIFT adapters; historical adapter identities remain unchanged."""

from dataclasses import dataclass
from pathlib import Path

from darwinagent.agent import AgentKernelAssets, ExecutionBackend, ExecutionProfile
from darwinagent.runtime.artifacts import atomic_json

from .agents import DarwinChatAgent, NativeReAct
from .full_tools import FullLocalTools

NATIVE_ROUNDS = 30


def full_worker_profile():
    names = tuple(t.name for t in FullLocalTools(Path(".")).specs())
    return ExecutionProfile("lift-full-tools-v1", names, max_model_calls=None, max_leaf_calls=64)


@dataclass(frozen=True)
class FullNativeReAct(NativeReAct):
    worker_session_cap: int | None = None
    worker_turn_model_cap: int | None = None

    def identity(self):
        return dict(super().identity(), native_correction_rounds=NATIVE_ROUNDS)


class FullDarwinChatAgent(DarwinChatAgent):
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
        trace=False,
        cell_id=None,
        arm=None,
        phase=None,
    ):
        import json

        from darwinagent.agent import ReActAgent

        self.task, self.shared, self.root, self.kernel = public_task, shared, Path(root), kernel
        self.role, self.run_id, self.session, self.trace = role, run_id, session, trace
        self.cursor = 0
        declaration = dict(
            task_id=public_task.id,
            cell_id=cell_id,
            arm=arm,
            phase=phase,
            session=session,
            run_id=run_id,
            role=role,
            kernel_id=kernel.content_id if kernel else None,
        )
        path = self.root / "session-identity.json"
        if path.exists():
            if json.loads(path.read_text()) != declaration:
                raise ValueError("Frozen full session identity changed")
        else:
            atomic_json(path, declaration)
        for tool in tools:
            tool.handler.__self__.audit_path = self.root / "tool-timings"
        profile = (
            full_worker_profile()
            if role == "worker"
            else ExecutionProfile(
                "lift-full-judge-read-v1",
                tuple(t.name for t in tools),
                prompt_roles={"judge": ("instruction", "tools", "view")},
                max_model_calls=None,
                max_leaf_calls=64,
            )
        )
        prompt = ReActAgent.system_prompt
        if role == "judge":
            prompt += (
                " Inspect delivered files using read tools. Keep native success, reason "
                "and score JSON inside the outer final_response STRING."
            )
        self.backend = ExecutionBackend(
            FullNativeReAct(max_turns=None, role=role, system_prompt=prompt, max_tokens=16384),
            tools,
            profile,
            shared.scoped(session, role),
            self.root / "episodes",
        )


def full_initial_kernel(root):
    """Re-export the same K0 S/F/C/P content under the seven-tool execution contract."""
    from tempfile import TemporaryDirectory

    from .kernel import initial_kernel

    with TemporaryDirectory() as temporary:
        old = initial_kernel(Path(temporary) / "old", max_model_calls=None)
        return AgentKernelAssets(old.assets.assets, full_worker_profile()).export(root)
