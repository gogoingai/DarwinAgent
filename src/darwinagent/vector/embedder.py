"""嵌入客户端：OpenAI 兼容 /embeddings，磁盘缓存（键含 base_url|model|text，端点或模型
变了自动失效）。同步实现——沙箱内 F 与索引构建共用；异步路径请用 asyncio.to_thread。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from darwinagent.runtime.deadline import bounded_timeout, remaining_seconds


@dataclass
class Embedder:
    base_url: str
    api_key: str
    model: str
    dim: int = 0  # 0 = 首次响应后回填
    cache_path: Path | None = None  # None = 不落盘
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            if self.cache_path.exists():
                try:
                    self._cache = json.loads(self.cache_path.read_text())
                except Exception:  # noqa: BLE001
                    self._cache = {}

    # ---------------------------------------------------------------- 基础
    def _key(self, text: str) -> str:
        blob = f"{self.base_url}|{self.model}|{text}"
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _request(self, texts: list[str], retries: int = 4) -> list[list[float]]:
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                r = httpx.post(
                    f"{self.base_url.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts},
                    timeout=bounded_timeout(60),
                )
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                data = r.json()["data"]
                remaining_seconds()
                vecs = [d["embedding"] for d in data]
                if not self.dim and vecs:
                    self.dim = len(vecs[0])
                return vecs
            except Exception as e:  # noqa: BLE001
                remaining_seconds()
                last_err = e
                time.sleep(bounded_timeout(1.5 * (attempt + 1)))
        raise RuntimeError(f"embeddings 调用失败（{self.base_url} {self.model}）: {last_err}")

    # ---------------------------------------------------------------- 对外
    def embed(self, text: str) -> list[float]:
        remaining_seconds()
        text = str(text).strip()
        k = self._key(text)
        if k in self._cache:
            return self._cache[k]
        (vec,) = self._request([text])
        remaining_seconds()
        self._cache[k] = vec
        self._flush()
        return vec

    def embed_batch(self, texts: list[str], batch: int = 10, progress=False) -> list[list[float]]:
        texts = [str(t).strip() for t in texts]
        keyed: list[tuple[str, list[float] | None]] = []
        todo: list[int] = []
        for i, t in enumerate(texts):
            k = self._key(t)
            if k in self._cache:
                keyed.append((k, self._cache[k]))
            else:
                keyed.append((k, None))
                todo.append(i)
        for j in range(0, len(todo), batch):
            idxs = todo[j : j + batch]
            vecs = self._request([texts[i] for i in idxs])
            for i, v in zip(idxs, vecs):
                keyed[i] = (keyed[i][0], v)
                self._cache[keyed[i][0]] = v
            self._flush()
            if progress:
                print(f"  嵌入 {min(j + batch, len(todo))}/{len(todo)}", flush=True)
        return [v for _, v in keyed]

    def _flush(self) -> None:
        if self.cache_path is None:
            return
        # 并发写者各用唯一临时名，再原子改名——共享固定 .tmp 名会在并发缓存未命中时
        # 互相抢文件（conv-47 外测 17 题 FileNotFoundError 事故，2026-10-04）。
        import os, threading

        tmp = self.cache_path.with_name(
            f"{self.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False))
        tmp.replace(self.cache_path)


def load_embedder(env=None, cache_path: Path | None = None) -> Embedder:
    """Embedder from EMBEDDING_* environment variables (the caller is responsible for
    loading .env, e.g. via darwinagent.llm.settings.load_connection)."""
    import os

    env = env if env is not None else os.environ
    base, key, model = (
        env.get("EMBEDDING_BASE_URL", "").strip(),
        env.get("EMBEDDING_API_KEY", "").strip(),
        env.get("EMBEDDING_MODEL", "").strip(),
    )
    if not (base and key and model):
        raise RuntimeError("EMBEDDING_BASE_URL/EMBEDDING_API_KEY/EMBEDDING_MODEL 未配置")
    return Embedder(base_url=base, api_key=key, model=model, cache_path=cache_path)
