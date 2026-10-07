"""DarwinAgent: Evolution for the Agent Era.

Importing the package does not read credentials, create clients, or touch the filesystem.
"""

from .config import Config, RunConfig
from .contracts import (
    CaseInput,
    CorpusBlock,
    QuestionInput,
    SourceRef,
    AnswerResult,
    RunResult,
    EvaluationResult,
    DatasetAdapter,
    Evaluator,
)
from .engine import Pipeline
from .kernel import TaskSpec, KernelBundle
from .experiments import (
    ExperimentRunner,
    AdoptionPolicy,
    CampaignController,
    ExperimentSpec,
    SelectionPolicy,
)

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
