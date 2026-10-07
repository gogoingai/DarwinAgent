"""Opt-in compatibility profiles and explicit model connection resolution.

Generic mode uses one configured endpoint without vendor-specific parameters.
Legacy benchmark presets enable known model profiles explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 注册表结构版本：条目语义变化时 +1，随 transport identity / 缓存键失效
REGISTRY_VERSION = 2

DEFAULT_REASONING_BUFFER = 3072
EXTERNAL_REASONING_BUFFER = 32000


@dataclass(frozen=True)
class SiteSpec:
    kind: str  # 'default' | 'fast' | 'explicit'
    url: str = ""  # explicit 站点的 URL 常量
    key_env: str = ""  # explicit 站点的密钥环境变量名
    base_env: str = ""  # URL 环境变量名（缺省按 key_env 推导）


DEFAULT_SITE = SiteSpec("default")
FAST_SITE = SiteSpec("fast")


@dataclass(frozen=True)
class ModelProfile:
    site: SiteSpec = DEFAULT_SITE
    thinking: str = "forced"  # 'offable' | 'effort' | 'forced'
    reasoning_buffer: int = DEFAULT_REASONING_BUFFER  # 思考未关（或关不掉）时的请求侧余量
    disabled_buffer: int = DEFAULT_REASONING_BUFFER  # offable 且已关时的余量（残留推理兜底）
    default_reasoning_effort: str = ""  # effort 型模型的缺省深度（cfg.reasoning_effort 优先）
    pool_size: int = 0  # 站点并发池大小；0 → 站点缺省


# ---- 注册条目（按模型名前缀，最长匹配胜出）----
REGISTRY: tuple[tuple[str, ModelProfile], ...] = (
    # 智谱 glm 系（bigmodel coding 端点＝唯一强档端点）：思考可关。
    # 关后长输出实测仍偶发数百字符残留推理 → disabled_buffer=768 兜底（2026-10-04 台账）。
    (
        "glm-5",
        ModelProfile(DEFAULT_SITE, thinking="offable", reasoning_buffer=3072, disabled_buffer=768),
    ),
    (
        "glm-4",
        ModelProfile(DEFAULT_SITE, thinking="offable", reasoning_buffer=3072, disabled_buffer=768),
    ),
    # MiniMax M3.1 Flash（中间档，historical benchmark compatibility profile）：
    # 端点自带显式 LOCOMO_FAST_*，不再占用框架 fast 槽。强制思考（disabled 400），
    # 唯一旋钮 effort，low 档实测推理近零。缓冲按外部推理模型给足。
    (
        "MiniMax-M3.1-Flash",
        ModelProfile(
            SiteSpec(
                "explicit",
                url="https://api.minimaxi.chat/v1",
                key_env="LOCOMO_FAST_API_KEY",
                base_env="LOCOMO_FAST_API_BASE",
            ),
            thinking="effort",
            reasoning_buffer=EXTERNAL_REASONING_BUFFER,
            default_reasoning_effort="low",
            pool_size=6,
        ),
    ),
    # commandcode 网关 deepseek 系：思考可关（实测 plain 432ch / disabled 25ch），
    # 不关时是长推理模型 → 外部缓冲；独立限流池可略高。
    (
        "deepseek/",
        ModelProfile(
            SiteSpec(
                "explicit",
                url="https://api.commandcode.ai/provider/v1",
                key_env="COMMANDCODE_API_KEY",
            ),
            thinking="offable",
            reasoning_buffer=EXTERNAL_REASONING_BUFFER,
            disabled_buffer=256,
            pool_size=6,
        ),
    ),
)

# 未注册模型的保守缺省：默认端点、思考关不掉、缓冲给足（绝不因角色关思考而清零缓冲）
FALLBACK_PROFILE = ModelProfile(
    DEFAULT_SITE,
    thinking="forced",
    reasoning_buffer=DEFAULT_REASONING_BUFFER,
    disabled_buffer=DEFAULT_REASONING_BUFFER,
)


@dataclass(frozen=True)
class ResolvedModel:
    model: str
    base_url: str
    api_key: str
    profile: ModelProfile
    pool_size: int

    @property
    def pool_id(self) -> tuple[str, str]:
        """同站（同 URL 同密钥）模型共享一个并发池：单网关不得看到 2×并发。"""
        return (self.base_url, self.api_key)


def resolve(model: str, cfg) -> ResolvedModel:
    entry = next(
        ((k, p) for k, p in sorted(REGISTRY, key=lambda e: -len(e[0])) if model.startswith(k)), None
    )
    profile = (
        (entry[1] if entry else FALLBACK_PROFILE)
        if cfg.model_profiles
        else ModelProfile(reasoning_buffer=0, disabled_buffer=0)
    )
    site = profile.site
    if site.kind == "fast":
        base_url, api_key = cfg.fast_base_url, cfg.fast_api_key
    elif site.kind == "explicit":
        base_url = os.environ.get(
            site.base_env or site.key_env.replace("_API_KEY", "_BASE_URL"), site.url
        )
        api_key = os.environ.get(site.key_env, "")
    else:
        base_url, api_key = cfg.api_base_url, cfg.api_key
        if model != cfg.model_strong and model == cfg.model_middle and cfg.middle_base_url:
            base_url, api_key = cfg.middle_base_url, cfg.middle_api_key
        elif model != cfg.model_strong and model == cfg.model_fast and cfg.fast_base_url:
            base_url, api_key = cfg.fast_base_url, cfg.fast_api_key
    pool_size = profile.pool_size or (
        cfg.fast_max_concurrency if site.kind == "fast" else cfg.max_concurrency
    )
    return ResolvedModel(model, base_url, api_key, profile, pool_size)


def request_policy(resolved: ResolvedModel, thinking_off: bool, global_effort: str = ""):
    """按模型（＋角色开关）决定本次请求的 extra_body 与推理缓冲。

    - offable 且角色关思考 → 发 thinking:disabled，缓冲降为 disabled_buffer；
    - effort 型 → 发 reasoning_effort（cfg.reasoning_effort 优先于条目缺省），
      缓冲恒为 reasoning_buffer（low 档也仍在思考）；
    - forced / 未注册 → 无参数可发，只有缓冲兜底；缓冲不因角色开关清零
      （旧「档位键控」实现里未知模型＋关思考角色会拿 0 缓冲，是缺陷，此处修复）。
    """
    profile = resolved.profile
    extra: dict = {}
    if thinking_off and profile.thinking == "offable":
        extra["thinking"] = {"type": "disabled"}
        return extra, profile.disabled_buffer
    if profile.thinking == "effort":
        effort = global_effort or profile.default_reasoning_effort
        if effort:
            extra["reasoning_effort"] = effort
    return extra, profile.reasoning_buffer
