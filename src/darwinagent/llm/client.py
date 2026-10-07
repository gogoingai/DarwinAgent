"""OpenAI-compatible chat transport with bounded retries, cache, and attempt accounting."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, APIError, APIConnectionError, APIStatusError, APITimeoutError
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
        self._http_budget_path = Path(
            cfg.request_budget_path or (Path(cfg.work_dir) / "http_attempts.json")
        )
        if cfg.max_http_requests is not None and cfg.max_http_requests < 1:
            raise ValueError("max_http_requests must be positive")
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
                base_url=base_url,
                api_key=api_key,
                timeout=cfg.request_timeout_s,
                max_retries=0,
                http_client=httpx.AsyncClient(
                    trust_env=False,
                    timeout=cfg.request_timeout_s,
                    event_hooks={"request": [self._reserve_http_attempt]},
                ),
            )

        self._mk = _mk
        # 预热默认站点，保持「构造即可用」的旧契约
        if cfg.model_strong and cfg.api_base_url:
            self._site_for(cfg.model_strong)

    async def _reserve_http_attempt(self, request):
        # Reserve immediately before HTTP dispatch, including retries; crash-safe and
        # shared across all role clients. No prompts or credentials enter this file.
        if (
            self.cfg.deadline_monotonic is not None
            and time.monotonic() >= self.cfg.deadline_monotonic
        ):
            raise BudgetExceeded("Model request deadline exceeded")
        with counter_transaction(self._http_budget_path) as counts:
            used = counts.get("attempts", 0)
            if self.cfg.max_http_requests is not None and used >= self.cfg.max_http_requests:
                raise BudgetExceeded(
                    f"HTTP request cap reached ({used}/{self.cfg.max_http_requests})"
                )
            counts["attempts"] = used + 1

    def http_attempts(self):
        if not self._http_budget_path.exists():
            return 0
        return json.loads(self._http_budget_path.read_text()).get("attempts", 0)

    def _site_for(self, model: str) -> tuple[AsyncOpenAI, asyncio.Semaphore]:
        resolved = resolve(model, self.cfg)
        site = self._sites.get(resolved.pool_id)
        if site is None:
            site = (
                self._mk(resolved.base_url, resolved.api_key),
                asyncio.Semaphore(resolved.pool_size),
            )
            self._sites[resolved.pool_id] = site
        return site

    def _redact_error(self, value):
        """Redact transport-error data only; successful model output is unchanged."""
        secrets = {self.cfg.api_key, self.cfg.fast_api_key, self.cfg.middle_api_key}
        secrets.update(api_key for _base_url, api_key in self._sites)
        if isinstance(value, str):
            for secret in sorted((s for s in secrets if s), key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, bytes):
            for secret in sorted((s for s in secrets if s), key=len, reverse=True):
                value = value.replace(secret.encode(), b"[redacted]")
            return value
        if isinstance(value, dict):
            return {self._redact_error(k): self._redact_error(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._redact_error(v) for v in value)
        return value

    def _sanitize_transport_error(self, error):
        # Retain SDK exception types/status codes for retry classification while
        # cleaning strings and structured error metadata before runtime persistence.
        pending, seen = [error], set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
            current.args = tuple(self._redact_error(arg) for arg in current.args)
            for name in ("message", "body", "code", "param", "type"):
                if hasattr(current, name):
                    setattr(current, name, self._redact_error(getattr(current, name)))
        return error

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
        request = dict(
            role=role,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            namespace=namespace,
            use_cache=use_cache,
        )
        deadline = self.cfg.deadline_monotonic
        if deadline is None:
            return await self._chat(**request)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded("Model request deadline exceeded")
        try:
            async with asyncio.timeout(remaining):
                return await self._chat(**request)
        except TimeoutError:
            raise BudgetExceeded("Model request deadline exceeded") from None

    async def _chat(
        self, *, role, messages, temperature, max_tokens, json_mode, namespace, use_cache
    ):
        model = self.cfg.model_for(role)
        resolved = resolve(model, self.cfg)
        thinking_off = role in THINKING_OFF_ROLES or role in self.cfg.thinking_disabled_roles
        extra_body, buffer = request_policy(resolved, thinking_off, self.cfg.reasoning_effort)
        key = self._cache_key(
            model,
            messages,
            temperature,
            max_tokens,
            json_mode,
            base_url=resolved.base_url,
            extra_body=extra_body,
        )
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
                    content=data["content"],
                    usage=data.get("usage", {}),
                    cache_hit=True,
                    elapsed_s=0.0,
                    model=model,
                    role=role,
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
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        # 推理模型的 reasoning 与正文共享补全预算：缓冲来自注册表
                        # （offable 已关→disabled_buffer；effort/forced→reasoning_buffer）
                        max_tokens=max_tokens + buffer,
                        stream=True,  # 流式：防 TUN 代理掐长连接
                        stream_options={"include_usage": True},
                        timeout=min(
                            self.cfg.request_timeout_s,
                            max(0.001, self.cfg.deadline_monotonic - time.monotonic()),
                        )
                        if self.cfg.deadline_monotonic is not None
                        else self.cfg.request_timeout_s,
                    )
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    if extra_body:
                        kwargs["extra_body"] = extra_body
                    # 流式聚合（本地 TUN 代理会挂起非流式长请求）
                    parts: list[str] = []
                    usage: dict = {}
                    reasoning_chars = 0
                    stream = await use_client.chat.completions.create(**kwargs)
                    try:
                        async for chunk in stream:
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
                    finally:
                        close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                        if close is not None:
                            closing = close()
                            if inspect.isawaitable(closing):
                                await closing
                    content = "".join(parts)
                    if reasoning_chars:
                        usage["reasoning_chars"] = reasoning_chars
                result = LLMResult(
                    content=content,
                    usage=usage,
                    cache_hit=False,
                    elapsed_s=round(time.time() - t0, 2),
                    model=model,
                    role=role,
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
            except BudgetExceeded:
                raise
            except EmptyCompletion as e:
                last_err = e
            except (APIConnectionError, APITimeoutError) as e:
                last_err = self._sanitize_transport_error(e)
            except APIStatusError as e:
                last_err = self._sanitize_transport_error(e)
                if e.status_code not in (429, 500, 502, 503, 504, 529):
                    raise last_err from None  # Retry only transient service status codes.
            except APIError as e:
                raise self._sanitize_transport_error(e) from None
            # 指数退避 + 抖动（429 首次退避从 8s 起，避免反复撞限流）
            base = (
                8.0
                if (
                    isinstance(last_err, APIStatusError)
                    and getattr(last_err, "status_code", None) == 429
                )
                else 1.0
            )
            backoff = min(60.0, base * (2**attempt)) + random.uniform(0, 2)
            self._log_retry(namespace, role, attempt, repr(last_err), backoff)
            if attempt + 1 >= self.cfg.max_retries:
                break
            if (
                self.cfg.deadline_monotonic is not None
                and time.monotonic() + backoff >= self.cfg.deadline_monotonic
            ):
                raise BudgetExceeded("Model request deadline would be exceeded by retry")
            await asyncio.sleep(backoff)

        raise TransportExhausted(role, last_err) from last_err

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
                used = sum(
                    count
                    for name, count in self._ns_calls.items()
                    if name == scope or name.startswith(scope + "_")
                )
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
                f.write(
                    json.dumps(
                        {
                            "ts": round(time.time(), 1),
                            "namespace": namespace,
                            "role": role,
                            "model": model,
                            "cache_hit": cache_hit,
                            **usage,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def ledger_summary(self) -> dict:
        """汇总台账：按模型/命名空间的调用数与 token。"""
        summary: dict[str, Any] = {
            "total_calls": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "by_model": {},
            "by_ns": {},
        }
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
    def _cache_key(
        model, messages, temperature, max_tokens, json_mode, *, base_url="", extra_body=None
    ) -> str:
        # 缓存随「有效传输策略」失效：站点 + 实际发送的参数。推理缓冲是
        # (模型注册档, 参数) 的确定函数，不必单独入键。
        value = {
            "m": model,
            "msg": messages,
            "t": temperature,
            "mt": max_tokens,
            "j": json_mode,
            "base_url": base_url,
            "extra_body": extra_body or {},
            "transport_version": 3,
            "registry_version": REGISTRY_VERSION,
        }
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    @staticmethod
    def _save_cache(path: Path, result: LLMResult) -> None:
        atomic_json(path, {"content": result.content, "usage": result.usage, "model": result.model})

    def _log_retry(self, namespace, role, attempt, err, backoff) -> None:
        err = self._redact_error(err)
        try:
            with self._retry_log.open("a") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": round(time.time(), 1),
                            "ns": namespace,
                            "role": role,
                            "attempt": attempt,
                            "err": err,
                            "backoff_s": round(backoff, 1),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass
