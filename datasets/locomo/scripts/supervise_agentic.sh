#!/bin/bash
# 看护循环：B0 被阻（blocked_b0）或 B0 全败时换根重试，保留每个被阻根；最多 N 次。
# 用法: supervise_agentic.sh <arm> <root> <max_attempts> [额外参数...]
# 环境变量 CARRY_FROM=<旧根>：新根缺 B0 时自动搬运其 B0 资产/答案检查点/判分检查点
# （用户指令：不要从头跑——答案与判分是最大头，换根不得丢）。
ARM=$1; ROOT=$2; MAX=$3; shift 3
cd /Users/xu/git/oak
mkdir -p "$(dirname "$ROOT")"
# precheck 与正式跑必须同一 arm config：--vector-k 若在附加参数里，须随 precheck 一并传，
# 否则 precheck.json 记录的 config 摘要与 campaign 不符，身份门直接拒绝启动（v2 首发事故）。
VK=""; prev=""
for a in "$@"; do
  [ "$prev" = "--vector-k" ] && VK="$a"
  prev="$a"
done
RESUME=""
carry_into() {
  local src="$1"
  [ -n "$src" ] && [ -d "$src/train/B0/generation" ] || return 0
  [ -f "$ROOT/train/B0/assets/manifest.json" ] && return 0
  # 搬运走 carry_rebase：复制 assets/generation/evaluation＋写 CARRIED 旁车＋答案路径预检；
  # 框架版本差异（仅编排/评测层）不再作废答案检查点（用户指令：不要从头跑）。
  uv run python -m datasets.locomo.scripts.carry_rebase "$ROOT" "$src" \
    || echo "[supervise] carry_rebase 失败（答案路径不一致？），该根将冷启动"
}
for i in $(seq 1 "$MAX"); do
  mkdir -p "$ROOT"
  carry_into "$CARRY_FROM"
  uv run python -m datasets.locomo.scripts.precheck_agentic --output "$ROOT" --arm "$ARM" ${VK:+--vector-k "$VK"} >/dev/null 2>&1 || { echo "[supervise] precheck 失败，重试"; sleep 60; continue; }
  echo "[supervise] attempt $i for $ARM at $ROOT $(date)"
  uv run python -u -m datasets.locomo.run --arm "$ARM" --output "$ROOT" $RESUME "$@"
  code=$?
  phase=$(python3 -c "import json;print(json.load(open('$ROOT/campaign.json'))['phase'])" 2>/dev/null || echo none)
  train_status=$(python3 -c "import json;print(json.load(open('$ROOT/train/summary.json'))['status'])" 2>/dev/null || echo none)
  echo "[supervise] exit=$code phase=$phase train=$train_status"
  # 只有明确推进（训练完成或阶段进入验证/测试/长跑）才算成功；崩溃/被阻/失败按下面规则处置。
  case "$phase:$train_status" in
    validation:*|selection:*|test:*|done:*|*:complete)
      echo "[supervise] $ARM 完成/进入长跑，退出看护"
      exit 0
      ;;
  esac
  # B0 已完整（答案检查点在案）：R 轮中途崩溃同根 --resume 重试（决策回放＋候选再预检），
  # 不换根——换根会丢迭代历史，等于从头爬。
  if [ -f "$ROOT/train/B0/stage.json" ] && \
     python3 -c "import json,sys;sys.exit(0 if json.load(open('$ROOT/train/B0/stage.json'))['status']=='complete' else 1)" 2>/dev/null; then
    echo "[supervise] B0 已完整，同根 resume 重试（保留迭代历史）"
    RESUME="--resume"; sleep 10; continue
  fi
  blocked="${ROOT}_blocked$(date +%H%M%S)"
  mv "$ROOT" "$blocked"
  echo "[supervise] 未过门（phase=$phase train=$train_status），换根重试（被阻根已保留）"
  CARRY_FROM="$blocked"   # 若被阻根已产出答案/判分检查点，新根自动续用
  RESUME=""
  sleep 10
done
echo "[supervise] $ARM 连续 $MAX 次未过 B0 门，停止（保留现场）"
exit 1
