"""conv-30/41/42 各 2 题（0,20）独立 Wiki 循环（缺口修复后，新目录新身份）。

结构与归档 multi_driver.py 相同：训练每对话 2 题，冒烟用原 6 题（0,1,2,3,4,20）
与原 2/6 门——不缩门不降阈；经验与 conv-26 相互隔离。
"""
import asyncio
import dataclasses
import json
from pathlib import Path

from oak.experiments import AdoptionPolicy, ExperimentRunner
from oak.experiments.snapshots import attach_vector, load_frozen_graph
from oak.kernel import TaskSpec
from oak.runtime.artifacts import atomic_json

from datasets.locomo.adapter import LocomoAdapter
from datasets.locomo.evaluator import LocomoEvaluator
from datasets.locomo.run import (SNAPSHOTS, TASK_DIR, arm_config, connection,
                                 frozen_files, memory_structure_sample, smoke_judge)

ROOT = Path('datasets/locomo/runs/wiki_gap_repair_20261005_mc4').resolve()
IDS = ('conv-30', 'conv-41', 'conv-42')


async def main():
    original = LocomoAdapter(Path('datasets/locomo/data/locomo10_zh.json'))

    class Adapter:
        def generation_input(self, cid):
            c = original.generation_input(cid)
            return dataclasses.replace(c, questions=(c.questions[0], c.questions[20]))

    adapter = Adapter()
    conn = connection(ROOT)
    conn.max_concurrency = 2
    conn.fast_max_concurrency = 2
    config = dataclasses.replace(arm_config('g1'), concurrency=2)
    graph = load_frozen_graph(SNAPSHOTS / IDS[0], adapter.generation_input(IDS[0]).corpus)
    attach_vector(graph, SNAPSHOTS / IDS[0])
    atomic_json(ROOT / 'plan.json', {
        'purpose': 'Wiki layer multi-conversation loops after gap repair (phase 2)',
        'cases': [{'id': cid, 'questions': [q.to_dict() for q in adapter.generation_input(cid).questions]}
                  for cid in IDS], 'rounds': 2, 'questions_per_case': 2,
        'full_question_set': False, 'official_holdouts_used': False,
        'wiki_isolated_from_conv26': True,
        'smoke_question_ids': [0, 1, 2, 3, 4, 20], 'smoke_gate': 'original 2/6 threshold'})

    class SmallRunner(ExperimentRunner):
        async def _smoke_gate(self, cases, spec, candidate=False, questions_per_case=6):
            expanded = [dataclasses.replace(original.generation_input(c.id), questions=tuple(
                original.generation_input(c.id).questions[i] for i in (0, 1, 2, 3, 4, 20)))
                for c in cases]
            return await super()._smoke_gate(expanded, spec, candidate=candidate, questions_per_case=6)

    runner = SmallRunner(adapter, lambda client, p: LocomoEvaluator(client, p), conn, config,
                         AdoptionPolicy('original_precise', ('original_lenient',)), ROOT / 'train',
                         frozen_files(), snapshot_root=SNAPSHOTS,
                         bootstrap_context=memory_structure_sample(SNAPSHOTS / IDS[0]),
                         bootstrap_trial_graph=graph, smoke_judge=smoke_judge,
                         optimization_mode='wiki', wiki_call_limit=15)
    summary = await runner.run(IDS, TaskSpec.load(TASK_DIR / 'task.yaml'), rounds=2,
                               scope=('S', 'F', 'C', 'P'))
    print(json.dumps({'status': summary.get('status'),
                      'rounds': [{k: d.get(k) for k in ('stage', 'accepted', 'reasons')}
                                 for d in summary.get('rounds', [])],
                      'adopted': summary.get('adopted_version')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
