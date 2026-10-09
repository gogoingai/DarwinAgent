"""Task-independent model connections, execution limits, and artifact paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent

# Framework roles; application-specific roles are injected by adapters.
MODEL_ROLES: dict[str, str] = {
    # Generic environment configuration gives every tier the same model.
    "extraction": "middle",
    "tools": "middle",
    "answer": "strong",
    "review": "strong",
    "bootstrap": "strong",
    "proposal": "strong",
    "wiki_maintainer": "strong",
    "schema": "strong",  # P1 需求分析 / P2 模式草拟
    "func_gen": "strong",  # P4 函数生成（含能力规划）
    "judicator": "strong",  # P6 评判器
    "kg": "fast",  # P3 建图抽取
    "react": "fast",  # P5 ReAct 执行
    "slots": "fast",  # extract_runtime_slots 槽位提取
    "plan_repair": "fast",  # 计划格式修复 / salvage
}


@dataclass(frozen=True)
class RunConfig:
    """Engineering policy, frozen before a baseline; never an optimization asset."""

    concurrency: int = 4
    extraction_batch_chars: int = 400
    extraction_bisect_depth: int = 2
    extraction_max_tokens: int = 8000
    extraction_temperature: float = 0.0
    protocol_attempts: int = 3
    answer_attempts: int = 3
    tool_steps: int = 5
    calls_per_question: int = 32
    max_tokens: int = 4096
    wiki_max_tokens: int | None = None
    temperature: float = 0.2
    function_steps: int = 30000
    function_timeout_s: float = 2.0
    result_bytes: int = 180000
    # 双臂检索模式：'agentic'＝tools 步循环里模型自主选图/向量工具；'vector_once'＝V0
    # 纯向量一次检索基线（确定性 top-K 后冻结证据，跳过工具循环，拒答审计不读全图）。
    retrieval_mode: str = "agentic"
    vector_k: int = 30
    extraction_role: str = "extraction"
    tools_role: str = "tools"
    answer_role: str = "answer"
    review_role: str = "review"
    bootstrap_role: str = "bootstrap"
    proposal_role: str = "proposal"

    def __post_init__(self):
        for key in (
            "concurrency",
            "extraction_batch_chars",
            "extraction_bisect_depth",
            "extraction_max_tokens",
            "protocol_attempts",
            "answer_attempts",
            "tool_steps",
            "calls_per_question",
            "max_tokens",
            "function_steps",
            "result_bytes",
            "vector_k",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.wiki_max_tokens is not None and (
            type(self.wiki_max_tokens) is not int or self.wiki_max_tokens <= 0
        ):
            raise ValueError("wiki_max_tokens must be a positive integer or None")
        if self.retrieval_mode not in ("agentic", "vector_once"):
            raise ValueError("retrieval_mode must be 'agentic' or 'vector_once'")
        if (
            self.function_timeout_s <= 0
            or not 0 <= self.temperature <= 2
            or not 0 <= self.extraction_temperature <= 2
        ):
            raise ValueError("Invalid execution limits")

    def to_dict(self):
        from dataclasses import asdict

        return asdict(self)


@dataclass
class Config:
    # A single OpenAI-compatible endpoint by default; tiers are optional overrides.
    api_base_url: str = ""
    api_key: str = ""
    model_strong: str = ""
    # Optional fast-tier model and endpoint override.
    fast_base_url: str = ""
    fast_api_key: str = ""
    model_fast: str = ""
    # Optional middle-tier model and endpoint override.
    model_middle: str = ""
    middle_base_url: str = ""
    middle_api_key: str = ""

    # Endpoint concurrency and bounded transport attempts.
    max_concurrency: int = 4
    # Independent pool size when an optional fast endpoint is used.
    fast_max_concurrency: int = 6
    max_retries: int = 10

    # 框架级路径（任务通常覆盖 work_dir 以隔离产物）
    work_dir: Path = field(default_factory=lambda: Path.cwd() / "runs")

    # Legacy default limits; applications register explicit namespace scopes.
    limits: dict = field(
        default_factory=lambda: {
            "build_round_calls": 600,  # 单轮 LLM 调用上限
            "inference_calls_per_q": 60,  # 单测试题上限
        }
    )

    role_tiers: dict[str, str] = field(default_factory=lambda: dict(MODEL_ROLES))
    namespace_limits: dict[str, int] = field(default_factory=dict)
    thinking_disabled_roles: set[str] = field(default_factory=set)
    empty_response_passthrough_roles: set[str] = field(default_factory=lambda: {"judicator"})
    # 思考深度的全局旋钮（effort 型模型生效；条目缺省见 darwinagent/llm/registry.py）
    reasoning_effort: str = ""
    thinking_type: str = ""
    # Provider-specific routing is an explicit compatibility preset.
    model_profiles: bool = False
    request_timeout_s: float = 120.0
    max_http_requests: int | None = None
    deadline_monotonic: float | None = None
    request_budget_path: Path | None = None

    @classmethod
    def from_env(cls, *, work_dir=None):
        import os

        model = os.environ.get("DARWINAGENT_MODEL", "")
        return cls(
            api_base_url=os.environ.get("DARWINAGENT_BASE_URL", ""),
            api_key=os.environ.get("DARWINAGENT_API_KEY", ""),
            model_strong=model,
            model_middle=model,
            model_fast=model,
            reasoning_effort=os.environ.get("DARWINAGENT_REASONING_EFFORT", ""),
            thinking_type=os.environ.get("DARWINAGENT_THINKING_TYPE", ""),
            max_concurrency=int(os.environ.get("DARWINAGENT_MAX_CONCURRENCY", "4")),
            work_dir=Path(work_dir) if work_dir is not None else Path.cwd() / "runs",
        )

    def validate_model(self):
        from urllib.parse import urlparse

        url = urlparse(self.api_base_url)
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError("Set DARWINAGENT_BASE_URL to your OpenAI-compatible API base URL")
        if not self.model_strong:
            raise ValueError("Set DARWINAGENT_MODEL to an explicit model name")
        if not self.api_key:
            raise ValueError("Set DARWINAGENT_API_KEY for your endpoint")

    def tier_for(self, role: str) -> str:
        tier = self.role_tiers.get(role)
        if tier not in ("strong", "middle", "fast"):
            raise ValueError(f"unknown LLM role or tier: {role}")
        return tier

    def model_for(self, role: str) -> str:
        tier = self.tier_for(role)
        if tier is None:
            raise ValueError(f"unknown LLM role: {role}")
        if tier == "strong":
            return self.model_strong
        if tier == "middle":
            return self.model_middle or self.model_strong
        return self.model_fast or self.model_strong

    # ---- 框架派生路径 ----
    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache" / "llm"

    @property
    def ledger_path(self) -> Path:
        return self.work_dir / "cost_ledger.jsonl"
