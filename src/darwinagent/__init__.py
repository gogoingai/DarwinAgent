"""DarwinAgent: Evolution for the Agent Era.

Importing the package does not read credentials, create clients, or touch the filesystem.
"""

from .config import Config, RunConfig
from .contracts import (
    AnswerResult,
    CaseInput,
    CorpusBlock,
    DatasetAdapter,
    EvaluationResult,
    Evaluator,
    QuestionInput,
    RunResult,
    SourceRef,
)
from .engine import Pipeline
from .experiments import (
    AdoptionPolicy,
    CampaignController,
    ExperimentRunner,
    ExperimentSpec,
    SelectionPolicy,
)
from .kernel import KernelBundle, TaskSpec

__version__ = "0.1.0"
__all__ = [
    "Config",
    "RunConfig",
    "CaseInput",
    "CorpusBlock",
    "QuestionInput",
    "SourceRef",
    "AnswerResult",
    "RunResult",
    "EvaluationResult",
    "DatasetAdapter",
    "Evaluator",
    "Pipeline",
    "TaskSpec",
    "KernelBundle",
    "ExperimentRunner",
    "AdoptionPolicy",
    "CampaignController",
    "ExperimentSpec",
    "SelectionPolicy",
]
