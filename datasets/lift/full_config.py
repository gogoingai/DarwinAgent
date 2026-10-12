"""Explicit-execution full preset; credential environment access occurs only here."""

import os
from pathlib import Path

from darwinagent.agent import AgentKernelBundle
from darwinagent.config import Config
from darwinagent.llm.client import LLMClient
from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.identity import snapshot_files

from .full import FullLocalRunner
from .full_data import FullLiftDataset


def build_runner(config, run_id, *, shared=None):
    from src.lift.eval import task_exec

    from .full_agents import full_initial_kernel

    for key, expected in dict(
        model="MiniMax-M3.1-Flash-Preview",
        base_url="https://api.minimax.cn/v1",
        temperature=0,
        correction_rounds=30,
        max_tokens=16384,
        worker_limit=None,
        http_cap=None,
        provider_request_policy="minimax-adaptive-v1",
    ).items():
        if config.get(key) != expected:
            raise ValueError("Full preset requires explicit authorized " + key)
    if not config.get("unbounded_model_calls"):
        raise ValueError("Explicit unbounded_model_calls opt-in is required")
    if shared is None:
        from .full_transport import FullSharedTransport

        if (
            os.environ["DARWINAGENT_MODEL"] != config["model"]
            or os.environ["DARWINAGENT_BASE_URL"] != config["base_url"]
        ):
            raise ValueError("Environment model/endpoint differs from frozen config")
        budget = Path(config["budget_root"]).resolve()
        cfg = Config(
            api_base_url=config["base_url"],
            api_key=os.environ["DARWINAGENT_API_KEY"],
            model_strong=config["model"],
            model_middle=config["model"],
            model_fast=config["model"],
            max_retries=1,
            max_concurrency=1,
            max_http_requests=None,
            request_budget_path=budget / "http-attempts.json",
            work_dir=Path(config["run_root"]) / "transport",
            model_profiles=False,
            provider_request_policy="minimax-adaptive-v1",
            thinking_type="adaptive",
            reasoning_effort="max",
        )
        cfg.limits = {}
        cfg.role_tiers.update(worker="strong", judge="strong", trajectory_judge="strong")
        source = snapshot_files(
            [
                Path(__file__).parent,
                Path(__file__).parents[2] / "src/darwinagent",
                Path(task_exec.__file__).parents[2],
            ]
        )
        shared = FullSharedTransport(
            LLMClient(cfg),
            budget / "shared",
            worker_limit=None,
            enroll_role=True,
            experiment_identity=dict(
                run_id=run_id, source_sha256=digest(source), config_sha256=digest(config)
            ),
        )
    root = Path(config["run_root"]).resolve()
    initial = (
        AgentKernelBundle(root / "K0")
        if (root / "K0").exists()
        else full_initial_kernel(root / "K0")
    )
    return FullLocalRunner(
        dataset=FullLiftDataset(config["data_root"]),
        shared=shared,
        root=root,
        initial=initial,
        run_id=run_id,
        rounds=config.get("rounds", 1),
        scenes=config.get("scenes"),
    )
