"""Explicit dataset paths and run-local memory preparation, independent of old runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from darwinagent.agents import ExtractionAgent
from darwinagent.contracts import MemoryResult
from darwinagent.runtime.artifacts import atomic_json, digest
from darwinagent.runtime.identity import transport_identity
from darwinagent.runtime.leases import execution_lease
from darwinagent.runtime.workspace import Workspace
from darwinagent.schema.model import Schema

ROOT = Path(__file__).resolve().parents[2]
FACT_GUIDANCE = "从原始消息抽取完整、自足的原子事实。依据 speaker 识别第一人称；保留否定、计划和时间精度。不要读取问题、答案或评测标注。"


def dataset_dir(value=None):
    if not value:
        raise ValueError("Specify --dataset-repo or --data-dir; no implicit local dataset")
    root = Path(value).resolve()
    for name in ("locomo10_zh.json", "locomo10.json"):
        if not (root / name).is_file():
            raise ValueError(f"LoCoMo dataset missing {root / name}")
    return root


def add_dataset_arguments(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset-repo", help="HF 数据集仓库，如 justis-xu/memory-eval-zh")
    source.add_argument("--data-dir", help="显式使用本地数据目录（可选替代 HF）")
    parser.add_argument(
        "--dataset-revision", default="main", help="HF 分支、标签或提交；首次加载后锁定实际提交"
    )
    parser.add_argument("--dataset-subdir", default="locomo", help="HF 仓库内原始数据目录")
    parser.add_argument("--dataset-cache", help="可选 HF 缓存位置，与数据身份无关")
    parser.add_argument("--dataset-offline", action="store_true", help="仅使用缓存；需要锁定的提交")


def resolve_dataset(args, output):
    """Resolve only the commanded source; persist logical identity, never cache location.

    A mutable HF reference is resolved once. Continuations download by that immutable
    commit even when its branch moves. Only the two raw files are allowed, excluding
    QA-derived slots and old experiment artifacts.
    """
    repo = getattr(args, "dataset_repo", None)
    local = getattr(args, "data_dir", None)
    if bool(repo) == bool(local):
        raise ValueError("Specify exactly one of --dataset-repo or --data-dir")
    target = Path(output) / "dataset-source.json"
    previous = json.loads(target.read_text()) if target.exists() else None
    names = ("locomo10_zh.json", "locomo10.json")
    if repo:
        revision = getattr(args, "dataset_revision", "main")
        subdir = getattr(args, "dataset_subdir", "locomo").strip("/")
        if ".." in PurePosixPath(subdir).parts:
            raise ValueError("Dataset subdirectory must stay inside the HF repository")
        source = {
            "kind": "huggingface",
            "repo_id": repo,
            "requested_revision": revision,
            "subdir": subdir,
        }
        if previous and previous["source"] != source:
            raise ValueError("Dataset source changed; use a new output directory")
        try:
            from huggingface_hub import HfApi, hf_hub_download
        except ImportError as exc:
            raise RuntimeError("HF input requires darwinagent[benchmarks]") from exc
        offline = bool(getattr(args, "dataset_offline", False))
        commit = previous["resolved_revision"] if previous else revision
        is_commit = len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
        if not is_commit:
            if offline:
                raise ValueError(
                    "Offline HF input requires a full commit or existing dataset-source.json"
                )
            commit = HfApi().dataset_info(repo, revision=revision).sha
        paths = [
            Path(
                hf_hub_download(
                    repo_id=repo,
                    repo_type="dataset",
                    filename=str(PurePosixPath(subdir) / name),
                    revision=commit,
                    cache_dir=getattr(args, "dataset_cache", None),
                    local_files_only=offline,
                )
            )
            for name in names
        ]
        root = dataset_dir(paths[0].parent)
        record = {"source": source, "resolved_revision": commit}
    else:
        root = dataset_dir(local)
        paths = [root / name for name in names]
        # Local opt-in is content addressed too, so moving identical files is harmless.
        record = {"source": {"kind": "local"}, "resolved_revision": None}
    record["files"] = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in zip(names, paths)
    }
    if previous and previous != record:
        raise ValueError("Dataset contents changed; use a new output directory")
    atomic_json(target, record)
    return root


async def prepare_memory(case, root, client, config):
    """Extract once without questions/gold; S/P graph revisions reuse these frozen facts.

    This package declares no vector model. An empty index is explicit graph-only input;
    an imported memory package can instead provide its own frozen vector index.
    """
    root = Path(root) / case.id
    root.mkdir(parents=True, exist_ok=True)
    policy = (ROOT / "tasks/conversation_memory/assets/S/schema.yaml").read_text()
    identity = digest(
        {
            "corpus": [b.to_dict() for b in case.corpus],
            "policy": policy,
            "guidance": FACT_GUIDANCE,
            "config": config.to_dict(),
            "transport": transport_identity(client),
        }
    )
    workspace = Workspace(root.parent / "workspace")
    with execution_lease(root / "prepare.lock"):
        if (root / "manifest.json").exists():
            manifest = json.loads((root / "manifest.json").read_text())
            if manifest.get("preparation_identity") != identity:
                raise ValueError("Memory preparation identity changed; use a new memory directory")
            rows = [json.loads(x) for x in (root / "facts.jsonl").read_text().splitlines() if x]
            if digest(rows) != manifest["facts_digest"]:
                raise ValueError("Prepared facts changed")
            return manifest
        runtime = SimpleNamespace(
            schema=Schema.from_yaml(policy),
            bundle=SimpleNamespace(version=identity),
            prompt=lambda role: FACT_GUIDANCE,
        )
        memory_path = root / "memory.json"
        if memory_path.exists():
            saved = json.loads(memory_path.read_text())
            if saved["identity"] != identity:
                raise ValueError("Incomplete memory preparation identity changed")
            memory = MemoryResult.from_dict(saved["memory"], {b.source.id: b for b in case.corpus})
        else:
            memory = await ExtractionAgent(
                runtime,
                client,
                config,
                "facts_" + identity[:16],
                journal_root=root / "steps",
                workspace=workspace,
            ).extract(case.corpus)
            atomic_json(memory_path, {"identity": identity, "memory": memory.to_dict()})
        locations = {b.source.id: b.source.location for b in case.corpus}
        rows = [
            {
                "fid": f.id,
                "statement": f.text,
                "subject": f.subject.name,
                "ftype": f.modality + ":" + f.polarity,
                "date_iso": f.time.start,
                "date_raw": f.time.raw,
                "date_precision": f.time.precision,
                "sources": sorted({locations[e.source_id] for e in f.evidence}),
                "mentions": [f.object_entity.name] if f.object_entity else [],
                "topics": [],
                "atomic_fact": f.to_dict(),
            }
            for f in memory.facts
        ]
        # Atomic replacement: a crash leaves the durable model receipts and memory.json reusable.
        temp = root / "facts.jsonl.tmp"
        temp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        temp.replace(root / "facts.jsonl")
        (root / "vector").mkdir(exist_ok=True)
        (root / "vector/index.jsonl").write_text("")
        manifest = {
            "conversation": case.id,
            "preparation_identity": identity,
            "facts_digest": digest(rows),
            "vector_digest": digest([]),
            "graph_digest": digest(None),
            "n_facts": len(rows),
            "n_nodes": 0,
            "n_vector_records": 0,
            "memory_id_field": "编号",
            "vector_mode": "none",
            "source": "raw_dialogue_extraction",
            "corpus_digest": digest([b.to_dict() for b in case.corpus]),
        }
        manifest["snapshot_digest"] = digest(manifest)
        atomic_json(root / "manifest.json", manifest)
        return manifest
