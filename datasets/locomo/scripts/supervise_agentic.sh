#!/bin/bash
# 看护循环：B0 被阻（blocked_b0）或 B0 全败时换根重试，保留每个被阻根；最多 N 次。
# 用法: supervise_agentic.sh <arm> <root> <max_attempts> [额外参数...]
ARM=$1; ROOT=$2; MAX=$3; shift 3
cd /Users/xu/git/oak
mkdir -p "$(dirname "$ROOT")"
for i in $(seq 1 "$MAX"); do
  mkdir -p "$ROOT"
  uv run python -m datasets.locomo.scripts.precheck_agentic --output "$ROOT" --arm "$ARM" ${EXTRA_K:+--vector-k $EXTRA_K} >/dev/null 2>&1 || { echo "[supervise] precheck 失败，重试"; sleep 60; continue; }
  echo "[supervise] attempt $i for $ARM at $ROOT $(date)"
  uv run python -u -m datasets.locomo.run --arm "$ARM" --output "$ROOT" "$@"
  code=$?
  phase=$(python3 -c "import json;print(json.load(open('$ROOT/campaign.json'))['phase'])" 2>/dev/null || echo none)
  train_status=$(python3 -c "import json;print(json.load(open('$ROOT/train/summary.json'))['status'])" 2>/dev/null || echo none)
  echo "[supervise] exit=$code phase=$phase train=$train_status"
  if [ "$phase" = "blocked_b0" ] || [ "$train_status" = "failed" ]; then
    mv "$ROOT" "${ROOT}_blocked$(date +%H%M%S)"
    echo "[supervise] B0 未过门，换根重试（被阻根已保留）"
    sleep 10
    continue
  fi
  echo "[supervise] $ARM 完成/进入长跑，退出看护"
  exit 0
done
echo "[supervise] $ARM 连续 $MAX 次未过 B0 门，停止（保留现场）"
exit 1
