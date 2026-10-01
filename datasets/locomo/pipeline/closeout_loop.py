"""收口迭代编排：snapshot / rerun / gate / rollback 原语 + patch-impact 审计。

每轮协议（docs/DESIGN-closeout.md）：
  1) snapshot rN     —— 修复前快照管线资产（评测器三件套除外，它永不改）
  2) （提议器=驱动 agent）做单变量修复
  3) rerun rN        —— 全量重跑 conv-44（新 tag=closeout-rN，fresh 目录；
                        请求级缓存使未受影响调用近零成本；逐题 delta 可见负面效果）
  4) gate rN         —— 归因 + 与 best 比较系统侧计数：严格下降才 ACCEPTED，否则
                        自动 rollback 并记 REJECTED（含 diff 与被拒提案全文）
评测器冻结集（gate 时校验指纹零变化，变了即拒绝该轮）：
  judge.py / prompts/judge.py / protocol.py / lenient_report.py /
  data/gold_repairs.jsonl / data/locomo10_zh.json
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from .config import load_locomo_config
from . import closeout

ROOT = Path(__file__).resolve().parents[3]
PIPE = ROOT / "datasets" / "locomo" / "pipeline"
CONV = "conv-44"
FROZEN = [
    PIPE / "judge.py",
    PIPE / "prompts" / "judge.py",
    PIPE / "protocol.py",
    PIPE / "lenient_report.py",
    ROOT / "datasets" / "locomo" / "data" / "gold_repairs.jsonl",
    ROOT / "datasets" / "locomo" / "data" / "locomo10_zh.json",
]
# 可被修复触碰的资产面（快照/回滚范围）
ASSETS = sorted(
    [p for p in PIPE.rglob("*.py")]
    + [ROOT / "oak" / "llm" / "client.py", ROOT / "oak" / "config.py"]
)


def _cdir() -> Path:
    d = load_locomo_config().runs_dir / "closeout"
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshots").mkdir(exist_ok=True)
    return d


def _state(cdir: Path) -> dict:
    p = cdir / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"round": 0, "best_sys": None, "best_tag": "full", "history": []}


def _save_state(cdir: Path, st: dict) -> None:
    (cdir / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=2))


def _fp(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def snapshot(round_no: int) -> None:
    cdir = _cdir()
    snap = cdir / "snapshots" / f"r{round_no}"
    if snap.exists():
        shutil.rmtree(snap)
    snap.mkdir(parents=True)
    for p in ASSETS:
        dst = snap / p.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    frozen_fps = {str(p.relative_to(ROOT)): _fp(p) for p in FROZEN}
    (snap / "_frozen.json").write_text(json.dumps(frozen_fps, indent=2))
    print(f"snapshot r{round_no}: {len(ASSETS)} 个资产文件 + 冻结集指纹")


async def rerun(round_no: int) -> None:
    from .runner import eval_conversation
    from oak.llm.client import LLMClient
    lc = load_locomo_config()
    client = LLMClient(lc.cfg)
    tag = f"closeout-r{round_no}"
    t0 = time.time()
    rep = await eval_conversation(lc, client, CONV, tag=tag)
    print(f"rerun {tag}: exact={rep.get('exact')}/{rep.get('n')} 用时 {time.time()-t0:.0f}s")


def rollback(round_no: int) -> None:
    snap = _cdir() / "snapshots" / f"r{round_no}"
    for f in snap.rglob("*"):
        if f.is_file() and f.name != "_frozen.json":
            shutil.copy2(f, ROOT / f.relative_to(snap))
    print(f"rollback r{round_no}: 资产已恢复")


def _diff_snapshot(round_no: int) -> str:
    snap = _cdir() / "snapshots" / f"r{round_no}"
    parts = []
    for p in ASSETS:
        old = snap / p.relative_to(ROOT)
        if not old.exists():
            continue
        if p.read_text() == old.read_text():
            continue
        d = difflib.unified_diff(
            old.read_text().splitlines(), p.read_text().splitlines(),
            fromfile=f"a/{p.relative_to(ROOT)}", tofile=f"b/{p.relative_to(ROOT)}", lineterm="")
        parts.append("\n".join(d))
    return "\n\n".join(parts) or "(无差异)"


async def gate(round_no: int, note: str = "") -> dict:
    cdir = _cdir()
    st = _state(cdir)
    tag = f"closeout-r{round_no}"
    # 冻结校验：评测器指纹必须与快照时一致
    snap_frozen = json.loads((cdir / "snapshots" / f"r{round_no}" / "_frozen.json").read_text())
    violated = [p for p, fp in snap_frozen.items() if _fp(ROOT / p) != fp]
    health = "ok"
    if violated:
        health = f"FROZEN-VIOLATION: {violated}"
    out = await closeout.run(CONV, tag)
    sys_now = out["system_side_count"]
    best = st["best_sys"]
    accepted = (best is None and sys_now is not None) or (sys_now < best)
    if health != "ok":
        accepted = False
    diff = _diff_snapshot(round_no)
    prev = None
    prev_p = cdir / f"closeout_{CONV}_{st['best_tag']}.json"
    if prev_p.exists():
        prev = json.loads(prev_p.read_text())
    fixed, broke = [], []
    if prev:
        prev_sys_idx = {i["idx"] for i in prev["system_side"]}
        now_sys_idx = {i["idx"] for i in out["system_side"]}
        fixed = sorted(prev_sys_idx - now_sys_idx, key=int)
        broke = sorted(now_sys_idx - prev_sys_idx, key=int)
    entry = [
        f"\n### round-{round_no} — {'ACCEPTED' if accepted else 'REJECTED'} "
        f"(sys={sys_now}, best={best}, exact={out['exact']}/{out['n']})  {time.strftime('%F %T')}",
        f"Health: {health}",
        f"Proposal (ρ): {note or '(未注明)'}",
        "Diff:",
        "```diff", diff, "```",
        f"Per-question delta vs best({st['best_tag']}): fixed={fixed} broke={broke}",
        f"System-side by kind: {out['system_side_by_kind']}",
        f"Eval-side by kind: {out['eval_side_by_kind']}",
        ("判定: 系统侧计数未严格下降 → 资产已回滚，提案全文留在上方 diff" if not accepted
         else "判定: 采纳，best 前移"),
    ]
    with (cdir / "patch-impact.md").open("a") as f:
        f.write("\n".join(entry) + "\n")
    if accepted:
        st["best_sys"], st["best_tag"] = sys_now, tag
    else:
        rollback(round_no)
    st["round"] = round_no
    st["history"].append({"round": round_no, "sys": sys_now, "best": best,
                          "accepted": accepted, "health": health,
                          "fixed": fixed, "broke": broke, "note": note})
    _save_state(cdir, st)
    print(f"gate r{round_no}: sys={sys_now} best={best} -> {'ACCEPTED' if accepted else 'REJECTED+回滚'}")
    return {"accepted": accepted, "sys": sys_now}


async def init_baseline() -> None:
    """以 full tag 的归因作为 best 基线（round 0）。"""
    cdir = _cdir()
    out = await closeout.run(CONV, "full")
    st = _state(cdir)
    st.update({"round": 0, "best_sys": out["system_side_count"], "best_tag": "full"})
    _save_state(cdir, st)
    print(f"baseline: sys={out['system_side_count']} threshold={out['threshold']} met={out['met']}")


def main() -> None:
    cmd = sys.argv[1]
    if cmd == "snapshot":
        snapshot(int(sys.argv[2]))
    elif cmd == "rerun":
        asyncio.run(rerun(int(sys.argv[2])))
    elif cmd == "gate":
        asyncio.run(gate(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else ""))
    elif cmd == "rollback":
        rollback(int(sys.argv[2]))
    elif cmd == "baseline":
        asyncio.run(init_baseline())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
