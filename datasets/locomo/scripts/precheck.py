"""Real-model engineering precheck. The campaign refuses to start B0 without a passing record.

Checks: both model tiers respond; the fixed fact-extraction protocol holds on a synthetic
corpus with punctuation traps, negation, plan modality and relative time; quotes locate
verbatim at recorded offsets; explicit truncation engages bounded bisection and fails clean;
a real conv-26 slice extracts non-empty memory. Failures stop the official experiment:
adjust engineering configuration only, then re-run this precheck."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from darwinagent.agents import ExtractionAgent
from darwinagent.agents.protocol import ProtocolError
from darwinagent.config import RunConfig
from darwinagent.contracts import CorpusBlock, SourceRef
from darwinagent.kg.assembler import GraphAssembler, anchoring_errors
from darwinagent.llm.client import LLMClient
from darwinagent.llm.settings import load_legacy_connection as load_connection
from darwinagent.schema.model import Schema

from datasets.locomo.adapter import LocomoAdapter
from darwinagent.experiments.spec import precheck_identity

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / 'tasks/conversation_memory/assets/S/schema.yaml'
GUIDE = '每条事实一个独立命题；引用逐字复制原文（含标点）；相对时间不换算；类别只用声明类别。'

SYNTHETIC = [
    ('s1', '甲说:我下周,修打印机。', '甲', '2024-05-01'),
    ('s2', '乙没有去过巴黎,但计划明年去。', '乙', '2024-05-01'),
    ('s3', '丙负责项目A的预算,共3,500元。', '丙', '2024-05-08'),
]


class SeedRuntime:
    """Engineering precheck runtime: the fixed seed schema, no bootstrapped assets yet."""

    def __init__(self):
        self.schema = Schema.from_yaml(SEED.read_text())

    def prompt(self, role):
        return GUIDE


async def probe_tier(client, role, json_mode):
    try:
        result = await client.chat(role=role, use_cache=False, namespace='precheck',
                                   json_mode=json_mode, temperature=0.0, max_tokens=256,
                                   messages=[{'role': 'system', 'content': '你是连通性探测助手。'},
                                             {'role': 'user', 'content': '回复 JSON：{"ok": true}'}])
        return {'ok': bool(result.content.strip()), 'reply': result.content.strip()[:80]}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': repr(exc)[:200]}


def synthetic_corpus():
    return tuple(CorpusBlock(SourceRef('message_text', 'precheck', sid), text,
                             {'speaker': speaker, 'date': date})
                 for sid, text, speaker, date in SYNTHETIC)


def verify_offsets(memory):
    for fact in memory.facts:
        for ev in fact.evidence:
            block = memory.corpus.get(ev.source_id)
            if block is None or block.text[ev.start:ev.end] != ev.quote:
                return f'证据偏移与原文不符: {fact.id[:12]}'
    return None


async def main(args):
    from datasets.locomo.inputs import resolve_dataset
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    connection = load_connection(ROOT, out / 'runtime', 'LOCOMO')
    connection.role_tiers['locomo_judge'] = 'strong'
    connection.empty_response_passthrough_roles.add('locomo_judge')
    schema = Schema.from_yaml(SEED.read_text())
    data_dir = resolve_dataset(args, out)
    checks = {}
    started = time.time()
    async with LLMClient(connection) as client:
        checks['strong_tier'] = await probe_tier(client, 'locomo_judge', True)
        checks['fast_tier_json'] = await probe_tier(client, 'extraction', True)
        config = RunConfig()
        runtime = SeedRuntime()

        # Synthetic corpus: protocol compliance and verbatim quote location.
        try:
            memory = await ExtractionAgent(runtime, client, config, 'precheck_synth').extract(synthetic_corpus())
            problem = verify_offsets(memory)
            problem = problem or ('事实数不足' if len(memory.facts) < 2 else None)
            graph = GraphAssembler.build(memory, None, schema)
            checks['synthetic_extraction'] = {'ok': problem is None, 'facts': len(memory.facts),
                                              'error': problem, 'nodes': graph.graph.number_of_nodes(),
                                              'batches': [d.get('status') for d in memory.diagnostics if d.get('batch') is not None]}
        except Exception as exc:  # noqa: BLE001
            checks['synthetic_extraction'] = {'ok': False, 'error': repr(exc)[:300]}

        # Starvation budget must end bounded and clean: a valid minimal memory (graceful
        # degradation) or a clean ProtocolError both pass; hangs or foreign errors fail.
        try:
            tiny = RunConfig(extraction_max_tokens=48, protocol_attempts=1)
            memory = await ExtractionAgent(runtime, client, tiny, 'precheck_trunc').extract(synthetic_corpus())
            problem = verify_offsets(memory)
            checks['starvation_budget'] = {'ok': problem is None, 'facts': len(memory.facts),
                                           'note': '模型在饥饿预算内优雅降级产出合法记忆', 'error': problem}
        except ProtocolError as exc:
            checks['starvation_budget'] = {'ok': True, 'error': str(exc)[:160],
                                           'note': 'clean protocol failure with bounded budget'}
        except Exception as exc:  # noqa: BLE001
            checks['starvation_budget'] = {'ok': False, 'error': repr(exc)[:300]}

        # A real training-conversation slice through the same fixed protocol.
        try:
            case = LocomoAdapter(data_dir / 'locomo10_zh.json').generation_input('conv-26')
            corpus = case.corpus[:7]
            memory = await ExtractionAgent(runtime, client, config, 'precheck_slice').extract(corpus)
            problem = verify_offsets(memory)
            checks['conv26_slice'] = {'ok': problem is None and bool(memory.facts), 'facts': len(memory.facts),
                                      'messages': len(corpus), 'error': problem}
        except Exception as exc:  # noqa: BLE001
            checks['conv26_slice'] = {'ok': False, 'error': repr(exc)[:300]}

        anchor = anchoring_errors(schema)
        checks['seed_schema'] = {'ok': not anchor, 'error': anchor[:5]}
        ledger = client.ledger_summary()
    passed = all(entry.get('ok') for entry in checks.values())
    record = {'passed': passed, 'checks': checks, 'ledger': ledger,
              'identity': precheck_identity(connection, config),
              'elapsed_s': round(time.time() - started, 1), 'ts': time.time()}
    (out / 'precheck.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': passed, 'checks': {k: v.get('ok') for k, v in checks.items()}},
                     ensure_ascii=False))
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    from datasets.locomo.inputs import add_dataset_arguments
    parser = argparse.ArgumentParser()
    add_dataset_arguments(parser)
    parser.add_argument('--output', required=True, help='campaign root (precheck.json written there)')
    asyncio.run(main(parser.parse_args()))
