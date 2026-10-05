#!/bin/zsh
# Wiki 合并暂存清单（阶段 5）：只加任务路径，禁止 git add .
# 运行前置：mc3/c26c 完成、三套回归绿、README/报告已更新。
set -e
cd /Users/xu/git/oak/.cache/worktrees/wiki-experience-phase-one

# 代码（修改 24 文件）
git add \
  oak/experiments/runner.py \
  oak/experiments/admission.py \
  oak/experiments/admission_worker.py \
  oak/experiments/bootstrap.py \
  oak/experiments/campaign.py \
  oak/experiments/proposal.py \
  oak/experiments/wiki.py \
  oak/kernel/spec.py \
  oak/kernel/validation.py \
  oak/kernel/checks.py \
  oak/engine/pipeline.py \
  oak/agents/answer.py \
  oak/agents/protocol.py \
  oak/config.py \
  oak/llm/client.py \
  datasets/locomo/evaluator.py \
  datasets/locomo/run.py \
  datasets/locomo/graph_rules.py \
  datasets/locomo/scripts/question_split.py \
  datasets/travelplanner/pipeline/eval/_worker.py \
  datasets/travelplanner/tests/test_gate_a.py \
  tests/integration/test_agentic_fixes.py \
  tests/integration/test_stability_admission.py \
  tasks/travel_planning/task.yaml

# 新增测试与夹具
git add \
  tests/fixtures \
  tests/integration/test_transient_faults.py \
  tests/integration/test_wiki_check_replay.py \
  tests/integration/test_wiki_faults_repro.py \
  tests/integration/test_wiki_optimization.py \
  tests/integration/test_wiki_revision.py \
  tests/integration/test_locomo_graph_rules.py \
  tests/integration/test_fastloop_mode.py \
  datasets/locomo/tests/test_wiki_cli.py \
  datasets/locomo/tests/test_evaluator_subset.py

# 文档与诊断（小文件；不含任何 runs/ 输出）
git add \
  docs/WIKI-PHASE1-VALIDATION.md \
  docs/WIKI-VALIDATION-GOAL-20261005.md \
  docs/WIKI-VALIDATION-RESULTS-20261005.md \
  docs/diagnostics/wiki-validation-20261005 \
  docs/diagnostics/wiki-gap-review-20261005 \
  docs/diagnostics/wiki-gap-recheck-20261005 \
  docs/diagnostics/wiki-gap-recheck3-20261005 \
  docs/diagnostics/wiki-gap-repair-20261005

git status --short | grep -v '^??' || true
echo "=== staged 以上；runs/ 输出一律未暂存 ==="
