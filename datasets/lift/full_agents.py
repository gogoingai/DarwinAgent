"""Opt-in full-LIFT adapters; historical adapter identities remain unchanged."""

from dataclasses import dataclass, field
from pathlib import Path

from darwinagent.agent import AgentKernelAssets, ExecutionBackend, ExecutionProfile
from darwinagent.runtime.artifacts import atomic_json

from .agents import DarwinChatAgent, NativeReAct
from .full_tools import FullLocalTools
from .read_coverage import ReadCoverageCheck

NATIVE_ROUNDS = 30


def full_worker_profile():
    names = tuple(t.name for t in FullLocalTools(Path(".")).specs())
    return ExecutionProfile("lift-full-tools-v1", names, max_model_calls=None, max_leaf_calls=64)


@dataclass(frozen=True)
class FullNativeReAct(NativeReAct):
    expose_execution_budget: bool = field(default=True, kw_only=True)
    worker_session_cap: int | None = None
    worker_turn_model_cap: int | None = None
    max_protocol_failures: int | None = field(default=8, kw_only=True)
    invalid_reply_context_chars: int | None = field(default=2048, kw_only=True)
    max_delivery_rejections: int | None = field(default=None, kw_only=True)
    native_rounds: int = field(default=NATIVE_ROUNDS, kw_only=True)

    def __post_init__(self):
        super().__post_init__()
        if type(self.native_rounds) is not int or not 1 <= self.native_rounds <= NATIVE_ROUNDS:
            raise ValueError("Native correction rounds must be between one and thirty")

    def identity(self):
        return dict(
            super().identity(),
            native_correction_rounds=self.native_rounds,
            expose_execution_budget=self.expose_execution_budget,
            max_protocol_failures=self.max_protocol_failures,
            invalid_reply_context_chars=self.invalid_reply_context_chars,
            max_delivery_rejections=self.max_delivery_rejections,
        )

    async def run(self, task, context):
        if self.max_delivery_rejections is None:
            return await super().run(task, context)
        from darwinagent.agent.context import CheckRejected

        limit = self.max_delivery_rejections

        class GuardedContext:
            rejections = 0

            def __getattr__(self, name):
                return getattr(context, name)

            def check(self, stage, candidate):
                try:
                    return context.check(stage, candidate)
                except CheckRejected:
                    if stage == "final_delivery":
                        self.rejections += 1
                        if self.rejections >= limit:
                            raise ValueError(
                                "Judge delivery checks repeatedly rejected unread results"
                            ) from None
                    raise

        return await super().run(task, GuardedContext())


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
        task_policy=None,
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
        if task_policy is not None:
            declaration["task_execution_policy"] = task_policy.declaration()
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
                host_checks=(
                    ReadCoverageCheck(
                        tools[0].handler.__self__.workspace, self.root / "episodes"
                    ).check,
                ),
                max_model_calls=None,
                max_leaf_calls=64,
            )
        )
        prompt = ReActAgent.system_prompt
        if task_policy is not None and role == "worker":
            prompt += (
                f" The entire task, including native correction turns, has at most "
                f"{task_policy.worker_request_limit} execution model requests. "
                "Plan and deliver within that cumulative task budget; the displayed "
                "per-episode tool/model counters are a separate local limit."
            )
        if role == "judge":
            prompt += (
                " Inspect delivered files using read tools. Keep native success, reason "
                "and score JSON inside the outer final_response STRING. "
                "Before success:true, fully read every current delivered file under result/. "
                "Large read_text responses are pages: follow next_offset until null, "
                "setting expected_sha256 to the first page sha256. Offsets are characters. "
                "A failed or partial read is not evidence of successful delivery. "
                "The final_delivery host check rejects success without complete reads of "
                "the current file versions; recover by listing result files and reading all pages."
            )
        if task_policy is None:
            client = shared.scoped(session, role)
        else:
            from .task_budget import scope_task_worker

            control = None
            if role == "worker" and task_policy.control is not None:
                from .task_control import TaskControl

                control = TaskControl(
                    shared,
                    session,
                    task_policy.worker_request_limit,
                    task_policy.control,
                    self.root / "tool-timings",
                )
                for tool in tools:
                    tool.handler.__self__.retrieval_control = control
                prompt += " A journaled execution_control snapshot gives the actual task-wide remaining requests and retrieval phase. Obey blocked_tools; use collected evidence and finish delivery before the remaining requests run out. Do not fabricate verification when retrieval stops."
            client = scope_task_worker(shared, session, role, task_policy, control=control)
        self.backend = ExecutionBackend(
            FullNativeReAct(
                max_turns=None,
                role=role,
                system_prompt=prompt,
                max_tokens=16384,
                max_delivery_rejections=8 if role == "judge" else None,
                native_rounds=task_policy.correction_rounds if task_policy else NATIVE_ROUNDS,
                worker_session_cap=task_policy.worker_request_limit
                if task_policy and role == "worker"
                else None,
            ),
            tools,
            profile,
            client,
            self.root / "episodes",
        )


def full_initial_kernel(root):
    """Re-export the same K0 S/F/C/P content under the seven-tool execution contract."""
    from tempfile import TemporaryDirectory

    from .kernel import initial_kernel

    with TemporaryDirectory() as temporary:
        old = initial_kernel(Path(temporary) / "old", max_model_calls=None)
        return AgentKernelAssets(old.assets.assets, full_worker_profile()).export(root)
