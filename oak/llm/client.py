"""统一 LLM 客户端：模型注册表路由、站点级并发池、退避重试、磁盘缓存、成本台账。

全项目所有 LLM 调用的唯一通道——任何模块不得直接实例化 OpenAI client。
端点/密钥/思考参数/推理缓冲/并发池全部按模型名从 oak.llm.registry 解析：
把任何注册模型调到任何角色，请求行为自动跟着模型走。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, APITimeoutError
import httpx

from ..config import Config
from .registry import REGISTRY_VERSION, request_policy, resolve
from ..runtime.artifacts import atomic_json
from ..runtime.budgets import counter_transaction


class BudgetExceeded(RuntimeError):
    pass


class EmptyCompletion(RuntimeError):
    pass


class TransportExhausted(RuntimeError):
    """A transport failure after the client's existing bounded recovery."""

    def __init__(self, role, cause):
        super().__init__(f"LLM 调用在传输层恢复耗尽: {role}: {cause!r}")
        self.role, self.cause_type = role, type(cause).__name__


# 关闭深度思考的角色（机械执行类任务无需长思考）；核心推理步骤保留思考。
# 「哪些角色关思考」是提示工程策略（Config 层）；「怎么关、关不掉怎么办」按模型
# 走注册表——两个维度正交。
THINKING_OFF_ROLES = {"kg", "react", "slots", "plan_repair"}


@dataclass
class LLMResult:
    content: str
    usage: dict
    cache_hit: bool
    elapsed_s: float
    model: str
    role: str


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if not cfg.fast_base_url:
            cfg.fast_base_url = cfg.api_base_url
            cfg.fast_api_key = cfg.fast_api_key or cfg.api_key
        elif cfg.fast_base_url == cfg.api_base_url and not cfg.fast_api_key:
            cfg.fast_api_key = cfg.api_key
        cfg.work_dir = Path(cfg.work_dir)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        # 站点池：按解析出的 (base_url, api_key) 惰性建客户端＋信号量。
        # 同站模型共享一池（单网关不得看到 2×并发），异站分池互不排队。
        self._sites: dict[tuple[str, str], tuple[AsyncOpenAI, asyncio.Semaphore]] = {}
        self._ledger_lock = asyncio.Lock()
        self._retry_log = cfg.work_dir / "logs" / "retry.jsonl"
        self._retry_log.parent.mkdir(parents=True, exist_ok=True)
        # 命名空间调用计数（内存 + 台账聚合），供限额检查
        self._ns_calls: dict[str, int] = {}
        self._budget_lock = asyncio.Lock()
        self._budget_path = cfg.work_dir / "call_counts.json"
        if self._budget_path.exists():
            self._ns_calls = json.loads(self._budget_path.read_text())

        # 直连不走系统代理（本地代理会吞掉国内 API 的长请求）
        def _mk(base_url: str, api_key: str) -> AsyncOpenAI:
            return AsyncOpenAI(
                base_url=base_url, api_key=api_key, timeout=300.0,
                http_client=httpx.AsyncClient(trust_env=False, timeout=300.0),
            )

        self._mk = _mk
        # 预热默认站点，保持「构造即可用」的旧契约
        self._site_for(cfg.model_strong)

    def _site_for(self, model: str) -> tuple[AsyncOpenAI, asyncio.Semaphore]:
        resolved = resolve(model, self.cfg)
        site = self._sites.get(resolved.pool_id)
        if site is None:
            site = (self._mk(resolved.base_url, resolved.api_key),
                    asyncio.Semaphore(resolved.pool_size))
            self._sites[resolved.pool_id] = site
        return site

    # ---------- 对外主入口 ----------
    async def chat(
        self,
        *,
        role: str,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        json_mode: bool = False,
        namespace: str = "default",
        use_cache: bool = True,
    ) -> LLMResult:
        model = self.cfg.model_for(role)
        resolved = resolve(model, self.cfg)
        thinking_off = role in THINKING_OFF_ROLES or role in self.cfg.thinking_disabled_roles
        extra_body, buffer = request_policy(resolved, thinking_off, self.cfg.reasoning_effort)
        key = self._cache_key(model, messages, temperature, max_tokens, json_mode,
                              base_url=resolved.base_url, extra_body=extra_body)
        cache_file = self.cfg.cache_dir / namespace / f"{key}.json"

        async with self._budget_lock:
            with counter_transaction(self._budget_path) as counts:
                self._ns_calls = counts
                self._check_budget(namespace)
                counts[namespace] = counts.get(namespace, 0) + 1
        if use_cache and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                return LLMResult(
                    content=data["content"], usage=data.get("usage", {}),
                    cache_hit=True, elapsed_s=0.0, model=model, role=role,
                )
            except Exception:
                pass  # 缓存损坏则重打

        last_err: Exception | None = None
        use_client, use_sem = self._site_for(model)
        for attempt in range(self.cfg.max_retries):
            try:
                async with use_sem:
                    t0 = time.time()
                    kwargs: dict[str, Any] = dict(
                        model=model, messages=messages,
                        temperature=temperature,
                        # 推理模型的 reasoning 与正文共享补全预算：缓冲来自注册表
                        # （offable 已关→disabled_buffer；effort/forced→reasoning_buffer）
                        max_tokens=max_tokens + buffer,
                        stream=True,                          # 流式：防 TUN 代理掐长连接
                        stream_options={"include_usage": True},
                        timeout=300.0,
                    )
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    if extra_body:
                        kwargs["extra_body"] = extra_body
                    # 流式聚合（本地 TUN 代理会挂起非流式长请求）
                    parts: list[str] = []
                    usage: dict = {}
                    reasoning_chars = 0
                    async for chunk in await use_client.chat.completions.create(**kwargs):
                        if chunk.choices:
                            delta = chunk.choices[0].delta
                            if delta and delta.content:
                                parts.append(delta.content)
                            if delta and getattr(delta, "reasoning_content", None):
                                reasoning_chars += len(delta.reasoning_content)
                        if getattr(chunk, "usage", None):
                            usage = {
                                "prompt_tokens": chunk.usage.prompt_tokens or 0,
                                "completion_tokens": chunk.usage.completion_tokens or 0,
                            }
                    content = "".join(parts)
                    if reasoning_chars:
                        usage["reasoning_chars"] = reasoning_chars
                result = LLMResult(
                    content=content, usage=usage, cache_hit=False,
                    elapsed_s=round(time.time() - t0, 2), model=model, role=role,
                )
                # 空正文（思考吃光预算）不入缓存，下次调用自动重试
                if content.strip():
                    self._save_cache(cache_file, result)
                else:
                    result.usage["empty_content"] = True
                await self._append_ledger(namespace, role, model, usage, cache_hit=False)
                if not content.strip() and role not in self.cfg.empty_response_passthrough_roles:
                    raise EmptyCompletion(f"Empty generation response from {model}")
                return result
            except EmptyCompletion as e:
                last_err = e
            except (APIConnectionError, APITimeoutError) as e:
                last_err = e
            except APIStatusError as e:
                last_err = e
                if e.status_code not in (429, 500, 502, 503, 504, 529):
                    raise                      # 4xx（除 429）不重试；529＝智谱 overloaded_error（B0/R1 服务拥塞故障，2026-10-05）
            # 指数退避 + 抖动（429 首次退避从 8s 起，避免反复撞限流）
            base = 8.0 if (isinstance(last_err, APIStatusError)
                           and getattr(last_err, "status_code", None) == 429) else 1.0
            backoff = min(60.0, base * (2 ** attempt)) + random.uniform(0, 2)
            self._log_retry(namespace, role, attempt, repr(last_err), backoff)
            await asyncio.sleep(backoff)

        raise TransportExhausted(role,last_err) from last_err

    def chat_sync(self, **kwargs) -> LLMResult:
        return asyncio.run(self.chat(**kwargs))

    # ---------- 限额与台账 ----------
    def _count(self, namespace: str) -> None:
        self._ns_calls[namespace] = self._ns_calls.get(namespace, 0) + 1
        atomic_json(self._budget_path, self._ns_calls)

    async def aclose(self) -> None:
        for client, _ in self._sites.values():
            await client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def _check_budget(self, namespace: str) -> None:
        # 命名空间 r{n}_* 受单轮上限约束；inf_q* 受单题上限约束
        n = self._ns_calls.get(namespace, 0)
        for scope, explicit in self.cfg.namespace_limits.items():
            if namespace == scope or namespace.startswith(scope + "_"):
                used = sum(count for name, count in self._ns_calls.items()
                           if name == scope or name.startswith(scope + "_"))
                if used >= explicit:
                    raise BudgetExceeded(f"{scope}: {used} >= explicit_limit={explicit}")
        if namespace.startswith("r") and "_inf" not in namespace:
            limit = self.cfg.limits.get("build_round_calls")
            if limit and n >= limit:
                raise BudgetExceeded(f"{namespace}: {n} >= build_round_calls={limit}")
        elif namespace.startswith("inf_q"):
            limit = self.cfg.limits.get("inference_calls_per_q")
            if limit and n >= limit:
                raise BudgetExceeded(f"{namespace}: {n} >= inference_calls_per_q={limit}")

    async def _append_ledger(self, namespace, role, model, usage, cache_hit: bool) -> None:
        async with self._ledger_lock:
            with self.cfg.ledger_path.open("a") as f:
                f.write(json.dumps({
                    "ts": round(time.time(), 1), "namespace": namespace,
                    "role": role, "model": model, "cache_hit": cache_hit,
                    **usage,
                }, ensure_ascii=False) + "\n")

    def ledger_summary(self) -> dict:
        """汇总台账：按模型/命名空间的调用数与 token。"""
        summary: dict[str, Any] = {"total_calls": 0, "cache_hits": 0,
                                   "prompt_tokens": 0, "completion_tokens": 0,
                                   "by_model": {}, "by_ns": {}}
        if not self.cfg.ledger_path.exists():
            return summary
        for line in self.cfg.ledger_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            summary["total_calls"] += 1
            if rec.get("cache_hit"):
                summary["cache_hits"] += 0  # 命中不写台账；此处恒 0，留字段
            summary["prompt_tokens"] += rec.get("prompt_tokens", 0)
            summary["completion_tokens"] += rec.get("completion_tokens", 0)
            m = summary["by_model"].setdefault(rec["model"], {"calls": 0})
            m["calls"] += 1
            ns = summary["by_ns"].setdefault(rec["namespace"], {"calls": 0})
            ns["calls"] += 1
        return summary

    # ---------- 缓存 ----------
    @staticmethod
    def _cache_key(model, messages, temperature, max_tokens, json_mode, *,
                   base_url="", extra_body=None) -> str:
        # 缓存随「有效传输策略」失效：站点 + 实际发送的参数。推理缓冲是
        # (模型注册档, 参数) 的确定函数，不必单独入键。
        value = {
            "m": model, "msg": messages, "t": temperature,
            "mt": max_tokens, "j": json_mode, "base_url": base_url,
            "extra_body": extra_body or {},
            "transport_version": 3, "registry_version": REGISTRY_VERSION,
        }
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @staticmethod
    def _save_cache(path: Path, result: LLMResult) -> None:
        atomic_json(path, {"content": result.content, "usage": result.usage,
                           "model": result.model})

    def _log_retry(self, namespace, role, attempt, err, backoff) -> None:
        try:
            with self._retry_log.open("a") as f:
                f.write(json.dumps({
                    "ts": round(time.time(), 1), "ns": namespace, "role": role,
                    "attempt": attempt, "err": err, "backoff_s": round(backoff, 1),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
