"""Historical evaluation record only. Generation uses oak.agents.AnswerAgent."""
from dataclasses import dataclass, field

@dataclass
class QAOutput:
    idx: int
    question: str
    answer: str = ""
    evidence: list[str] = field(default_factory=list)
    refused: bool = False
    n_steps: int = 0
    collected: list[str] = field(default_factory=list)
    trajectory: dict = field(default_factory=dict)
    status: str = "ok"
    context_fids: list[str] = field(default_factory=list)
    context_text: str = ""
    raw_outputs: list[str] = field(default_factory=list)
