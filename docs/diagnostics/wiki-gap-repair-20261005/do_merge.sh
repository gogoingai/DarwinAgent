#!/bin/zsh
# 合并序列（阶段 5）：Wiki 分支提交 → 主干合入分支 → 回归 → main --no-ff 合并 → 验证
# 前置：mc3/c26c 健康通过、三套回归绿、报告更新完成。
set -e
cd /Users/xu/git/oak/.cache/worktrees/wiki-experience-phase-one

# 1) 暂存（只任务路径）并提交
zsh docs/diagnostics/wiki-gap-repair-20261005/stage_for_merge.sh
git commit -m "Wiki 循环记忆层：动态图真实数据准入＋C 失败候选可验证回放（三轮审查修复）

缺口①动态图准入：_preflight 动态分支 per-case 真图供给（复用/重抽两支，键控缓存），
冒烟/B0/bootstrap 门同构开启，零任务硬编码。
缺口②C 失败回放：候选检查失败快照持久化（60行/200KB 封顶），三族分类
must_reject/verified_must_pass/informational，verified_fix 绑定具体复现检查
（digest-in-ref 跨场景匹配），归因不覆盖回放事实。
三轮独立审查 8 项 P1/P2 全部修复（正例三档/全拒C拦截/压缩真实字段/数值×宽过滤
组合/绑定强化/自洽夹具替换旧模型输出）。
真实验证：Travel loop6/loop7 完整健康闭环；conv-26 c26b R2 9/10 采纳；
回归 319+14+11 全绿。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"

WIKI_SHA=$(git rev-parse HEAD)
echo "Wiki branch commit: $WIKI_SHA"

# 2) 主干合入分支（保留主干修复；冲突时以内容等价优先）
git merge main --no-edit
# 等价性证明：合并树与验证代码在全部任务路径上，除主干既有的 pipeline.py
# 豁免表单行（1dfde3c，仅放宽检查点搬运容忍、不参与答案计算）外应零差异。
git diff "$WIKI_SHA" -- oak/ datasets/ tasks/ tests/ > /tmp/merge_drift.diff || true
KNOWN=$(grep -vc "^ " /tmp/merge_drift.diff || true)
FILTERED=$(grep -v "^ " /tmp/merge_drift.diff | grep -v -E "^(diff --git|index |--- |\+\+\+ |@@|-CARRY_NEUTRAL_SUFFIXES|\+CARRY_NEUTRAL_SUFFIXES)" || true)
if [ -n "$FILTERED" ]; then
  echo "!! 合并引入计划外源码漂移（需解释或重验）："; echo "$FILTERED" | head -30; exit 1
fi
echo "合并树与验证代码等价（仅 pipeline.py 豁免表单行＝主干 1dfde3c 既有修复）"

# 3) 受影响回归（Wiki 工作树，合并后代码）
PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3
PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python -m unittest discover -s datasets/locomo/tests 2>&1 | tail -3
PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python -m unittest discover -s datasets/travelplanner/tests 2>&1 | tail -3

# 4) main 上 --no-ff 合并（不经 cd，用 -C 操作主检出）
git -C /Users/xu/git/oak merge --no-ff wiki-experience-phase-one-20261005 -m "Merge wiki-experience-phase-one-20261005：Wiki 循环记忆层两缺口修复与三轮审查验证

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
MERGE_SHA=$(git -C /Users/xu/git/oak rev-parse HEAD)
echo "main merge commit: $MERGE_SHA"

# 5) 合并后 main 验证（身份抽查＋回归）
git -C /Users/xu/git/oak status --short | head -20
PYTHONPATH=. /Users/xu/git/oak/.venv/bin/python - <<'EOF'
import subprocess, sys
for suite in ('tests', 'datasets/locomo/tests', 'datasets/travelplanner/tests'):
    r = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', suite],
                       cwd='/Users/xu/git/oak', capture_output=True, text=True)
    tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''
    print(suite, '->', tail)
EOF
echo "全部完成：Wiki=$WIKI_SHA Merge=$MERGE_SHA（不 push）"
