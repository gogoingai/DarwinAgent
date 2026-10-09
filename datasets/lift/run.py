"""Coordinator-only native CLI wrapper; importing this module never calls a model."""

import argparse
import json
import os
from pathlib import Path

from .register import register, upstream

_ACTIVE_ADAPTERS = []


def build_adapter(options):
    from darwinagent.agent import AgentKernelBundle
    from darwinagent.config import Config
    from darwinagent.llm.client import LLMClient

    from .adapter import DarwinLiftAdapter
    from .data import LiftDataset
    from .kernel import initial_kernel
    from .transport import SharedTransport

    config = json.loads(Path(os.environ["DARWIN_LIFT_CONFIG"]).read_text())
    root = Path(config["run_root"]).resolve()
    budget_root = Path(config["budget_root"]).resolve()
    if budget_root == root or budget_root.is_relative_to(root):
        raise ValueError("budget_root must be independent of campaign run_root")
    # Credentials are host environment only and are never persisted in config/container.
    model, endpoint = os.environ["DARWINAGENT_MODEL"], os.environ["DARWINAGENT_BASE_URL"]
    if model != config["model"] or endpoint != config["base_url"]:
        raise ValueError("User model/endpoint differs from frozen pilot configuration")
    if config["http_cap"] != 100 or config["worker_limit"] != 8:
        raise ValueError("Pilot requires the explicit 100-attempt shared cap and 8 worker calls")
    cfg = Config(
        api_base_url=endpoint,
        api_key=os.environ["DARWINAGENT_API_KEY"],
        model_strong=model,
        model_middle=model,
        model_fast=model,
        max_retries=1,
        max_concurrency=1,
        max_http_requests=100,
        request_budget_path=budget_root / "http-attempts.json",
        work_dir=root / "transport",
        model_profiles=False,
    )
    cfg.role_tiers.update(worker="strong", judge="strong")
    shared = SharedTransport(LLMClient(cfg), budget_root / "shared", worker_limit=8)
    initial = (
        AgentKernelBundle(root / "K0") if (root / "K0").exists() else initial_kernel(root / "K0")
    )
    adapter = DarwinLiftAdapter(
        options,
        dataset=LiftDataset(config["data_root"]),
        shared=shared,
        root=root,
        initial=initial,
        trace=True,
    )

    _ACTIVE_ADAPTERS.append(adapter)
    return adapter


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--upstream", required=True)
    args, native_args = parser.parse_known_args()
    upstream(args.upstream)
    os.environ["DO_TRAJECTORY_JUDGE"] = "false"
    config = json.loads(Path(os.environ["DARWIN_LIFT_CONFIG"]).read_text())
    root = Path(config["run_root"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(arg.split("=")[0] in {"--suite", "--benchmark_dir"} for arg in native_args):
        raise ValueError("The pilot wrapper derives its q1/q5 suite; omit --suite/--benchmark_dir")
    from .data import LiftDataset

    suites = root / "native-suite"
    suites.mkdir(exist_ok=True)
    LiftDataset(config["data_root"]).write_suite(suites / "development.json")
    native_args.extend(["--benchmark_dir", str(suites), "--suite", "development.json"])
    register(build_adapter)
    import sys

    from src.cli.lift_main import main as native_main

    sys.argv = [sys.argv[0], *native_args]
    try:
        native_main()
    finally:
        from .report import report

        for adapter in _ACTIVE_ADAPTERS:
            report(adapter.root, adapter.shared)


if __name__ == "__main__":
    main()
