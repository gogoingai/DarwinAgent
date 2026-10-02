"""Disjoint train/validation/test reruns using the untouched grading stack."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path

from oak.kernel.harness import Harness
from oak.llm.client import LLMClient
from oak.runtime import atomic_json, digest, verify_files

from .config import load_locomo_config
from .data import load_conversation, list_conversations

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "datasets/locomo/runs"
CANONICAL = RUNS / "portable_v1"
DEFAULT_SPLITS = {
    "train": ["conv-26", "conv-30", "conv-41", "conv-42", "conv-43", "conv-44"],
    "validation": ["conv-47", "conv-48"],
    "test": ["conv-49", "conv-50"],
}
# 2026-10 迭代协议：单对话三集合（conv-26 训练 / conv-47 验证 / conv-49 测试）
SINGLE3_SPLITS = {"train": ["conv-26"], "validation": ["conv-47"], "test": ["conv-49"]}
PROTOCOLS = {"default": DEFAULT_SPLITS, "single3": SINGLE3_SPLITS}
HISTORICALLY_EXPOSED = ["conv-26", "conv-30", "conv-44"]


def frozen_check(campaign=CANONICAL):
    lock = campaign / "evaluation_frozen.json"
    if not lock.exists():
        # 新实验根目录首次使用：从规范根目录复制同一份评测锁（锁的是共享的
        # 评测代码/数据文件，内容逐字节一致，不构成任何评测规则变更）
        canonical = CANONICAL / "evaluation_frozen.json"
        if not canonical.exists():
            raise FileNotFoundError(canonical)
        atomic_json(lock, json.loads(canonical.read_text()))
    expected = json.loads(lock.read_text())
    verify_files(ROOT, expected)
    return expected


def prepare(campaign=CANONICAL, splits=None):
    splits = splits or DEFAULT_SPLITS
    frozen = frozen_check(campaign)
    lc = load_locomo_config()
    ids = list_conversations(lc.dataset_path)
    flat = [cid for split in splits.values() for cid in split]
    # 默认协议须恰好划分全集；子集协议（如 single3）只要求互异且均在数据集内
    if not (len(flat) == len(set(flat)) and all(cid in ids for cid in flat)
            and (sorted(flat) == sorted(ids) or set(flat) < set(ids))):
        raise ValueError("Split must partition the dataset exactly once or be a disjoint subset")
    data = {"version": 1, "unit": "conversation", "splits": splits,
            "dataset": hashlib.sha256(lc.dataset_path.read_bytes()).hexdigest(),
            "historically_exposed_train": [c for c in HISTORICALLY_EXPOSED if c in flat],
            "n": {split: sum(len(load_conversation(lc.dataset_path, cid).qas) for cid in convs)
                  for split, convs in splits.items()},
            "target": {"metric": "frozen_closeout_system_side_count / all_questions", "strictly_less_than": 0.03},
            "frozen_evaluation": frozen,
            "source_layers": ["message_text", "observation", "event_summary"]}
    path = campaign / "splits.json"
    if path.exists() and json.loads(path.read_text()) != data:
        raise ValueError("Existing split contract differs")
    atomic_json(path, data)
    seed = ROOT / "datasets/locomo/runs/frozen"
    target = campaign / "seed"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("schema.yaml", "topics.json"):
        destination = target / name
        if not destination.exists():
            destination.write_bytes((seed / name).read_bytes())
    return data


def config(round_name, mode, fast_profile="configured", campaign=CANONICAL):
    lc = load_locomo_config()
    lc.cfg.work_dir = campaign / round_name
    lc.evaluation_concurrency = 4
    if fast_profile == "native":
        lc.cfg.fast_base_url = lc.cfg.api_base_url
        lc.cfg.fast_api_key = lc.cfg.api_key
        lc.cfg.model_fast = "glm-5.3-flash"
    frozen = lc.runs_dir / "frozen"
    frozen.mkdir(parents=True, exist_ok=True)
    for name in ("schema.yaml", "topics.json"):
        target = frozen / name
        if not target.exists():
            target.write_bytes((campaign / "seed" / name).read_bytes())
    if mode == "coverage":
        lc.harness = Harness(version="coverage-v4", retrieval_mode="coverage", answer_mode="structured",
            context_limit=100, supplemental_limit=30, completion_tokens=6144,
            review_tokens=6144, candidate_temperatures=(0.0, 0.4), max_steps=6,
            refusal_recheck=False, answer_thinking=False, requirements_review=True)
        lc.cfg.thinking_disabled_roles.add("locomo_answer")
        lc.cfg.role_tiers["locomo_review"] = "strong"
    return lc


def selection_identity(lc):
    pipe = Path(__file__).resolve().parent
    sources = list((ROOT / "oak").rglob("*.py")) + list((ROOT / "oak_domains").rglob("*.py"))
    sources += list(pipe.rglob("*.py"))
    return {"sources": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
            "schema": hashlib.sha256((lc.runs_dir / "frozen/schema.yaml").read_bytes()).hexdigest(),
            "topics": hashlib.sha256((lc.runs_dir / "frozen/topics.json").read_bytes()).hexdigest(),
            "harness": lc.harness.to_dict(),
            "request_policy": {"roles": lc.cfg.role_tiers, "thinking_disabled": sorted(lc.cfg.thinking_disabled_roles),
                               "reasoning_buffer": lc.cfg.reasoning_buffer,
                               "external_reasoning_buffer": lc.cfg.external_reasoning_buffer},
            "models": {"strong": lc.cfg.model_strong, "fast": lc.cfg.model_fast,
                       "strong_endpoint": lc.cfg.api_base_url, "fast_endpoint": lc.cfg.fast_base_url}}


def freeze_round(lc):
    identity = selection_identity(lc)
    path = lc.runs_dir / "generation_contract.json"
    if path.exists() and json.loads(path.read_text()) != identity:
        raise ValueError("Round sources changed; use a new round name")
    for relative, expected in identity["sources"].items():
        source = ROOT / relative
        target = lc.runs_dir / "frozen_code" / relative
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("Frozen source snapshot mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for source in Path(__file__).resolve().parent.rglob("*.py"):
        target = lc.runs_dir / "frozen_code" / source.relative_to(ROOT)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    atomic_json(path, identity)
    return identity


def select(args, campaign):
    contract = prepare(campaign, PROTOCOLS[args.protocol])
    lc = config(args.round, args.mode, args.fast_profile, campaign)
    validation = json.loads((lc.runs_dir / "validation_summary.json").read_text())
    if validation["partial"] or validation["n"] != contract["n"]["validation"]:
        raise ValueError("Complete validation required before selecting")
    if any(value["evaluation_errors"] for value in validation["by_conversation"].values()):
        raise ValueError("Cannot select an incompletely evaluated configuration")
    lock = campaign / "selected.json"
    if lock.exists():
        raise ValueError("Test configuration already selected; do not silently replace it")
    atomic_json(lock, {"round": args.round, "mode": args.mode, "fast_profile": args.fast_profile,
                      "identity": selection_identity(lc), "validation": validation})


async def attribute(lc, conv_id, tag, client):
    # Call the original classifier with its original prompt/rules. Its verdicts
    # remain unchanged; a new report only records the user's <3% acceptance target.
    from .closeout import classify_one
    conv = load_conversation(lc.dataset_path, conv_id)
    out_dir = lc.conv_dir(conv_id) / tag
    failures = [json.loads(line) for line in (out_dir / "failures.jsonl").read_text().splitlines() if line.strip()]
    semaphore = asyncio.Semaphore(4)
    async def one(f):
        async with semaphore:
            return await classify_one(client, conv, f, f"lc{conv_id.replace('conv-', '')}_closeout_attr")
    rows = await asyncio.gather(*(one(f) for f in failures))
    report = json.loads((out_dir / "report.json").read_text())
    count = sum(r["verdict"] == "评测外" for r in rows)
    value = {"n": report["n"], "system_side_count": count, "rate": count / report["n"],
             "target_met": count * 100 < 3 * report["n"], "rows": rows,
             "evaluation_errors": report.get("evaluation_errors", 0),
             "exact": report["exact"], "exact_rate": report["exact_rate"]}
    # A run with incomplete grading cannot establish attainment.
    value["target_met"] = value["target_met"] and value["evaluation_errors"] == 0
    atomic_json(out_dir / "system_side.json", value)
    return value


async def run(args):
    campaign = RUNS / args.root
    splits = PROTOCOLS[args.protocol]
    prepare(campaign, splits)
    frozen_check(campaign)
    lc = config(args.round, args.mode, args.fast_profile, campaign)
    if (lc.cfg.tier_for("locomo_judge") != "strong" or "locomo_judge" in lc.cfg.thinking_disabled_roles
            or lc.cfg.reasoning_buffer != 3072 or lc.cfg.max_retries != 5):
        raise ValueError("Frozen evaluator request policy changed")
    freeze_round(lc)
    if args.split == "test":
        lock = campaign / "selected.json"
        if not lock.exists():
            raise ValueError("Freeze a validation-selected configuration before test")
        selected = json.loads(lock.read_text())
        if selected["round"] != args.round or selected["mode"] != args.mode or selected["fast_profile"] != args.fast_profile:
            raise ValueError("Test configuration differs from selected configuration")
        if digest(selected["identity"]) != digest(selection_identity(lc)):
            raise ValueError("Selected generation assets changed before testing")
    from .runner import eval_conversation
    split_convs = splits[args.split]
    convs = split_convs
    if args.only:
        if args.only not in convs:
            raise ValueError("Requested conversation is outside this split")
        convs = [args.only]
    results = {}
    async with LLMClient(lc.cfg) as client:
        for conv_id in convs:
            frozen_check(campaign)
            conv = load_conversation(lc.dataset_path, conv_id)
            indices = None
            if args.limit:
                if args.split != "train":
                    raise ValueError("Partial QA runs are allowed for training smoke checks only")
                # Deterministic first examples per category, independent of answers.
                groups = {}
                for q in conv.qas:
                    groups.setdefault(q.category, []).append(q.idx)
                indices = set()
                for offset in range(args.limit):
                    for category in sorted(groups):
                        if offset < len(groups[category]) and len(indices) < args.limit:
                            indices.add(groups[category][offset])
            tag = "smoke" if args.limit else args.split
            print(f"START {args.round} {args.split} {conv_id} tag={tag}", flush=True)
            await eval_conversation(lc, client, conv_id, tag=tag, idx_filter=indices)
            frozen_check(campaign)
            value = await attribute(lc, conv_id, tag, client)
            frozen_check(campaign)
            results[conv_id] = value
            n = sum(r["n"] for r in results.values())
            errors = sum(r["system_side_count"] for r in results.values())
            summary = {"round": args.round, "mode": args.mode, "fast_profile": args.fast_profile, "split": args.split,
                       "protocol": args.protocol, "root": campaign.name,
                       "partial": bool(args.limit or (args.only and len(split_convs) > 1)),
                       "n": n,
                       "system_side_count": errors, "rate": errors / n,
                       "target_met": not (args.limit or (args.only and len(split_convs) > 1)) and all(r["evaluation_errors"] == 0 for r in results.values()) and errors * 100 < 3 * n,
                       "by_conversation": results, "frozen_evaluation": frozen_check(campaign)}
            atomic_json(lc.runs_dir / f"{args.split}_summary.json", summary)
            print(f"DONE {conv_id}: system={value['system_side_count']}/{value['n']} exact={value['exact']} target={value['target_met']}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=list(DEFAULT_SPLITS), default="train")
    ap.add_argument("--round", default="r0")
    ap.add_argument("--mode", choices=["legacy", "coverage"], default="coverage")
    ap.add_argument("--fast-profile", choices=["configured", "native"], default="native")
    ap.add_argument("--root", default="portable_v1",
                    help="runs/ 下的实验根目录；portable_v1 为规范多对话划分")
    ap.add_argument("--protocol", choices=list(PROTOCOLS), default="default",
                    help="single3 = conv-26 训练 / conv-47 验证 / conv-49 测试（2026-10 迭代协议）")
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--select", action="store_true")
    args = ap.parse_args()
    campaign = RUNS / args.root
    if args.prepare:
        print(json.dumps(prepare(campaign, PROTOCOLS[args.protocol]), ensure_ascii=False, indent=2))
    elif args.select:
        select(args, campaign)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
