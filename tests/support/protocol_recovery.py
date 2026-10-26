from dataclasses import dataclass

from darwinagent.agent import ReActAgent


@dataclass(frozen=True)
class BoundedReAct(ReActAgent):
    max_protocol_failures: int | None = 2
