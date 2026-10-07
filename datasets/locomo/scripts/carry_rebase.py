"""答案检查点搬运＋验证（用户指令：不要从头跑）。

把源根 train/B0 的 assets/generation/evaluation 复制进目标根，并对每个会话写
CARRIED.json 旁车（源身份清单＋源框架映射）。管道加载时对「答案路径文件」
（darwinagent/ 全部，experiments 编排层除外）自行复核哈希；本脚本在搬运前预检一次，
不一致立即拒绝——不产出半套。用法：
  uv run python -m datasets.locomo.scripts.carry_rebase <目标根> <源根>
"""
import json
import shutil
import sys
from pathlib import Path

from darwinagent.runtime.artifacts import atomic_json

REPO = Path(__file__).resolve().parents[3]


def answer_path_ok(source_framework, current):
    from darwinagent.engine.pipeline import CARRY_NEUTRAL_SUFFIXES
    bad = [p for p, sha in source_framework.items()
           if '/darwinagent/experiments/' not in p
           and not any(p.endswith(sfx) for sfx in CARRY_NEUTRAL_SUFFIXES)
           and current.get(p) != sha]
    return bad


def main(argv):
    if len(argv) != 3:
        print(__doc__); return 2
    dst, src = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    from darwinagent.runtime.identity import snapshot_files
    current = snapshot_files([REPO / 'darwinagent'])
    for name in ('assets', 'generation'):
        s = src / 'train/B0' / name
        if not s.exists():
            continue
        d = dst / 'train/B0' / name
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d)
    ev = src / 'train/B0/evaluation'
    if ev.exists() and not (dst / 'train/B0/evaluation').exists():
        shutil.copytree(ev, dst / 'train/B0/evaluation')
        # 判分检查点带 run_identity（含完整框架图）：框架有任何变更即作废——删除身份章
        # 文件（evaluation/<case>.json），保留 evaluation/<case>/<gold>/cache 判题缓存
        # （按题+答案+gold 键控，身份无关）——重判走缓存命中，成本近零。v13 事故修复。
        carried_fw = {}
        ident_probe = next((dst / 'train/B0/generation').glob('conv-*/identity.json'), None)
        if ident_probe is not None:
            carried_fw = json.loads(ident_probe.read_text()).get('framework', {})
        fw_changed = any(current.get(p) != sha for p, sha in carried_fw.items())
        if fw_changed:
            for case_json in (dst / 'train/B0/evaluation').glob('conv-*.json'):
                case_json.unlink()
            print('框架已变更：丢弃身份章判分检查点（判题缓存保留，重判近零成本）')
    carried = 0
    for case_dir in sorted((dst / 'train/B0/generation').glob('conv-*')):
        ident_file = case_dir / 'identity.json'
        if not ident_file.exists():
            continue
        recorded = json.loads(ident_file.read_text())
        bad = answer_path_ok(recorded.get('framework', {}), current)
        if bad:
            print(f'拒绝搬运 {case_dir.name}: 答案路径文件与源不一致 {bad[:3]}')
            shutil.rmtree(dst / 'train/B0/generation', ignore_errors=True)
            return 1
        atomic_json(case_dir / 'CARRIED.json', {
            'from_root': str(src), 'source_case': case_dir.name,
            'accepted_identities': [recorded['identity']],
            'source_framework': recorded['framework'],
            'note': '答案路径一致性由本脚本预检、管道加载时复核'})
        carried += 1
    print(f'搬运完成: {carried} 个会话检查点（答案路径一致性预检通过）')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
