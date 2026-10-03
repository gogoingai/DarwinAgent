"""OaK 框架核心配置：模型路由、并发、限额、缓存路径（任务无关）。

各任务（datasets/travelplanner、datasets/locomo）在此 Config 之上叠自己的
任务参数与产物目录——任务专属字段不在框架层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Framework roles; application-specific roles are injected by adapters.
MODEL_ROLES: dict[str, str] = {
    "extraction": "fast", "tools": "fast", "answer": "strong", "review": "strong",
    "bootstrap": "strong", "proposal": "strong",
    "schema": "strong",      # P1 需求分析 / P2 模式草拟
    "func_gen": "strong",    # P4 函数生成（含能力规划）
    "judicator": "strong",   # P6 评判器
    "kg": "fast",            # P3 建图抽取
    "react": "fast",         # P5 ReAct 执行
    "slots": "fast",         # extract_runtime_slots 槽位提取
    "plan_repair": "fast",   # 计划格式修复 / salvage

}


@dataclass(frozen=True)
class RunConfig:
    """Engineering policy, frozen before a baseline; never an optimization asset."""
    concurrency: int = 4
    extraction_batch_chars: int = 2000
    extraction_bisect_depth: int = 2
    extraction_max_tokens: int = 8000
    protocol_attempts: int = 3
    answer_attempts: int = 3
    tool_steps: int = 5
    calls_per_question: int = 32
    max_tokens: int = 4096
    temperature: float = 0.2
    function_steps: int = 30000
    function_timeout_s: float = 2.0
    result_bytes: int = 180000
    extraction_role: str = "extraction"
    tools_role: str = "tools"
    answer_role: str = "answer"
    review_role: str = "review"
    bootstrap_role: str = "bootstrap"
    proposal_role: str = "proposal"

    def __post_init__(self):
        for key in ("concurrency", "extraction_batch_chars", "extraction_bisect_depth", "extraction_max_tokens",
                    "protocol_attempts", "answer_attempts",
                    "tool_steps", "calls_per_question", "max_tokens", "function_steps", "result_bytes"):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.function_timeout_s <= 0 or not 0 <= self.temperature <= 2:
            raise ValueError("Invalid execution limits")

    def to_dict(self):
        from dataclasses import asdict
        return asdict(self)


@dataclass
class Config:
    # API（strong 档走智谱；fast 档可切第三方网关省额度）
    api_base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4"
    api_key: str = ""
    model_strong: str = "glm-5.3"
    # fast 档默认同站；.env 可覆盖 FAST_API_BASE/FAST_MODEL/FAST_API_KEY
    fast_base_url: str = ""
    fast_api_key: str = ""
    model_fast: str = "glm-5.3-flash"

    # 并发与重试（账户有限流：8 并发触发 429，降到 4）
    max_concurrency: int = 4
    # fast 档独立池大小（仅当 fast 异站分池时生效；commandcode 独立限流池，可略高）
    fast_max_concurrency: int = 6
    max_retries: int = 5

    # 框架级路径（任务通常覆盖 work_dir 以隔离产物）
    work_dir: Path = field(default_factory=lambda: Path.cwd() / "runs")

    # Legacy default limits; applications register explicit namespace scopes.
    limits: dict = field(default_factory=lambda: {
        "build_round_calls": 600,      # 单轮 LLM 调用上限
        "inference_calls_per_q": 60,   # 单测试题上限
    })

    role_tiers: dict[str, str] = field(default_factory=lambda: dict(MODEL_ROLES))
    namespace_limits: dict[str, int] = field(default_factory=dict)
    thinking_disabled_roles: set[str] = field(default_factory=set)
    empty_response_passthrough_roles: set[str] = field(default_factory=lambda: {"judicator"})
    reasoning_buffer: int = 3072
    # 外部(非智谱)端点多为长思考模型且无思考开关：请求侧给足推理余量，正文才拿得到预算。
    # 实测 MiniMax-M3.1-Flash 抽取批推理 ~21k tokens+正文 ~5k（2026-10-03），8192 会空正文。
    external_reasoning_buffer: int = 32000

    def tier_for(self, role: str) -> str:
        tier = self.role_tiers.get(role)
        if tier not in ("strong", "fast"):
            raise ValueError(f"unknown LLM role or tier: {role}")
        return tier

    def model_for(self, role: str) -> str:
        tier = self.tier_for(role)
        if tier is None:
            raise ValueError(f"unknown LLM role: {role}")
        return self.model_strong if tier == "strong" else self.model_fast

    # ---- 框架派生路径 ----
    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache" / "llm"

    @property
    def ledger_path(self) -> Path:
        return self.work_dir / "cost_ledger.jsonl"
