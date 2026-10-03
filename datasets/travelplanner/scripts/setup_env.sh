#!/usr/bin/env bash
# 阶段0：数据落盘 + 环境自检（Java/database 已由前置步骤就位）
set -euo pipefail
cd "$(dirname "$0")/../../.."
mkdir -p datasets/travelplanner/data
uv run python - <<'EOF'
from datasets.travelplanner.pipeline.config_task import TPConfig
from datasets.travelplanner.pipeline.data.queries import load_queries, partition_train, stratified_test_subset
cfg = TPConfig()
for split in ("train", "validation"):
    qs = load_queries(split, cfg)
    print(split, len(qs), "queries")
groups = partition_train(cfg.train_per_round, cfg.rounds, cfg.seed, cfg)
test = stratified_test_subset(cfg.test_size, cfg.seed, cfg)
print("round groups:", groups)
print("test subset:", test[:10], "... total", len(test))
EOF
echo "setup_env done"
