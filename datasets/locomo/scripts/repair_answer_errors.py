"""answer_error 外科修复:从作答产物中摘除执行失败行,使断点续跑重新作答。

answer_all 会把 status!=ok 的行写入 answers.jsonl 并计入 checkpoint done,
重跑同命令不会补答它们;本脚本删掉这些行并同步 checkpoint,随后重跑同一
campaign 命令即可补答。只动运行产物,不碰任何评测文件。

用法:
  uv run python -m datasets.locomo.scripts.repair_answer_errors \
      --dir datasets/locomo/runs/iter_v2/b0/conv-26/train
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="含 answers.jsonl/checkpoint.json 的作答目录")
    args = ap.parse_args()
    d = Path(args.dir)
    rows = [json.loads(l) for l in (d / "answers.jsonl").read_text().splitlines() if l.strip()]
    bad = sorted({r["idx"] for r in rows if r.get("status") != "ok"})
    if not bad:
        print("no answer_error rows; nothing to do")
        return
    keep = [r for r in rows if r.get("status") == "ok"]
    ck = json.loads((d / "checkpoint.json").read_text())
    ck["done"] = [i for i in ck["done"] if i not in bad]
    (d / "answers.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in keep) + "\n")
    (d / "checkpoint.json").write_text(json.dumps(ck, ensure_ascii=False, indent=2))
    print(f"removed {len(bad)} rows: {bad}; re-run the same campaign command to re-answer them")


if __name__ == "__main__":
    main()
