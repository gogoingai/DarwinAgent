"""模型注册表：端点、密钥、思考参数、推理缓冲、并发池全部按模型名解析。

用户决策（2026-10-04）：参数决策跟着模型走，key/端点/参数收进统一注册模式——
不再有「档位→端点」「端点→缓冲」的间接层，把任何模型调到任何角色，行为自洽。

解析规则：
  - 条目按键（模型名前缀）「最长匹配」胜出，精确名即最长前缀；
  - 未注册模型回落到保守缺省档（默认端点、思考关不掉、给足缓冲）；
  - 站点三种：default（cfg.api_base_url/api_key，主站）、fast（cfg.fast_*，
    环境可按数据集前缀覆盖）、explicit（常量 URL + 密钥环境变量）。
    explicit 站点的 URL 可被 <KEY_ENV 前缀>_BASE_URL 覆盖（如 COMMANDCODE_BASE_URL）。

密钥本体只存 .env（gitignore），注册表与代码库绝不出现密钥值。
思考语义三类：
  offable —— 支持 {"thinking":{"type":"disabled"}}（智谱 glm 系；commandcode 的
             deepseek 系实测同样接受，432ch→25ch）。关后仍可能残留少量推理，
             disabled_buffer 兜底。
  effort  —— 关不掉，唯一旋钮是 reasoning_effort（MiniMax M3.1 Flash，传
             disabled 会 400）。
  forced  —— 关不掉也没有深度档，只能靠大缓冲兜底（缺省档即此）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 注册表结构版本：条目语义变化时 +1，随 transport identity / 缓存键失效
REGISTRY_VERSION = 1

DEFAULT_REASONING_BUFFER = 3072
EXTERNAL_REASONING_BUFFER = 32000


@dataclass(frozen=True)
class SiteSpec:
    kind: str                 # 'default' | 'fast' | 'explicit'
    url: str = ''             # explicit 站点的 URL 常量
    key_env: str = ''         # explicit 站点的密钥环境变量名
    base_env: str = ''        # URL 环境变量名（缺省按 key_env 推导）


DEFAULT_SITE = SiteSpec('default')
FAST_SITE = SiteSpec('fast')


@dataclass(frozen=True)
class ModelProfile:
    site: SiteSpec = DEFAULT_SITE
    thinking: str = 'forced'            # 'offable' | 'effort' | 'forced'
    reasoning_buffer: int = DEFAULT_REASONING_BUFFER   # 思考未关（或关不掉）时的请求侧余量
    disabled_buffer: int = DEFAULT_REASONING_BUFFER    # offable 且已关时的余量（残留推理兜底）
    default_reasoning_effort: str = ''  # effort 型模型的缺省深度（cfg.reasoning_effort 优先）
    pool_size: int = 0                  # 站点并发池大小；0 → 站点缺省


# ---- 注册条目（按模型名前缀，最长匹配胜出）----
REGISTRY: tuple[tuple[str, ModelProfile], ...] = (
    # 智谱 glm 系（bigmodel coding 端点＝唯一强档端点）：思考可关。
    # 关后长输出实测仍偶发数百字符残留推理 → disabled_buffer=768 兜底（2026-10-04 台账）。
    ('glm-5', ModelProfile(DEFAULT_SITE, thinking='offable',
                           reasoning_buffer=3072, disabled_buffer=768)),
    ('glm-4', ModelProfile(DEFAULT_SITE, thinking='offable',
                           reasoning_buffer=3072, disabled_buffer=768)),
    # MiniMax M3.1 Flash（中间档，用户拍板 2026-10-04：MiniMax 强于 DeepSeek、弱于 glm）：
    # 端点自带显式 LOCOMO_FAST_*，不再占用框架 fast 槽。强制思考（disabled 400），
    # 唯一旋钮 effort，low 档实测推理近零。缓冲按外部推理模型给足。
    ('MiniMax-M3.1-Flash', ModelProfile(SiteSpec('explicit',
                                                 url='https://api.minimaxi.chat/v1',
                                                 key_env='LOCOMO_FAST_API_KEY',
                                                 base_env='LOCOMO_FAST_API_BASE'),
                                        thinking='effort',
                                        reasoning_buffer=EXTERNAL_REASONING_BUFFER,
                                        default_reasoning_effort='low',
                                        pool_size=6)),
    # commandcode 网关 deepseek 系：思考可关（实测 plain 432ch / disabled 25ch），
    # 不关时是长推理模型 → 外部缓冲；独立限流池可略高。
    ('deepseek/', ModelProfile(SiteSpec('explicit',
                                        url='https://api.commandcode.ai/provider/v1',
                                        key_env='COMMANDCODE_API_KEY'),
                               thinking='offable',
                               reasoning_buffer=EXTERNAL_REASONING_BUFFER,
                               disabled_buffer=256,
                               pool_size=6)),
)

# 未注册模型的保守缺省：默认端点、思考关不掉、缓冲给足（绝不因角色关思考而清零缓冲）
FALLBACK_PROFILE = ModelProfile(DEFAULT_SITE, thinking='forced',
                                reasoning_buffer=DEFAULT_REASONING_BUFFER,
                                disabled_buffer=DEFAULT_REASONING_BUFFER)


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
    entry = next(((k, p) for k, p in sorted(REGISTRY, key=lambda e: -len(e[0]))
                  if model.startswith(k)), None)
    profile = entry[1] if entry else FALLBACK_PROFILE
    site = profile.site
    if site.kind == 'fast':
        base_url, api_key = cfg.fast_base_url, cfg.fast_api_key
    elif site.kind == 'explicit':
        base_url = os.environ.get(site.base_env or site.key_env.replace('_API_KEY', '_BASE_URL'), site.url)
        api_key = os.environ.get(site.key_env, '')
    else:
        base_url, api_key = cfg.api_base_url, cfg.api_key
    pool_size = profile.pool_size or (cfg.fast_max_concurrency if site.kind == 'fast'
                                      else cfg.max_concurrency)
    return ResolvedModel(model, base_url, api_key, profile, pool_size)


def request_policy(resolved: ResolvedModel, thinking_off: bool, global_effort: str = ''):
    """按模型（＋角色开关）决定本次请求的 extra_body 与推理缓冲。

    - offable 且角色关思考 → 发 thinking:disabled，缓冲降为 disabled_buffer；
    - effort 型 → 发 reasoning_effort（cfg.reasoning_effort 优先于条目缺省），
      缓冲恒为 reasoning_buffer（low 档也仍在思考）；
    - forced / 未注册 → 无参数可发，只有缓冲兜底；缓冲不因角色开关清零
      （旧「档位键控」实现里未知模型＋关思考角色会拿 0 缓冲，是缺陷，此处修复）。
    """
    profile = resolved.profile
    extra: dict = {}
    if thinking_off and profile.thinking == 'offable':
        extra['thinking'] = {'type': 'disabled'}
        return extra, profile.disabled_buffer
    if profile.thinking == 'effort':
        effort = global_effort or profile.default_reasoning_effort
        if effort:
            extra['reasoning_effort'] = effort
    return extra, profile.reasoning_buffer
