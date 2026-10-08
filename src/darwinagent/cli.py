"""Installed command-line interface for a bounded, inspectable example."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from .config import Config


def _parser():
    parser = argparse.ArgumentParser(prog="darwinagent", description="Evolution for the Agent Era")
    parser.add_argument("--version", action="version", version="darwinagent 0.1.2")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser(
        "demo", help="Run a tiny maintenance task through Pipeline and Wiki optimization"
    )
    demo.add_argument("--mode", choices=("replay", "live"), default="replay")
    demo.add_argument("--rounds", type=int, default=2)
    demo.add_argument("--output", type=Path, default=Path("runs/demo"))
    demo.add_argument("--resume", action="store_true")
    demo.add_argument("--preview", action="store_true", help="Show scoped execution plan offline")
    demo.add_argument("--execution", type=Path, help="ExecutionSelection JSON")
    from .runtime.execution_cli import add_execution_arguments

    add_execution_arguments(demo, preview=False)
    demo.add_argument(
        "--max-requests",
        type=int,
        default=40,
        help="Maximum actual HTTP attempts, including retries",
    )
    demo.add_argument("--timeout", type=float, default=1800, help="Whole run time limit in seconds")
    demo.add_argument(
        "--model-profiles",
        action="store_true",
        help="Opt in to provider-specific request parameters",
    )
    doctor = sub.add_parser(
        "doctor", help="Check installation offline; explicitly opt in to an endpoint probe"
    )
    doctor.add_argument("--check-model", action="store_true")
    doctor.add_argument(
        "--output",
        type=Path,
        default=Path("runs"),
        help="Artifact directory to check for write access",
    )
    workspace = sub.add_parser("workspace", help="Inspect and operate durable progress offline")
    workspace.add_argument("--root", type=Path, required=True, help="Experiment directory")
    commands = workspace.add_subparsers(dest="operation", required=True)
    commands.add_parser("status", help="Show branches, attempts and unresolved requests")
    intervene = commands.add_parser("intervene", help="Register a human change from JSON")
    intervene.add_argument("change", type=Path)
    intervene.add_argument("--branch", default="main")
    intervene.add_argument("--expected-revision", type=int)
    fork = commands.add_parser("fork", help="Reference existing progress on a new branch")
    fork.add_argument("name")
    fork.add_argument("--parent", default="main")
    export = commands.add_parser("export", help="Export immutable evidence and progress")
    export.add_argument("destination", type=Path)
    export.add_argument("--legacy-source", type=Path)
    export.add_argument("--path-map", type=Path)
    imported = commands.add_parser(
        "import", help="Validate and import without deleting target results"
    )
    imported.add_argument("bundle", type=Path)
    request = commands.add_parser("request", help="Recover or resolve unknown requests")
    request_commands = request.add_subparsers(dest="request_operation", required=True)
    recover = request_commands.add_parser("recover")
    recover.add_argument(
        "--executor-stopped",
        action="store_true",
        required=True,
        help="Confirm the old executor has stopped before recovery",
    )
    resolve = request_commands.add_parser("resolve")
    resolve.add_argument("request_id")
    resolve.add_argument("--action", choices=("response", "new-attempt", "abandon"), required=True)
    resolve.add_argument("--response", type=Path)
    maintenance = commands.add_parser(
        "wiki-maintenance-retry", help="Register an explicit attribution retry without model calls"
    )
    maintenance.add_argument("event_id")
    maintenance.add_argument("--reason", required=True)
    query = commands.add_parser(
        "wiki-query", help="Query training evidence or explicitly regroup it"
    )
    query.add_argument("question")
    query.add_argument("--scope", type=Path, help="JSON scope object")
    query.add_argument("--view", choices=("raw", "summary", "regroup"), default="raw")
    query.add_argument("--cursor")
    query.add_argument("--max-chars", type=int, default=8000)
    query.add_argument("--max-requests", type=int, default=80)
    query.add_argument("--timeout", type=float, default=900)
    query.add_argument(
        "--allow-model", action="store_true", help="Use the explicitly configured model"
    )
    preview = commands.add_parser("preview", help="Resolve an execution scope without model calls")
    preview.add_argument("cases", type=Path, help="JSON list of case id and question objects")
    preview.add_argument("--selection", type=Path, help="ExecutionSelection JSON")
    return parser


async def _doctor(check_model, output):
    from .demo import TASK_ROOT
    from .kernel import TaskSpec
    from .kernel.registration import load_assets

    task = TaskSpec.load(TASK_ROOT / "task.yaml")
    assets = load_assets(TASK_ROOT)
    if not task.name or not assets.assets:
        raise ValueError("Installed demo resources are incomplete; reinstall darwinagent")
    print("Installation OK: darwinagent 0.1.2; packaged task and assets available.")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doctor-write-", dir=output) as probe:
        (Path(probe) / "probe").write_text("ok")
    print("Output directory OK: writable.")
    readiness = Config.from_env()
    try:
        readiness.validate_model()
        print("Live model configuration: ready (no network check performed).")
    except ValueError:
        print(
            "Live model configuration: incomplete; replay is ready. Set DARWINAGENT_BASE_URL, DARWINAGENT_MODEL, DARWINAGENT_API_KEY for live mode."
        )
    if check_model:
        from .llm.client import LLMClient

        with tempfile.TemporaryDirectory(prefix="darwinagent-doctor-") as tmp:
            cfg = Config.from_env(work_dir=tmp)
            cfg.validate_model()
            cfg.max_retries = 1
            cfg.max_http_requests = 1
            cfg.request_timeout_s = 30
            cfg.deadline_monotonic = time.monotonic() + 30
            async with asyncio.timeout(30):
                async with LLMClient(cfg) as client:
                    result = await client.chat(
                        role="answer",
                        messages=[{"role": "user", "content": "Reply OK."}],
                        max_tokens=16,
                        use_cache=False,
                        namespace="doctor",
                    )
                    if not result.content.strip():
                        raise ValueError("Endpoint returned an empty model response")
            print("Model endpoint OK: one authenticated Chat Completions response received.")
    return 0


async def _demo(args):
    from .demo import MaintenanceAdapter, run_demo
    from .runtime.execution import ExecutionSelection
    from .runtime.execution_cli import selection_from_args

    selection = (
        ExecutionSelection(**json.loads(args.execution.read_text()))
        if args.execution
        else selection_from_args(args)
    )

    if args.preview:
        from .experiments.control import preview

        result = preview(
            args.output, [MaintenanceAdapter().generation_input("maintenance-demo")], selection
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return 0
    if args.rounds < 0 or args.rounds > 10:
        raise ValueError("--rounds must be between 0 and 10")
    if args.max_requests < 1 or args.timeout <= 0:
        raise ValueError("--max-requests and --timeout must be positive")
    cfg = Config.from_env(work_dir=args.output)
    cfg.model_profiles = (
        args.model_profiles or os.environ.get("DARWINAGENT_MODEL_PROFILES", "").lower() == "true"
    )
    if cfg.model_profiles:
        cfg.thinking_disabled_roles.update(cfg.role_tiers)
    cfg.max_retries = 2
    cfg.max_http_requests = args.max_requests
    cfg.request_budget_path = args.output.resolve() / "http_attempts.json"
    cfg.deadline_monotonic = time.monotonic() + args.timeout
    if args.mode == "live":
        cfg.validate_model()
    if args.mode == "replay":
        print("Replay demonstrates mechanisms, not measured model performance. No network calls.")
    async with asyncio.timeout(args.timeout):
        result = await run_demo(
            args.output,
            mode=args.mode,
            rounds=args.rounds,
            resume=args.resume,
            config=cfg,
            execution=selection,
        )
    attempts = (
        json.loads(cfg.request_budget_path.read_text()).get("attempts", 0)
        if cfg.request_budget_path.exists()
        else 0
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "status": result["status"],
                "output": str(args.output.resolve()),
                "decisions": [
                    {"round": index, "accepted": row["accepted"], "reasons": row["reasons"]}
                    for index, row in enumerate(result["rounds"], 1)
                ],
                "http_attempts": attempts,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "complete" else 1


async def _workspace(args):
    from .runtime.workspace import Workspace

    workspace = Workspace(args.root / "workspace")
    operation = args.operation
    if operation == "status":
        with workspace._db() as db:
            result = {
                "branches": [dict(r) for r in db.execute("SELECT * FROM branches ORDER BY name")],
                "attempts": [
                    dict(r) for r in db.execute("SELECT id,task,stage,status FROM attempts")
                ],
                "unresolved_requests": [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,status FROM requests WHERE status IN ('prepared','submitted','unknown','failed')"
                    )
                ],
                "objects": db.execute("SELECT count(*) FROM objects").fetchone()[0],
                "real_model_smoke": "pending explicit model selection and execution",
            }
    elif operation == "intervene":
        from .experiments.control import intervene

        result = intervene(
            args.root,
            json.loads(args.change.read_text()),
            branch=args.branch,
            expected_revision=args.expected_revision,
        )
    elif operation == "fork":
        result = workspace.create_branch(args.name, parent=args.parent)
    elif operation in ("export", "import"):
        from .runtime.migration import export_legacy_run, export_run, import_bundle, import_run

        if operation == "import":
            result = (
                import_run(args.bundle, args.root)
                if (args.bundle / "run-manifest.json").exists()
                else import_bundle(args.bundle, workspace)
            )
        elif args.legacy_source:
            result = export_legacy_run(
                args.legacy_source,
                args.destination,
                path_map=json.loads(args.path_map.read_text()) if args.path_map else None,
            )
        else:
            result = export_run(args.root, args.destination)
    elif operation == "request":
        if args.request_operation == "recover":
            result = {"recovered": workspace.recover_requests()}
        elif args.action == "response":
            if args.response is None:
                raise ValueError("--response is required to recover a saved response")
            result = {
                "response_ref": workspace.record_response(
                    args.request_id,
                    json.loads(args.response.read_text()),
                    metadata={"source": "human-recovery"},
                )
            }
        elif args.action == "new-attempt":
            new = workspace.retry_request(args.request_id)
            result = {"request_id": new, "status": "prepared", "possible_duplicate_cost": True}
        else:
            workspace.set_request_status(args.request_id, "abandoned")
            result = {"request_id": args.request_id, "status": "abandoned"}
    elif operation == "wiki-maintenance-retry":
        from .config import RunConfig
        from .experiments.wiki import WikiMaintainer

        state = json.loads((args.root / "optimization" / "state.json").read_text())
        wiki = WikiMaintainer(
            args.root, state["identity"], lambda _: None, RunConfig(), limit=state["limit"]
        )
        result = wiki.retry_maintenance(args.event_id, args.reason)
    elif operation == "preview":
        from types import SimpleNamespace

        from .contracts import QuestionInput
        from .experiments.control import preview
        from .runtime.execution import ExecutionSelection

        rows = json.loads(args.cases.read_text())
        cases = [
            SimpleNamespace(
                id=row["id"], questions=tuple(QuestionInput(**q) for q in row["questions"])
            )
            for row in rows
        ]
        selection = (
            ExecutionSelection(**json.loads(args.selection.read_text())) if args.selection else None
        )
        result = preview(args.root, cases, selection).to_dict()
    else:
        from .experiments.wiki_service import WikiQuery, WikiService

        query = WikiQuery(
            args.question,
            scope=json.loads(args.scope.read_text()) if args.scope else {},
            view=args.view,
            cursor=args.cursor,
            max_chars=args.max_chars,
        )
        if args.allow_model:
            if args.view != "regroup":
                raise ValueError("--allow-model applies only to --view regroup")
            from .config import RunConfig
            from .llm.client import LLMClient

            cfg = Config.from_env(work_dir=args.root / "wiki-connection")
            cfg.validate_model()
            if args.max_requests < 1 or args.timeout <= 0:
                raise ValueError("--max-requests and --timeout must be positive")
            cfg.max_http_requests = args.max_requests
            cfg.request_budget_path = args.root.resolve() / "wiki-http-attempts.json"
            cfg.deadline_monotonic = time.monotonic() + args.timeout
            async with asyncio.timeout(args.timeout):
                async with LLMClient(cfg) as client:
                    result = (
                        await WikiService(args.root, lambda: client, RunConfig()).query(query)
                    ).to_dict()
        else:
            result = (await WikiService(args.root).query(query)).to_dict()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env")
    try:
        if args.command == "workspace":
            return asyncio.run(_workspace(args))
        return asyncio.run(
            _doctor(args.check_model, args.output) if args.command == "doctor" else _demo(args)
        )
    except (Exception, KeyboardInterrupt) as exc:
        # Keep provider responses/credentials out of console output.
        message = str(exc) if isinstance(exc, (ValueError, FileExistsError)) else type(exc).__name__
        for name in ("DARWINAGENT_API_KEY",):
            key = os.environ.get(name, "")
            if key:
                message = message.replace(key, "[redacted]")
        print(
            f"Error: {message}. Inspect the output artifacts for details; verify configuration and limits.",
            file=__import__("sys").stderr,
        )
        return 1
