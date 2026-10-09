"""conv-26 题目分层划分（新模式：同对话 15 训练＋10 验证，四次复查/recheck4 协议）。

规则：
- 按 category 比例及最大余数法计算平衡目标，实际类别数随完整证据组调整；
- 跨类别、部分重叠及传递共享 evidence 的题作为完整连通组，只能进入一侧或不选；
- 分组和选题只读取类别及 evidence，不使用题面或 gold；题数无法满足时明确报错；
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
TRAIN_N = 15
VAL_N = 10
SEED = 20261005


def build_split(case_id='conv-26', train_n=TRAIN_N, val_n=VAL_N, seed=SEED, *, data_path):
    if type(train_n) is not int or type(val_n) is not int or min(train_n, val_n)<1:
        raise ValueError('Training and validation counts must be positive integers')
    data = json.loads(Path(data_path).read_text())
    item = next(x for x in data if x['sample_id'] == case_id)
    qs = item['qa']
    total = train_n + val_n
    if len(qs) < total:
        raise ValueError(f'{case_id} 题数不足: {len(qs)} < {total}')
    cats = sorted({q['category'] for q in qs})
    counts = Counter(q['category'] for q in qs)
    targets = (largest_remainder(counts, len(qs), train_n),
               largest_remainder(counts, len(qs), val_n))
    rng = random.Random(seed)
    # 全局证据连通组：跨题型、部分重叠和传递重叠都不能被拆到两侧。
    parents=list(range(len(qs)))
    def find(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]]
            i=parents[i]
        return i
    seen={}
    for i,q in enumerate(qs):
        for evidence in q.get('evidence') or ():
            key=json.dumps(evidence,ensure_ascii=False,sort_keys=True)
            if key in seen:
                parents[find(i)]=find(seen[key])
            else:
                seen[key]=i
    grouped=defaultdict(list)
    for i in range(len(qs)):
        grouped[find(i)].append(i)
    groups=sorted(grouped.values(),key=lambda g:g[0])
    rng.shuffle(groups)
    def distance(value):
        return sum((value[side+2].get(c,0)-targets[side][c])**2
                   for side in (0,1) for c in cats)
    # 两侧题数作状态，整组选取；类别配额是平衡目标，绝不靠拆组凑数。
    states={(0,0):([],[],Counter(),Counter())}
    for group in groups:
        next_states=dict(states)
        group_counts=Counter(qs[i]['category'] for i in group)
        for sizes,value in states.items():
            for side,cap in ((0,train_n),(1,val_n)):
                if sizes[side]+len(group)>cap:
                    continue
                key=list(sizes);key[side]+=len(group);key=tuple(key)
                candidate=list(value)
                candidate[side]=value[side]+group
                candidate[side+2]=value[side+2]+group_counts
                candidate=tuple(candidate)
                if key not in next_states or distance(candidate)<distance(next_states[key]):
                    next_states[key]=candidate
        states=next_states
    if (train_n,val_n) not in states:
        raise ValueError('Cannot meet requested counts without splitting evidence groups; adjust counts')
    train,val,train_counts,val_counts=states[(train_n,val_n)]
    return {'case_id': case_id, 'seed': seed, 'method': 'category-balanced+evidence-components-v2',
            'categories': {str(c):train_counts[c]+val_counts[c] for c in cats},
            'split_categories':{'train':dict(train_counts),'validation':dict(val_counts)},
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
    from datasets.locomo.inputs import add_dataset_arguments, resolve_dataset
    p = argparse.ArgumentParser()
    add_dataset_arguments(p)
    p.add_argument('--case', default='conv-26')
    p.add_argument('--train', type=int, default=TRAIN_N)
    p.add_argument('--validation', type=int, default=VAL_N)
    p.add_argument('--output', default='runs/locomo-split', help='输入版本记录目录')
    p.add_argument('--out', default=None, help='划分清单落盘路径（新模式运行目录）')
    args = p.parse_args()
    identity_root = Path(args.out).parent if args.out else Path(args.output)
    data_dir = resolve_dataset(args, identity_root)
    split = build_split(args.case, args.train, args.validation, data_path=data_dir / "locomo10_zh.json")
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
