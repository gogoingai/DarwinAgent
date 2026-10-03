"""The only dataset boundary. Generation inputs never contain evaluation references."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


def freeze(value):
    """Primitive, recursively read-only values; no application objects cross the boundary."""
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if type(value) is float:
        import math
        if not math.isfinite(value): raise ValueError("Nonfinite input")
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError(f"Non-primitive boundary value: {type(value).__name__}")


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


@dataclass(frozen=True)
class SourceRef:
    kind: str
    document_id: str
    location: str

    def __post_init__(self):
        if not all(type(x) is str and x.strip() for x in (self.kind, self.document_id, self.location)):
            raise ValueError("Source kind, document and location are required")

    @property
    def id(self) -> str:
        from .runtime.artifacts import digest
        return digest(self.to_dict())

    def to_dict(self):
        return {"kind": self.kind, "document_id": self.document_id, "location": self.location}


@dataclass(frozen=True)
class CorpusBlock:
    source: SourceRef
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.source, SourceRef) or not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Corpus requires a registered source and text")
        object.__setattr__(self, "metadata", freeze(self.metadata))

    def to_dict(self):
        return {"source": self.source.to_dict(), "source_id": self.source.id,
                "text": self.text, "metadata": plain(self.metadata)}


@dataclass(frozen=True)
class QuestionInput:
    id: str
    text: str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("Question requires an id and text")
        object.__setattr__(self, "parameters", freeze(self.parameters))

    def to_dict(self):
        return {"id": self.id, "text": self.text, "parameters": plain(self.parameters)}


@dataclass(frozen=True)
class CaseInput:
    id: str
    corpus: tuple[CorpusBlock, ...]
    questions: tuple[QuestionInput, ...]

    def __post_init__(self):
        object.__setattr__(self, "corpus", tuple(self.corpus))
        object.__setattr__(self, "questions", tuple(self.questions))
        if not self.id or not self.corpus or not self.questions:
            raise ValueError("Case requires corpus and questions")
        if any(c in self.id for c in ('/', '\\')) or self.id in {'.','..'}:
            raise ValueError("Invalid case identifier")
        if not all(isinstance(x, CorpusBlock) for x in self.corpus) or not all(isinstance(x, QuestionInput) for x in self.questions):
            raise ValueError("Invalid case boundary types")
        for ids in ([x.source.id for x in self.corpus], [x.id for x in self.questions]):
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate source or question id")

    def to_dict(self):
        return {"id": self.id, "corpus": [x.to_dict() for x in self.corpus],
                "questions": [x.to_dict() for x in self.questions]}


@dataclass(frozen=True)
class AnswerResult:
    question_id: str
    status: str
    answer: str
    evidence: tuple[SourceRef, ...] = ()
    error: str | None = None
    node_ids: tuple[str, ...] = ()
    raw_outputs: tuple[str, ...] = ()
    trace: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "node_ids", tuple(self.node_ids))
        object.__setattr__(self, "raw_outputs", tuple(self.raw_outputs))
        object.__setattr__(self, "trace", tuple(freeze(x) for x in self.trace))
        if self.status not in {"answered", "abstained", "execution_error"}:
            raise ValueError("Invalid answer status")
        if self.status == "answered" and (not self.answer.strip() or not self.evidence or self.error):
            raise ValueError("Answered results require evidence and no error")
        if self.status == "abstained" and (not self.answer.strip() or self.evidence or self.node_ids or self.error):
            raise ValueError("Semantic abstention cannot carry evidence or an execution error")
        if self.status == "execution_error" and (not self.error or self.answer or self.evidence or self.node_ids):
            raise ValueError("Execution errors cannot publish an answer")
        if not all(isinstance(x, SourceRef) for x in self.evidence):
            raise ValueError("Invalid evidence type")

    def to_dict(self):
        return {"question_id": self.question_id, "status": self.status, "answer": self.answer,
                "evidence": [x.to_dict() for x in self.evidence], "error": self.error,
                "node_ids": list(self.node_ids), "raw_outputs": list(self.raw_outputs), "trace": plain(self.trace)}

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "evidence": tuple(SourceRef(**x) for x in data.get("evidence", []))})


@dataclass(frozen=True)
class GraphResult:
    graph: Any
    sources: Mapping[str, CorpusBlock]
    raw_outputs: tuple[str, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class RunResult:
    case_id: str
    identity: str
    asset_version: str
    answers: tuple[AnswerResult, ...]
    graph_nodes: int
    graph_diagnostics: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self):
        return {"case_id": self.case_id, "identity": self.identity, "asset_version": self.asset_version,
                "answers": [x.to_dict() for x in self.answers], "graph_nodes": self.graph_nodes,
                "graph_diagnostics": plain(self.graph_diagnostics)}


@dataclass(frozen=True)
class EvaluationResult:
    metrics: Mapping[str, int]
    total: int
    completed: int
    generation_faults: int
    evaluation_faults: int
    diagnostics: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "metrics", freeze(self.metrics))
        object.__setattr__(self, "diagnostics", tuple(freeze(x) for x in self.diagnostics))

    def to_dict(self):
        return {"metrics": plain(self.metrics), "total": self.total, "completed": self.completed,
                "generation_faults": self.generation_faults, "evaluation_faults": self.evaluation_faults,
                "diagnostics": plain(self.diagnostics)}


@runtime_checkable
class DatasetAdapter(Protocol):
    def generation_input(self, case_id: str) -> CaseInput: ...


@runtime_checkable
class Evaluator(Protocol):
    async def evaluate(self, result: RunResult) -> EvaluationResult: ...
