"""本地向量库：JSONL 持久化 + 余弦线性扫描（语料千级规模足够）。

VectorStore 协议保持最小（load/upsert/search/save），后续换 OceanBase
（pyobvector：建向量列 + approx_cosine_distance 查询）只替换实现，不动调用方。
记录形态 {id, text, meta, vector} 与 Mem0 记忆接口兼容（meta 携带主体/类型/日期/出处）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class VecHit:
    id: str
    score: float
    text: str
    meta: dict = field(default_factory=dict)


class LocalVectorStore:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.texts: list[str] = []
        self.metas: list[dict] = []
        self._matrix: np.ndarray | None = None  # 已归一化的向量矩阵

    # ---------------------------------------------------------------- 持久化
    @classmethod
    def load(cls, path: Path) -> LocalVectorStore:
        s = cls()
        if not path.exists():
            raise FileNotFoundError(f"向量库不存在：{path}")
        for line in path.read_text().splitlines():
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            s.ids.append(str(o["id"]))
            s.texts.append(str(o.get("text", "")))
            s.metas.append(dict(o.get("meta") or {}))
            vec = np.asarray(o["vector"], dtype=np.float32)
            n = float(np.linalg.norm(vec))
            s._append_vec(vec / n if n > 0 else vec)
        return s

    def _append_vec(self, normalized: np.ndarray) -> None:
        self._matrix = normalized if self._matrix is None else np.vstack([self._matrix, normalized])

    def upsert(self, items: list[dict]) -> None:
        """items: [{id, text, vector, meta}]，按 id 去重（后写覆盖）。"""
        existing = {i: k for k, i in enumerate(self.ids)}
        for it in items:
            vec = np.asarray(it["vector"], dtype=np.float32)
            n = float(np.linalg.norm(vec))
            norm = vec / n if n > 0 else vec
            if it["id"] in existing:
                k = existing[it["id"]]
                self.texts[k] = str(it.get("text", ""))
                self.metas[k] = dict(it.get("meta") or {})
                self._matrix[k] = norm  # type: ignore[index]
            else:
                existing[it["id"]] = len(self.ids)
                self.ids.append(str(it["id"]))
                self.texts.append(str(it.get("text", "")))
                self.metas.append(dict(it.get("meta") or {}))
                self._append_vec(norm)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        denorm = self._matrix  # 归一化向量即方向，余弦不依赖模长
        with path.open("w") as f:
            for i, fid in enumerate(self.ids):
                f.write(
                    json.dumps(
                        {
                            "id": fid,
                            "text": self.texts[i],
                            "meta": self.metas[i],
                            "vector": denorm[i].tolist(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    # ---------------------------------------------------------------- 检索
    def __len__(self) -> int:
        return len(self.ids)

    def meta(self) -> dict:
        return {"n_records": len(self.ids)}

    def search(
        self, query_vec: list[float], top_k: int = 10, pool: str | None = None
    ) -> list[VecHit]:
        if self._matrix is None or not len(self.ids):
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        scores = self._matrix @ q
        order = np.argsort(-scores)[: max(1, int(top_k))]
        out = []
        for k in order:
            if float(scores[k]) <= 0:
                break
            meta = dict(self.metas[k])
            if pool and meta.get("pool") not in (pool, None):
                continue
            out.append(
                VecHit(id=self.ids[k], score=float(scores[k]), text=self.texts[k], meta=meta)
            )
        return out
