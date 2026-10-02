"""Reference-free application inputs and explicit domain injection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class SourceRef:
    kind: str
    document_id: str
    location: str

    def __post_init__(self):
        if not all((self.kind, self.document_id, self.location)):
            raise ValueError("Source kind, document and location are required")


@dataclass(frozen=True)
class CorpusBlock:
    source: SourceRef
    text: str


@dataclass(frozen=True)
class QuestionInput:
    id: str
    text: str


@dataclass(frozen=True)
class CaseInput:
    id: str
    corpus: tuple[CorpusBlock, ...]
    questions: tuple[QuestionInput, ...]


@dataclass(frozen=True)
class AnswerResult:
    question_id: str
    status: str
    answer: str
    evidence: tuple[SourceRef, ...] = ()
    error: str | None = None

    def __post_init__(self):
        if self.status not in {"answered", "abstained", "execution_error"}:
            raise ValueError("Unknown answer status")
        if self.status == "answered" and (not self.answer.strip() or not self.evidence):
            raise ValueError("An answer requires text and source references")
        if self.status == "abstained" and (self.evidence or self.error):
            raise ValueError("An abstention cannot carry answer evidence or an error")
        if self.status == "execution_error" and (not self.error or self.answer or self.evidence):
            raise ValueError("Execution errors require an error and cannot masquerade as answers")


class DatasetAdapter(Protocol):
    def generation_input(self, case_id: str) -> CaseInput: ...


@dataclass(frozen=True)
class DomainSpec:
    """Callbacks receive portable inputs; reference answers stay in the adapter."""
    name: str
    builder: Callable[..., Awaitable[Any]]
    answerer: Callable[..., Awaitable[AnswerResult]]
    identity: Callable[[], dict]
