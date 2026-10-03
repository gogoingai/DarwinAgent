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


FACT_POLARITIES = frozenset({"positive", "negative", "uncertain"})
FACT_MODALITIES = frozenset({"statement", "plan", "hypothesis", "uncertain"})
FACT_PRECISIONS = frozenset({"day", "week", "month", "year", "hour", "unknown"})
FACT_VALUE_DTYPES = frozenset({"string", "int", "float", "bool", "date"})


def _fold_text(value: str) -> str:
    return " ".join(str(value).split())


def _iso_date_or_empty(value: str) -> str:
    if not value:
        return ""
    from datetime import date
    return date.fromisoformat(str(value)).isoformat()


@dataclass(frozen=True)
class EntityRef:
    """A typed reference to one entity; normalization folds whitespace only."""

    cls: str
    name: str

    def __post_init__(self):
        if not all(type(x) is str and x.strip() for x in (self.cls, self.name)):
            raise ValueError("Entity reference requires a class and a name")
        object.__setattr__(self, "cls", _fold_text(self.cls))
        object.__setattr__(self, "name", _fold_text(self.name))

    def to_dict(self):
        return {"class": self.cls, "name": self.name}


@dataclass(frozen=True)
class FactValue:
    """A typed scalar carried as canonical text; F converts when computing."""

    dtype: str
    value: str

    def __post_init__(self):
        if self.dtype not in FACT_VALUE_DTYPES or type(self.value) is not str or not self.value.strip():
            raise ValueError("Fact value requires a declared dtype and canonical text")

    def to_dict(self):
        return {"dtype": self.dtype, "value": self.value}


@dataclass(frozen=True)
class FactTime:
    raw: str
    precision: str = "unknown"
    start: str = ""
    end: str = ""
    anchor_source_id: str = ""

    def __post_init__(self):
        if type(self.raw) is not str or not self.raw.strip():
            raise ValueError("Fact time requires the original expression")
        if self.precision not in FACT_PRECISIONS:
            raise ValueError("Unknown time precision")
        object.__setattr__(self, "start", _iso_date_or_empty(self.start))
        object.__setattr__(self, "end", _iso_date_or_empty(self.end))
        if not all(type(x) is str for x in (self.anchor_source_id,)):
            raise ValueError("Invalid time anchor")

    def to_dict(self):
        return {"raw": self.raw, "precision": self.precision, "start": self.start,
                "end": self.end, "anchor_source_id": self.anchor_source_id}


@dataclass(frozen=True)
class FactEvidence:
    source_id: str
    quote: str
    start: int
    end: int

    def __post_init__(self):
        if not all(type(x) is str and x.strip() for x in (self.source_id, self.quote)):
            raise ValueError("Evidence requires a registered source and a verbatim quote")
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end:
            raise ValueError("Evidence offsets must bracket the quote")

    def to_dict(self):
        return {"source_id": self.source_id, "quote": self.quote, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class AtomicFact:
    """One judging proposition with its provenance; the id is derived, never supplied."""

    id: str
    text: str
    subject: EntityRef
    predicate: str
    object_entity: EntityRef | None = None
    object_value: FactValue | None = None
    polarity: str = "positive"
    modality: str = "statement"
    time: FactTime = field(default_factory=lambda: FactTime("未注明"))
    evidence: tuple[FactEvidence, ...] = ()

    def __post_init__(self):
        if not isinstance(self.subject, EntityRef):
            raise ValueError("Fact requires a typed subject")
        if not all(type(x) is str and x.strip() for x in (self.text, self.predicate)):
            raise ValueError("Fact requires complete proposition text and a predicate")
        if self.polarity not in FACT_POLARITIES or self.modality not in FACT_MODALITIES:
            raise ValueError("Unknown polarity or modality")
        if not isinstance(self.time, FactTime):
            raise ValueError("Fact requires a time record")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.evidence or not all(isinstance(x, FactEvidence) for x in self.evidence):
            raise ValueError("Facts require at least one located evidence span")
        if self.object_entity is not None and self.object_value is not None:
            raise ValueError("A fact object is either an entity or a value, not both")
        if self.object_entity is not None and not isinstance(self.object_entity, EntityRef):
            raise ValueError("Invalid object entity")
        if self.object_value is not None and not isinstance(self.object_value, FactValue):
            raise ValueError("Invalid object value")
        if type(self.id) is not str or not self.id:
            raise ValueError("Fact identity is framework-derived")

    def content(self):
        """Normalized body without the identity; the id material."""
        return {"text": self.text, "subject": self.subject.to_dict(), "predicate": self.predicate,
                "object_entity": self.object_entity.to_dict() if self.object_entity else None,
                "object_value": self.object_value.to_dict() if self.object_value else None,
                "polarity": self.polarity, "modality": self.modality, "time": self.time.to_dict(),
                "evidence": [e.to_dict() for e in self.evidence]}

    def derived_id(self):
        from .runtime.artifacts import digest
        return digest({"sources": sorted({e.source_id for e in self.evidence}), "fact": self.content()})

    @classmethod
    def create(cls, **fields):
        probe = cls("0", fields["text"], fields["subject"], fields["predicate"],
                    fields.get("object_entity"), fields.get("object_value"),
                    fields.get("polarity", "positive"), fields.get("modality", "statement"),
                    fields.get("time") or FactTime("未注明"), tuple(fields.get("evidence") or ()))
        return cls(probe.derived_id(), probe.text, probe.subject, probe.predicate,
                   probe.object_entity, probe.object_value, probe.polarity, probe.modality,
                   probe.time, probe.evidence)

    def to_dict(self):
        return {"id": self.id, **self.content()}

    @classmethod
    def from_dict(cls, data):
        def ref(x):
            return EntityRef(x["class"], x["name"]) if x else None
        fact = cls(data["id"], data["text"], ref(data["subject"]), data["predicate"],
                   ref(data["object_entity"]),
                   FactValue(**data["object_value"]) if data["object_value"] else None,
                   data["polarity"], data["modality"], FactTime(**data["time"]),
                   tuple(FactEvidence(**e) for e in data["evidence"]))
        if fact.id != fact.derived_id():
            raise ValueError("Fact identity does not match its content")
        return fact


@dataclass(frozen=True)
class MemoryResult:
    """Validated atomic facts with raw responses and per-batch diagnostics."""

    corpus: Mapping[str, CorpusBlock]
    facts: tuple[AtomicFact, ...]
    raw_outputs: tuple[str, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "corpus", MappingProxyType(dict(self.corpus)))
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "raw_outputs", tuple(self.raw_outputs))
        object.__setattr__(self, "diagnostics", tuple(freeze(x) for x in self.diagnostics))
        if not all(isinstance(x, AtomicFact) for x in self.facts):
            raise ValueError("Memory holds atomic facts only")

    @property
    def fingerprint(self):
        from .runtime.artifacts import digest
        return digest([f.to_dict() for f in sorted(self.facts, key=lambda x: x.id)])

    def to_dict(self):
        return {"facts": [f.to_dict() for f in self.facts], "raw_outputs": list(self.raw_outputs),
                "diagnostics": plain(self.diagnostics)}

    @classmethod
    def from_dict(cls, data, corpus):
        return cls(corpus, tuple(AtomicFact.from_dict(x) for x in data["facts"]),
                   tuple(data.get("raw_outputs", ())), tuple(data.get("diagnostics", ())))


@dataclass(frozen=True)
class GraphResult:
    graph: Any
    sources: Mapping[str, CorpusBlock]
    raw_outputs: tuple[str, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    # Frozen vector index attached to the graph (agentic rounds): semantic_search resolves
    # hits back to graph rows so read lineage and evidence attribution stay row-level.
    vector: Any = None


@dataclass(frozen=True)
class RunResult:
    case_id: str
    identity: str
    asset_version: str
    answers: tuple[AnswerResult, ...]
    graph_nodes: int
    graph_diagnostics: tuple[Mapping[str, Any], ...] = ()
    memory_count: int = 0
    memory_fingerprint: str = ""
    graph_fingerprint: str = ""

    def to_dict(self):
        return {"case_id": self.case_id, "identity": self.identity, "asset_version": self.asset_version,
                "answers": [x.to_dict() for x in self.answers], "graph_nodes": self.graph_nodes,
                "graph_diagnostics": plain(self.graph_diagnostics), "memory_count": self.memory_count,
                "memory_fingerprint": self.memory_fingerprint, "graph_fingerprint": self.graph_fingerprint}


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
