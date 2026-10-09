"""locomo 配置：复用 darwinagent 的 Config/LLMClient，但产物目录与模型路由独立。

模型路由（双档，无自动回退——网关不可用即报错，保证 campaign 模型同质）：
- strong = glm-5.3（智谱直连，本体起草 / 终答 / 判题）
- fast   = deepseek/deepseek-v4-flash-fast（commandcode 网关，事实抽取 / ReAct 步骤 / 归并审计）
  连通性检查用 probe_models.py 手动跑（落 runs/model_probe.json，仅诊断用，不驱动切换）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from darwinagent.config import Config
from darwinagent.presets import legacy_benchmark_config

LOCOMO_ROOT = Path(__file__).resolve().parent  # datasets/locomo/pipeline
LOCOMO_TASK_DIR = LOCOMO_ROOT.parent  # datasets/locomo
PROJECT_ROOT = LOCOMO_ROOT.parents[2]  # 仓库根

ZHIPU_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"

# namespace 统一 lc* 前缀：避开 LLMClient 对 r*/inf_q* 前缀的预算钩子
NS_PROBE = "lc_probe"
NS_SCHEMA = "lc_schema"


@dataclass
class LocomoConfig:
    cfg: Config  # darwinagent Config（含 LLMClient 所需一切）
    dataset_path: Path
    anchor_id: str = "conv-26"
    react_max_steps: int = 10
    evaluation_concurrency: int = 1
    audit_extract: bool = True  # 建图后二道完整性审计

    # ---- 派生路径 ----
    @property
    def runs_dir(self) -> Path:
        return self.cfg.work_dir

    def conv_dir(self, sample_id: str) -> Path:
        return self.runs_dir / sample_id

    def probe_path(self) -> Path:
        return self.runs_dir / "model_probe.json"


def load_locomo_config(*, dataset_path) -> LocomoConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = legacy_benchmark_config()
    cfg.role_tiers.update(
        {
            "locomo_schema": "strong",
            "locomo_answer": "strong",
            "locomo_review": "strong",
            "locomo_judge": "strong",
            "locomo_extract": "fast",
            "locomo_util": "fast",
            "locomo_steps": "fast",
            "mem0_extract": "fast",
        }
    )
    cfg.thinking_disabled_roles.update({"locomo_extract", "locomo_util", "locomo_steps"})
    cfg.empty_response_passthrough_roles.add("locomo_judge")
    cfg.api_base_url = ZHIPU_BASE
    cfg.api_key = os.environ.get("ZHIPU_API_KEY", "")
    if not cfg.api_key:
        raise RuntimeError("ZHIPU_API_KEY 未设置（.env 或环境变量）")

    # fast 档：locomo 独立变量 LOCOMO_FAST_*（不与 TravelPlanner 工作流共用 FAST_*，
    # 避免共享 .env 的写冲突）；未配置时回退 FAST_*，再回退智谱 glm-5.3-flash
    cfg.fast_base_url = (
        os.environ.get("LOCOMO_FAST_API_BASE") or os.environ.get("FAST_API_BASE", "") or ZHIPU_BASE
    )
    cfg.model_fast = (
        os.environ.get("LOCOMO_FAST_MODEL")
        or os.environ.get("FAST_MODEL", "")
        or "deepseek/deepseek-v4-flash-fast"
    )
    if cfg.fast_base_url != cfg.api_base_url:
        cfg.fast_api_key = os.environ.get("LOCOMO_FAST_API_KEY") or os.environ.get(
            "FAST_API_KEY", ""
        )
        if not cfg.fast_api_key:
            raise RuntimeError("fast 档指向外部网关但 LOCOMO_FAST_API_KEY/FAST_API_KEY 未设置")
    else:
        cfg.fast_api_key = cfg.api_key

    # locomo 产物全部隔离在 locomo/runs 下（缓存/台账/重试日志随 work_dir 派生）
    cfg.work_dir = LOCOMO_TASK_DIR / "runs"

    dataset_path = Path(dataset_path)
    lc = LocomoConfig(cfg=cfg, dataset_path=dataset_path)

    for d in (cfg.work_dir, cfg.cache_dir, cfg.ledger_path.parent, cfg.work_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    return lc


def ns(conv: str, kind: str) -> str:
    """namespace 约定：lc{NN缩写}_{kind}，如 lc26_build / lc26_q3 / lc26_judge。"""
    num = conv.replace("conv-", "")
    return f"lc{num}_{kind}"
