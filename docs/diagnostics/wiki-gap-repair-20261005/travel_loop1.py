"""缺口修复后的第一次真实 Travel Wiki 闭环（新目录新身份，不碰旧运行）。

与归档 travel_driver.py 的差异：ExperimentRunner 增加 dynamic_trial=True——
动态图任务（无冻结快照/共享试验图）的 B0 与每轮候选现在必须过同题真图准入
（复用已采纳图或候选重抽），冒烟门开启（单题执行门＋评测完整）。
目的：≥1 次健康的 B0→提案→准入→正式评分→决策回写（准入拦截不计正式轮）。
loop1（保留为失败证据）：首版草案真图抽取一次协议失败即终止冷启动——随后框架
把试验图构建失败转为校验反馈（bootstrap 内重拟草案），loop2 起新根重试。
预算声明（2026-10-05 审查 P2）：function_steps/timeout/result_bytes 全部用框架默认
（30000/2.0s/180KB，不放大）；protocol_attempts=5 为显式重试预算调整，对齐 LoCoMo
参照配置（arm_config 同值）——属重试次数声明，非准入/预算/门槛放宽。
用法：python travel_loop1.py <运行目录名> [--resume]
"""
import asyncio
import json
import sys
from pathlib import Path

from datasets.travelplanner.adapter import TravelPlannerAdapter
from datasets.travelplanner.evaluator import TravelPlannerEvaluator
from oak.config import RunConfig
from oak.experiments import AdoptionPolicy, ExperimentRunner
from oak.kernel import TaskSpec
from oak.llm.settings import load_connection

RUN = Path('datasets/travelplanner/runs') / (sys.argv[1] if len(sys.argv) > 1
                                            else 'wiki_gap_repair_20261005_loop1')


async def main():
    task = Path('tasks/travel_planning')
    RUN.mkdir(parents=True, exist_ok=True)
    conn = load_connection(Path.cwd(), RUN / 'runtime', 'LOCOMO')
    runner = ExperimentRunner(
        TravelPlannerAdapter(),
        lambda client, stage: TravelPlannerEvaluator(stage / 'evaluation'),
        conn, RunConfig(protocol_attempts=5), AdoptionPolicy('final', 'macro_cs'),
        RUN / 'train', optimization_mode='wiki', wiki_call_limit=10,
        frozen_files=(task, Path('datasets/travelplanner/adapter.py'),
                      Path('datasets/travelplanner/evaluator.py')),
        dynamic_trial=True)
    spec = TaskSpec.load(task / 'task.yaml')
    summary = await runner.run('train:0', spec, rounds=2, scope=('S', 'F', 'C', 'P'),
                               resume=len(sys.argv) > 2 and sys.argv[2] == '--resume')
    print(json.dumps({'status': summary.get('status'),
                      'rounds': [{k: d.get(k) for k in ('stage', 'accepted', 'reasons')}
                                 for d in summary.get('rounds', [])],
                      'adopted': summary.get('adopted_version')},
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
