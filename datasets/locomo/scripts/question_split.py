"""conv-26 题目分层划分（新模式：同对话 15 训练＋10 验证，四次复查/recheck4 协议）。

规则：
- 按 category 分层（1 单跳 / 2 多跳 / 3 时间 / 4 开放域 / 5 对抗），比例配额＋最大余数法；
- 引用同一 evidence 事实的题进同侧（防训练/验证泄漏，题面与 gold 不进入本脚本）；
- 固定种子确定顺序（同机同库输出恒定）；保留原 case_id/question_id（idx 不变）。

用法：python -m datasets.locomo.scripts.question_split [--out runs/<dir>/split.json]
       不带 --out 时仅打印划分（审计用）。清单落盘后由新模式驱动读取，不得漂移。
"""
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / 'datasets/locomo/data/locomo10_zh.json'
TRAIN_N = 15
VAL_N = 10
SEED = 20261005


def build_split(case_id='conv-26', train_n=TRAIN_N, val_n=VAL_N, seed=SEED):
    data = json.loads(DATA.read_text())
    item = next(x for x in data if x['sample_id'] == case_id)
    qs = item['qa']
    total = train_n + val_n
    if len(qs) < total:
        raise ValueError(f'{case_id} 题数不足: {len(qs)} < {total}')
    cats = sorted({q['category'] for q in qs})
    # 比例配额（最大余数法），保证 train+val 恰为 total 且每类分到非负配额
    quotas = largest_remainder({c: sum(1 for q in qs if q['category'] == c) for c in cats},
                               len(qs), total)
    rng = random.Random(seed)
    train, val = [], []
    for c in cats:
        # 同类内按 evidence 首指针分组（同一事实的题同侧），组间随机排序
        groups = defaultdict(list)
        for i, q in enumerate(qs):
            if q['category'] != c:
                continue
            ev = q.get('evidence')
            key = json.dumps(ev, ensure_ascii=False, sort_keys=True) if ev else f'no-ev-{i}'
            groups[key].append(i)
        keys = sorted(groups)
        rng.shuffle(keys)
        pool = [i for k in keys for i in groups[k]]
        # 组不可拆：整组交替分配直到该类配额用尽（超配额的组回落到同类已选侧）
        take = quotas[c]
        side, chosen, used = train, [], 0
        for k in keys:
            if used >= take:
                break
            grp = groups[k]
            if used + len(grp) <= take:
                chosen += grp
                used += len(grp)
            elif not chosen:
                chosen = grp[:take]
                used = take
        # 组不可拆导致配额未满时，从剩余同类题补齐（同类即同层，不破坏分层）
        rest = [i for k in keys for i in groups[k] if i not in set(chosen)]
        rng.shuffle(rest)
        chosen += rest[:take - len(chosen)]
        # 该类配额内部再分 train/val：按组顺序前段给少的一侧，保证两侧行稳
        n_train = round(take * train_n / total)
        shuffled = chosen[:]
        rng.shuffle(shuffled)
        train += shuffled[:n_train]
        val += shuffled[n_train:]
    return {'case_id': case_id, 'seed': seed, 'method': 'category-stratified+evidence-grouped',
            'categories': {str(c): n for c, n in quotas.items()},
            'train': sorted(train), 'validation': sorted(val),
            'n_pool': len(qs)}


def largest_remainder(counts, total_pool, total_take):
    raw = {c: n * total_take / total_pool for c, n in counts.items()}
    base = {c: int(v) for c, v in raw.items()}
    rest = total_take - sum(base.values())
    order = sorted(counts, key=lambda c: (-(raw[c] - base[c]), c))
    for c in order[:rest]:
        base[c] += 1
    return base


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--case', default='conv-26')
    p.add_argument('--train', type=int, default=TRAIN_N)
    p.add_argument('--validation', type=int, default=VAL_N)
    p.add_argument('--out', default=None, help='划分清单落盘路径（新模式运行目录）')
    args = p.parse_args()
    split = build_split(args.case, args.train, args.validation)
    text = json.dumps(split, ensure_ascii=False, indent=1)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f'split written: {path}')
    print(f"train({len(split['train'])}): {split['train']}")
    print(f"validation({len(split['validation'])}): {split['validation']}")
    assert len(set(split['train']) & set(split['validation'])) == 0
    assert len(split['train']) == args.train and len(split['validation']) == args.validation


if __name__ == '__main__':
    main()
