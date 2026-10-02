"""新迭代轮脚手架:目录 + 缓存继承 + 图复用 + 提案模板。

用法:
  uv run python -m datasets.locomo.scripts.new_round --prev b0 --round t1 \
      --kind H --asset "agent.py+kernel/checks" \
      --reason "多要素完整性确定性合并" --failures "idx?,?,?"

产物: runs/iter_v2/<round>/{cache/llm, conv-26/graph_*} 与 proposal.json
不复制: call_counts/cost_ledger/frozen_code/generation_contract/作答目录
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITER = ROOT / "datasets/locomo/runs/iter_v2"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prev", required=True)
    ap.add_argument("--round", required=True)
    ap.add_argument("--kind", required=True, choices=list("SFCPH"))
    ap.add_argument("--asset", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--failures", default="")
    ap.add_argument("--convs", default="conv-26")
    args = ap.parse_args()

    prev = ITER / args.prev
    target = ITER / args.round
    if target.exists():
        raise SystemExit(f"{target} already exists")
    (target / "cache").mkdir(parents=True)
    shutil.copytree(prev / "cache" / "llm", target / "cache" / "llm")
    for conv in args.convs.split(","):
        src = prev / conv
        if src.is_dir():
            for graph_dir in src.glob("graph_*"):
                (target / conv).mkdir(parents=True, exist_ok=True)
                shutil.copytree(graph_dir, target / conv / graph_dir.name)
    failure_ids = [int(x) for x in args.failures.replace("idx", "").split(",") if x.strip()]
    proposal = {"round": args.round, "prev": args.prev, "kind": args.kind,
                "asset": args.asset, "reason": args.reason, "failure_ids": failure_ids,
                "ts": time.time()}
    (target / "proposal.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2))
    n_cache = sum(1 for _ in (target / "cache/llm").rglob("*.json"))
    print(f"{target} ready: cache={n_cache} graphs={list((target).glob('conv-*/graph_*'))}")


if __name__ == "__main__":
    main()
