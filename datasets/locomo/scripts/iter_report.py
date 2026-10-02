"""迭代轮次错题分析报告(只读,不属冻结评测面)。

用法:
  uv run python -m datasets.locomo.scripts.iter_report \
      --round-dir datasets/locomo/runs/iter_v2/b0 --conv conv-26 --tag train

输出: 终端摘要 + 同目录 round_report.md(逐题摘要, 供人工诊断)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def clip(text: str, n: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round-dir", required=True)
    ap.add_argument("--conv", required=True)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    out_dir = Path(args.round_dir) / args.conv / args.tag

    report = json.loads((out_dir / "report.json").read_text())
    failures = load_jsonl(out_dir / "failures.jsonl")
    answers = {r["idx"]: r for r in load_jsonl(out_dir / "answers.jsonl")}
    ss_path = out_dir / "system_side.json"
    rows = json.loads(ss_path.read_text())["rows"] if ss_path.exists() else []
    verdict = {r["idx"]: r for r in rows}

    print(f"== {out_dir} ==")
    rate = report['exact_rate']
    print(f"n={report['n']} exact={report['exact']} ({rate if rate is not None else float('nan'):.3f}) "
          f"orig_exact={report.get('orig_exact')} f1={report['f1_avg']:.3f} "
          f"eval_errors={report.get('evaluation_errors', 0)}")
    for cat, v in sorted(report["by_category"].items()):
        cr = v["exact_rate"]
        print(f"  {cat}: {v['exact']}/{v['n']} = {cr if cr is not None else float('nan'):.3f}")
    if ss_path.exists():
        ss = json.loads(ss_path.read_text())
        print(f"评测外 {ss['system_side_count']}/{ss['n']} = {ss['rate']:.4f} "
              f"target_met={ss['target_met']} (上限 {int((ss['n'] * 3 - 1) // 100)} 题严格<3%)")

    kinds = Counter(verdict.get(f["idx"], {}).get("kind", "未归因") for f in failures)
    print("\n-- 错题归因分布(冻结 closeout 口径) --")
    for kind, c in kinds.most_common():
        in_eval = sum(1 for f in failures
                      if verdict.get(f["idx"], {}).get("kind") == kind
                      and verdict.get(f["idx"], {}).get("verdict") == "评测内")
        print(f"  {kind}: {c} (评测内 {in_eval})")

    # 逐题摘要, 按归因 kind 分组
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in failures:
        groups[verdict.get(f["idx"], {}).get("kind", "未归因")].append(f)

    lines = [f"# 轮次错题报告 {out_dir}", "",
             f"n={report['n']} exact={report['exact']} 评测外={sum(1 for r in rows if r['verdict']=='评测外')}", ""]
    for kind in sorted(groups, key=lambda k: -len(groups[k])):
        lines.append(f"## {kind} ({len(groups[kind])})")
        for f in sorted(groups[kind], key=lambda x: x["idx"]):
            v = verdict.get(f["idx"], {})
            a = answers.get(f["idx"], {})
            lines.append(
                f"- **idx{f['idx']}** [{f['category']}] {v.get('verdict','?')} "
                f"grade={f['grade']} status={a.get('status','?')} "
                f"missed={len(f.get('missed_fids') or [])}/{len(f.get('covered_fids') or [])+len(f.get('missed_fids') or [])}"
                f" steps={a.get('n_steps','?')} refused={a.get('refused')}")
            lines.append(f"  - Q: {clip(f['question'], 120)}")
            lines.append(f"  - gold: {clip(f['gold'], 100)}")
            lines.append(f"  - pred: {clip(f['pred'], 200)}")
            lines.append(f"  - judge: {clip(f.get('judge_reason',''), 180)}")
            lines.append(f"  - attr: {clip(v.get('evidence','') or f.get('attribution',''), 180)}")
        lines.append("")
    target = out_dir / "round_report.md"
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n逐题摘要 → {target}")


if __name__ == "__main__":
    main()
