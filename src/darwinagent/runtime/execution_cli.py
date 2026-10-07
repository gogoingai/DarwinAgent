"""Shared CLI spelling for explicit execution selection; no model configuration."""

from .execution import MODES, ExecutionSelection


def add_execution_arguments(parser, *, preview=True):
    parser.add_argument("--execution-mode", choices=sorted(MODES), default="continue")
    parser.add_argument(
        "--execution-stages", help="Comma-separated stages; missing prerequisites are not generated"
    )
    parser.add_argument("--execution-question-ids", help="Comma-separated question IDs")
    parser.add_argument("--execution-branch", default="main")
    parser.add_argument(
        "--strict-comparison", action="store_true", help="Require frozen comparison conditions"
    )
    if preview:
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Show execution scope offline without a model client",
        )


def selection_from_args(args):
    def values(name):
        raw = getattr(args, name, None)
        if raw is None:
            return ()
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    stages = values("execution_stages")
    return ExecutionSelection(
        mode=getattr(args, "execution_mode", "continue"),
        stages=stages if stages else ExecutionSelection().stages,
        question_ids=values("execution_question_ids"),
        branch=getattr(args, "execution_branch", "main"),
        strict=getattr(args, "strict_comparison", False),
    )


def has_scoped_flags(args):
    return bool(
        getattr(args, "execution_stages", None)
        or getattr(args, "execution_question_ids", None)
        or getattr(args, "execution_mode", "continue") != "continue"
        or getattr(args, "execution_branch", "main") != "main"
    )
