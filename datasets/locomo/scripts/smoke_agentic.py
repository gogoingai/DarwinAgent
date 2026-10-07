"""Agentic-round smoke: REAL cold-start bootstrap (structure sample in, atomic-memory floor
enforced) followed by a 5-question run of BOTH arms over the frozen conv-26 snapshot.

Usage: uv run python -m datasets.locomo.scripts.smoke_agentic --output datasets/locomo/runs/agentic_v1/smoke [--questions 5]"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from darwinagent.config import RunConfig
from darwinagent.contracts import CaseInput
from darwinagent.experiments.bootstrap import AssetBootstrapper
from darwinagent.kernel import TaskSpec
from darwinagent.llm.client import LLMClient

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.run import ROOT, SNAPSHOTS, TASK_DIR, arm_config, connection, memory_structure_sample


async def main(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    task = TaskSpec.load(TASK_DIR / 'task.yaml')
    adapter = LocomoAdapter(ROOT / 'datasets/locomo/data/locomo10_zh.json')
    case_full = adapter.generation_input('conv-26')
    case = CaseInput(case_full.id, case_full.corpus, case_full.questions[:args.questions])
    conn = connection(out)

    async with LLMClient(conn) as client:
        # 1) 真实冷启动 bootstrap：S 从零生成（唯一硬约束＝原子记忆内核）
        structure = memory_structure_sample(SNAPSHOTS / 'conv-26')
        bundle = await AssetBootstrapper().initialize(case, task, client, RunConfig(function_timeout_s=15.0),
                                                      out / 'assets', structure_sample=structure)
        schema_yaml = next(a.content for a in bundle.assets.assets if a.kind == 'S')
        print(json.dumps({'stage': 'bootstrap', 'version': bundle.version,
                          'assets': sorted((a.kind, a.id) for a in bundle.assets.assets)},
                         ensure_ascii=False))
        (out / 'bootstrapped-schema.yaml').write_text(schema_yaml)

        from darwinagent.engine import Pipeline
        for arm in ('g1', 'v0'):
            config = arm_config(arm, args.vector_k)
            result = await Pipeline(client, out / arm / 'generation',
                                    frozen_snapshot=SNAPSHOTS / 'conv-26').run(
                case, task.with_bundle(bundle), config)
            statuses = [a.status for a in result.answers]
            print(json.dumps({'stage': 'smoke', 'arm': arm,
                              'statuses': statuses,
                              'memory_count': result.memory_count,
                              'graph_nodes': result.graph_nodes,
                              'errors': [a.error[:120] for a in result.answers if a.error][:2]},
                             ensure_ascii=False))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--questions', type=int, default=5)
    p.add_argument('--vector-k', type=int, default=30)
    asyncio.run(main(p.parse_args()))
