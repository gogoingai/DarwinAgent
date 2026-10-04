"""轮次边界交接哨兵（专家规格三：R1 完成后新框架自动从 R2 生效）。

状态机（HANDOFF.json 持久化，flock 互斥，幂等可重启）：
  armed    —— 盯源根 R1/decision.json（3s 轮询）
  holding  —— R1 决策已落盘：拦停旧 worker（R2 提案前），等新框架就绪标记
  seeding  —— 按最后采纳版本种新根（B0 资产位）
  launched —— 新 worker 已起（接续逻辑 R2），哨兵退出
  failed   —— 记录原因，绝不静默；旧根产物一律不动

安全性质：不热改源码、不伪造旧身份、不覆盖旧根；交接重复触发由 flock＋状态检查挡住；
崩溃恢复后按状态续走；切换失败＝旧循环停在新根起跑前（不产生半套状态）。
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path('/Users/xu/git/oak')


def log(msg):
    print(f'[handoff {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def load(state_file):
    return json.loads(state_file.read_text())


def save(state_file, **fields):
    st = load(state_file)
    st.update(fields)
    tmp = state_file.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(state_file)


def stop_old_worker(source_root):
    """只杀属于源根的 supervisor 与 run.py（按路径匹配），其他运行不受影响。"""
    out = subprocess.run(['ps', '-axo', 'pid,command'], capture_output=True, text=True).stdout
    killed = []
    for line in out.splitlines():
        if 'datasets.locomo' not in line and 'supervise_agentic' not in line:
            continue
        if str(source_root) not in line:
            continue
        pid = int(line.strip().split()[0])
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            pass
    time.sleep(2)
    for pid in killed:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return killed


def main():
    state_file = Path(sys.argv[1])
    with open(state_file, 'a') as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = load(state_file)
        src = Path(st['source_root'])
        new_root = Path(st['new_root'])
        if st['state'] == 'launched':
            log('已完成，退出'); return
        if st['state'] == 'armed':
            # 用户指令（循环不停＞严格 R2）：每个新决策落地时检查就绪标记——
            # 就绪则交接；未就绪则不拦旧循环，记录顺延，继续盯下一轮边界。
            import re as _re
            watch_from = time.time()
            log(f"盯 {src} 的轮次决策（首个＝R1）…")
            while True:
                ready = Path(st['ready_marker']).exists()
                decisions = sorted(src.glob('train/R*/decision.json'),
                                   key=lambda p: p.stat().st_mtime)
                fresh = [d for d in decisions if d.stat().st_mtime > watch_from]
                if fresh:
                    dec_file = fresh[-1]
                    d = json.loads(dec_file.read_text())
                    rnd = _re.search(r'R(\d+)', str(dec_file)).group(0)
                    if ready:
                        slipped_next = (src / f'train/R{int(rnd[1:])+1}/proposal-call.json').exists()
                        killed = stop_old_worker(src)
                        save(state_file, state='holding',
                             trigger_round=rnd,
                             trigger_decision={'accepted': d.get('accepted'), 'reasons': d.get('reasons')},
                             next_round_slipped=slipped_next, stopped_pids=killed)
                        log(f"{rnd} 决策落地且新框架就绪：旧 worker 拦停 pid={killed}，交接")
                        break
                    save(state_file, deferred_rounds=(load(state_file).get('deferred_rounds') or []) + [rnd])
                    log(f"{rnd} 决策落地但新框架未就绪——不拦旧循环（循环不停），记录顺延，继续盯")
                    watch_from = dec_file.stat().st_mtime
                time.sleep(3)
        if load(state_file)['state'] == 'holding':
            log("等待新框架就绪标记…")
            while not Path(load(state_file)['ready_marker']).exists():
                time.sleep(10)
            st = load(state_file)
            log(f"新框架就绪：{st['framework_commit']}")
            save(state_file, state='seeding')
        if load(state_file)['state'] in ('seeding',):
            st = load(state_file)
            marker = json.loads(Path(st['ready_marker']).read_text())
            st['framework_commit'] = marker.get('commit')
            # 种子＝源根「最后采纳版本」（R1 采纳则 R1 候选，否则此前采纳版）
            pub = src / 'train/published'
            cur = json.loads((pub / 'current.json').read_text())
            version = cur['version']
            (new_root / 'train/B0').mkdir(parents=True, exist_ok=True)
            subprocess.run(['cp', '-R', str(pub / 'versions' / version),
                            str(new_root / 'train/B0/assets')], check=True)
            atomic_note = {
                'provenance': '轮次边界交接（专家规格三）：种子=源根最后采纳版本',
                'source_root': str(src), 'inherited_version': version,
                'r1_decision': st.get('r1_decision'),
                'framework_commit': st['framework_commit'],
                'round_map': {'v11 B0': '旧框架重锚', 'v11 R1': '逻辑 R1（旧框架）',
                              'v12 B0': '新框架锚定（继承资产，量框架效应，不算逻辑轮）',
                              'v12 R1': '逻辑 R2（新框架首个提案轮）'},
            }
            (new_root / 'train/B0/ROOT-NOTE.json').write_text(
                json.dumps(atomic_note, ensure_ascii=False, indent=1))
            save(state_file, state='launched', seeded_version=version)
            log_cmd = new_root.parent / 'supervise_g1.log'
            subprocess.Popen(
                ['/bin/bash', '-c',
                 f'nohup {REPO}/datasets/locomo/scripts/supervise_agentic.sh g1 {new_root} 6 '
                 f'--scope sfcp >> {log_cmd} 2>&1 &'],
                start_new_session=True)
            log(f"新 worker 已起：{new_root}（继承 {version[:12]}，接续逻辑 R2）")
        # failed 状态：如实留档退出
        if load(state_file)['state'] == 'failed':
            log('此前失败，需人工处理：' + str(load(state_file).get('fail_reason')))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001 —— 失败必须留档，绝不静默
        sf = Path(sys.argv[1])
        if sf.exists():
            save(sf, state='failed', fail_reason=f'{type(exc).__name__}: {exc}')
        log(f'失败：{type(exc).__name__}: {exc}')
        raise
