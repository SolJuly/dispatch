#!/usr/bin/env python3
"""任务1 审计：SSH 客户端断开后，远端任务是否继续。

远端先放好 /root/t1_loop.sh（1 秒一行的计数循环），本地用 Popen 起 ssh，
3 秒后 kill 掉本地 ssh（等价于平板关页面 / 网络断 / 笔记本合盖），
等 25 秒再用新连接数行数：涨了 = 任务还在跑。

四组对照：
  A raw     前台裸跑
  B nohup   nohup + &
  C setsid  setsid nohup + &（csctl --detach 的写法）
  D pty     裸跑但 ssh -tt 强制分配 tty
"""
import json
import subprocess
import sys
import time

sys.path.insert(0, "/root/智云视频工作流")
import csctl  # noqa: E402
from compshare.cs import CS  # noqa: E402

LOOP = "/root/t1_loop.sh"
CASES = [
    ("A", "前台裸跑（无 nohup / 无重定向）", "bash {loop} {log}", False),
    ("B", "nohup + &", "nohup bash {loop} {log} >/dev/null 2>&1 & echo bg:$!", False),
    ("C", "setsid nohup + &（csctl --detach 写法）",
     "setsid nohup bash {loop} {log} >{out} 2>&1 </dev/null & echo started:$!", False),
    ("D", "ssh -tt 强制 pty + 前台裸跑", "bash {loop} {log}", True),
]


def ssh_args(cred, tty=False):
    user, ip, pw, port = cred
    a = ["sshpass", "-p", pw, "ssh", "-p", str(port),
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=8"]
    if tty:
        a.append("-tt")
    return a + [f"{user}@{ip}"]


def sh(cred, cmd, timeout=40):
    r = subprocess.run(ssh_args(cred) + [cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def lines(cred, log):
    out = sh(cred, f"wc -l < {log} 2>/dev/null || echo 0", timeout=25)
    try:
        return int(out.split()[0])
    except Exception:
        return 0


def one(cred, key, title, tpl, tty):
    log = f"/root/t1_{key}.log"
    out = f"/root/t1_{key}.out"
    sh(cred, f"rm -f {log} {out}", timeout=20)
    cmd = tpl.format(loop=LOOP, log=log, out=out)

    p = subprocess.Popen(ssh_args(cred, tty) + [cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    time.sleep(4)
    was_alive = p.poll() is None
    p.kill()
    try:
        p.wait(timeout=5)
    except Exception:
        pass

    before = lines(cred, log)
    time.sleep(25)
    after = lines(cred, log)
    ps = sh(cred, f"ps -eo pid,ppid,stat,cmd | grep -F 't1_loop.sh {log}' | grep -v grep | head -3",
            timeout=25)
    return {"case": key, "title": title, "ssh_alive_at_kill": was_alive,
            "lines_at_disconnect": before, "lines_25s_later": after,
            "grew_by": after - before, "kept_running": after > before, "ps": ps}


def main():
    ident = sys.argv[1] if len(sys.argv) > 1 else "audit-store"
    cs = CS()
    inst = cs.get(ident)
    cred = csctl._ssh_cmd(inst, cs)
    if not cred:
        raise SystemExit("无 SSH 凭据")
    print(f"实例 {inst['UHostId']}  root@{cred[1]}:{cred[3]}", flush=True)

    rows = []
    for key, title, tpl, tty in CASES:
        r = one(cred, key, title, tpl, tty)
        rows.append(r)
        print(f"  [{key}] {title}: {r['lines_at_disconnect']} → {r['lines_25s_later']} "
              f"{'仍在跑' if r['kept_running'] else '已停止'}", flush=True)

    print("\n| 组 | 方式 | 断开时行数 | 25 秒后 | 还在跑 |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['case']} | {r['title']} | {r['lines_at_disconnect']} | "
              f"{r['lines_25s_later']} | {'✅ 是' if r['kept_running'] else '❌ 否'} |")
    print("\n进程快照（PPID=1 表示已被 init 收养，脱离 SSH 会话）：")
    for r in rows:
        print(f"[{r['case']}] {r['ps'] or '(已无进程)'}")

    # 关键补充证据：ComfyUI 自身进程的 PPID
    cf = sh(cred, "ps -eo pid,ppid,cmd | grep -E 'start.d/comfyui.sh|main.py --listen' | grep -v grep")
    print(f"\nComfyUI 启动链:\n{cf}")

    out = "/root/智云视频工作流/reports/audit-ssh-detach.json"
    with open(out, "w") as f:
        json.dump({"rows": rows, "comfy_chain": cf}, f, ensure_ascii=False, indent=2)
    print(f"\n写入 {out}")
    sh(cred, f"pkill -f 't1_loop.sh' || true", timeout=20)


if __name__ == "__main__":
    main()
