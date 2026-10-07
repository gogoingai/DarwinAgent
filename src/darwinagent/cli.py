"""Installed command-line interface for a bounded, inspectable example."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from .config import Config


def _parser():
    parser = argparse.ArgumentParser(prog='darwinagent', description='Evolution for the Agent Era')
    parser.add_argument('--version', action='version', version='darwinagent 0.1.0')
    sub = parser.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo', help='Run a tiny maintenance task through Pipeline and Wiki optimization')
    demo.add_argument('--mode', choices=('replay', 'live'), default='replay')
    demo.add_argument('--rounds', type=int, default=2)
    demo.add_argument('--output', type=Path, default=Path('runs/demo'))
    demo.add_argument('--resume', action='store_true')
    demo.add_argument('--max-requests', type=int, default=40, help='Maximum actual HTTP attempts, including retries')
    demo.add_argument('--timeout', type=float, default=1800, help='Whole run time limit in seconds')
    demo.add_argument('--model-profiles', action='store_true', help='Opt in to provider-specific request parameters')
    doctor = sub.add_parser('doctor', help='Check installation offline; explicitly opt in to an endpoint probe')
    doctor.add_argument('--check-model', action='store_true')
    doctor.add_argument('--output', type=Path, default=Path('runs'), help='Artifact directory to check for write access')
    return parser


async def _doctor(check_model, output):
    from .demo import TASK_ROOT
    from .kernel import TaskSpec
    from .kernel.registration import load_assets
    task = TaskSpec.load(TASK_ROOT/'task.yaml')
    assets = load_assets(TASK_ROOT)
    if not task.name or not assets.assets:
        raise ValueError('Installed demo resources are incomplete; reinstall darwinagent')
    print('Installation OK: darwinagent 0.1.0; packaged task and assets available.')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='doctor-write-', dir=output) as probe:
        (Path(probe)/'probe').write_text('ok')
    print('Output directory OK: writable.')
    readiness = Config.from_env()
    try:
        readiness.validate_model()
        print('Live model configuration: ready (no network check performed).')
    except ValueError:
        print('Live model configuration: incomplete; replay is ready. Set DARWINAGENT_BASE_URL, DARWINAGENT_MODEL, DARWINAGENT_API_KEY for live mode.')
    if check_model:
        from .llm.client import LLMClient
        with tempfile.TemporaryDirectory(prefix='darwinagent-doctor-') as tmp:
            cfg = Config.from_env(work_dir=tmp)
            cfg.validate_model()
            cfg.max_retries = 1
            cfg.max_http_requests = 1
            cfg.request_timeout_s = 30
            cfg.deadline_monotonic = time.monotonic()+30
            async with LLMClient(cfg) as client:
                result = await client.chat(role='answer', messages=[{'role': 'user', 'content': 'Reply OK.'}],
                                           max_tokens=16, use_cache=False, namespace='doctor')
                if not result.content.strip():
                    raise ValueError('Endpoint returned an empty model response')
            print('Model endpoint OK: one authenticated Chat Completions response received.')
    return 0


async def _demo(args):
    from .demo import run_demo
    if args.rounds < 0 or args.rounds > 10:
        raise ValueError('--rounds must be between 0 and 10')
    if args.max_requests < 1 or args.timeout <= 0:
        raise ValueError('--max-requests and --timeout must be positive')
    cfg = Config.from_env(work_dir=args.output)
    cfg.model_profiles = args.model_profiles or os.environ.get('DARWINAGENT_MODEL_PROFILES', '').lower() == 'true'
    if cfg.model_profiles:
        cfg.thinking_disabled_roles.update(cfg.role_tiers)
    cfg.max_retries = 2
    cfg.max_http_requests = args.max_requests
    cfg.request_budget_path = args.output.resolve()/'http_attempts.json'
    cfg.deadline_monotonic = time.monotonic()+args.timeout
    if args.mode == 'live':
        cfg.validate_model()
    if args.mode == 'replay':
        print('Replay demonstrates mechanisms, not measured model performance. No network calls.')
    async with asyncio.timeout(args.timeout):
        result = await run_demo(args.output, mode=args.mode, rounds=args.rounds, resume=args.resume, config=cfg)
    attempts = json.loads(cfg.request_budget_path.read_text()).get('attempts', 0) if cfg.request_budget_path.exists() else 0
    print(json.dumps({'mode': args.mode, 'status': result['status'], 'output': str(args.output.resolve()),
        'decisions': [{'round': index, 'accepted': row['accepted'], 'reasons': row['reasons']}
                      for index, row in enumerate(result['rounds'], 1)], 'http_attempts': attempts}, ensure_ascii=False))
    return 0 if result['status'] == 'complete' else 1


def main(argv=None):
    args = _parser().parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(Path.cwd()/".env")
    try:
        return asyncio.run(_doctor(args.check_model, args.output) if args.command == 'doctor' else _demo(args))
    except (Exception, KeyboardInterrupt) as exc:
        # Keep provider responses/credentials out of console output.
        message = str(exc) if isinstance(exc, (ValueError, FileExistsError)) else type(exc).__name__
        for name in ('DARWINAGENT_API_KEY',):
            key = os.environ.get(name, '')
            if key:
                message = message.replace(key, '[redacted]')
        print(f'Error: {message}. Inspect the output artifacts for details; verify configuration and limits.',
              file=__import__('sys').stderr)
        return 1
