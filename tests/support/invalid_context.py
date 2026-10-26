from dataclasses import dataclass, field

from darwinagent.agent import ReActAgent

try:
    from datasets.lift.agents import NativeReAct
except ModuleNotFoundError as exc:
    if exc.name not in {"src", "src.lift"}:
        raise
    NativeReAct = None


@dataclass(frozen=True)
class CompactingReAct(ReActAgent):
    invalid_reply_context_chars: int | None = field(default=2048, kw_only=True)


if NativeReAct is None:
    CompactingNativeReAct = None
else:

    @dataclass(frozen=True)
    class CompactingNativeReAct(NativeReAct):
        invalid_reply_context_chars: int | None = field(default=2048, kw_only=True)
