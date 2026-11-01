#!/usr/bin/env python3
"""风险清单自动化监控。

每条风险一个检查函数，返回 (level, value, detail, action)。
  level: ok / info / medium / high
输出 reports/risk/latest.json + reports/risk/alerts.md，并在 /risk 页面展示。

用法:
    python3 tools/riskmon.py            # 跑一次，打印表格
    python3 tools/riskmon.py --json     # 只输出 JSON（给 fileserver 用）
    python3 tools/riskmon.py --deep     # 额外探测正在运行的实例内部
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT_DIR = ROOT / "reports" / "risk"

LEVEL_ORDER = {"high": 0, "medium": 1, "info": 2, "ok": 3}
# 已被证实"重试即可"的失败原因，不算风险，避免常驻噪声
KNOWN_RETRYABLE = ("226604", "226628", "out of resources", "镜像正在同步",
                   "8333", "cpu memory ratio", "Connection to")
LEVEL_CN = {"high": "高危", "medium": "关注", "info": "提示", "ok": "正常"}


def _sh(cmd: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception as e:
        return f"__err__{e}"


def _ledger_rows():
    p = ROOT / ".compshare" / "ledger.jsonl"
    rows = []
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _today_cost() -> float:
    """与 `csctl cost` 同一口径：只累加 event==cost 的流水，避免重复计数。"""
    try:
        import csctl
        return round(csctl.ledger_today_cost(), 2)
    except Exception:
        today = time.strftime("%Y-%m-%d")
        total = 0.0
        for r in _ledger_rows():
            if str(r.get("at", ""))[:10] == today and r.get("event") == "cost":
                total += float(r.get("cost") or 0)
        return round(total, 2)


# --------------------------------------------------------------- 检查项
def c_balance(cs):
    b = cs.balance()
    v = float(b.get("AmountAvailable") or b.get("Amount") or 0)
    if v < 10:
        return "high", f"¥{v:.2f}", "余额撑不住一次长任务", "立即充值；否则所有实例会被 fail-safe 关掉"
    if v < 20:
        return "medium", f"¥{v:.2f}", "余额偏低，跑一条 4090 视频约 ¥0.7", "补到 ¥50 以上更稳"
    return "ok", f"¥{v:.2f}", "余额充足", ""


def c_running_unbounded(cs, insts, stop_map):
    bad = [i for i in insts if i.get("State") == "Running" and not stop_map.get(i["UHostId"])]
    if bad:
        names = ", ".join(i.get("Name") or i["UHostId"] for i in bad)
        return "medium", f"{len(bad)} 台", f"运行中但无定时关机: {names}", \
            f"python3 csctl.py stop-at {bad[0]['UHostId']} --at '+2h'"
    return "ok", f"{len(insts)} 台", "运行中的都有定时关机或没有实例", ""


def c_orphan_lab(cs, insts):
    lab_names = [i for i in insts if str(i.get("Name") or "").startswith("lab-")]
    if not lab_names:
        return "ok", "0 台", "没有 lab-* 实例", ""
    svc = _sh("systemctl is-active cs-lab")
    if svc != "active":
        names = ", ".join(i.get("Name") or "" for i in lab_names)
        return "high", f"{len(lab_names)} 台", \
            f"cs-lab 服务是 {svc}，但这些实例还在跑: {names}", \
            f"python3 csctl.py rm {lab_names[0]['UHostId']} --yes"
    return "ok", f"{len(lab_names)} 台", "lab 实例归属正常（cs-lab 在跑）", ""


def c_day_budget(cs):
    t = _today_cost()
    if t > 20:
        return "high", f"¥{t:.2f}", "今日花费已超日预算 ¥20", "停掉非必要实例，检查是否有失控任务"
    if t > 12:
        return "medium", f"¥{t:.2f}", "今日花费接近日预算 ¥20", "收敛实验规模"
    return "ok", f"¥{t:.2f}", "今日花费在预算内", ""


def c_price_spike(cs, insts):
    bad = []
    for i in insts:
        if i.get("State") != "Running":
            continue
        try:
            p = sum(cs.hourly(i))
        except Exception:
            continue
        if p and p > 3.0:
            bad.append((i.get("Name") or i["UHostId"], p))
    if bad:
        n, p = bad[0]
        return "medium", f"{len(bad)} 台", f"单价超 ¥3/h: {n} ¥{p:.2f}/h", \
            "换低成本卡型或缩短时长"
    return "ok", "—", "在跑实例单价都 ≤ ¥3/h", ""


def c_stock(cs):
    try:
        inv = cs.inventory()
        zid = cs._zone_id(cs.zone)
        ex = (inv.get("Exclusive") or {}).get(zid, {})
        g4090 = int(ex.get("4090") or 0)
    except Exception as e:
        return "info", "未知", f"库存查询失败: {str(e)[:60]}", ""
    if g4090 == 0:
        return "medium", "4090 0 台", "主力卡 4090 当前无货，部署会卡在等库存", \
            "用 --fallback 3090,V100S 或先跑别的"
    return "ok", f"4090 {g4090} 台", "有货", ""


def c_ssh_unready(cs, insts):
    running = [i for i in insts if i.get("State") == "Running"]
    if not running:
        return "ok", "—", "没有运行中的实例", ""
    try:
        import csctl
    except Exception:
        return "info", "—", "无法加载 csctl，跳过", ""
    bad = []
    for i in running:
        rc, _ = csctl.ssh_run(i, cs, "echo ok", timeout=12)
        if rc != 0:
            bad.append(i.get("Name") or i["UHostId"])
    if bad:
        return "medium", f"{len(bad)} 台", f"Running 但 SSH 不通: {', '.join(bad)}", \
            "Running ≠ sshd 就绪，等 20–40 秒；持续不通则 reboot"
    return "ok", f"{len(running)} 台", "SSH 全部可达", ""


def c_service(cs):
    down = []
    for s in ("cs-files", "cs-lab"):
        if _sh(f"systemctl is-active {s}") != "active":
            down.append(s)
    if down:
        return "high", ", ".join(down), "常驻服务不在跑，页面/基准会停更", \
            f"systemctl restart {' '.join(down)}"
    return "ok", "cs-files, cs-lab", "两个常驻服务 active", ""


def c_cron(cs):
    txt = _sh("crontab -l")
    need = {"watch": "watch --once", "arch": "tools/arch.py", "cost": "cost_snapshot.py"}
    miss = [k for k, v in need.items() if v not in txt]
    if miss:
        return "medium", ", ".join(miss), f"缺定时任务: {miss}", "bash scripts/install_cron.sh"
    return "ok", "3/3", "watch / arch / cost 定时任务齐全", ""


def c_cred_perm(cs):
    p = ROOT / "compshare" / "credentials.env"
    if not p.exists():
        return "info", "—", "未找到 credentials.env", ""
    mode = oct(os.stat(p).st_mode & 0o777)
    if mode != "0o600":
        return "high", mode, f"凭证文件权限是 {mode}，同机其他用户可读", f"chmod 600 {p}"
    return "ok", mode, "凭证权限 600", ""


def c_disk(cs):
    du = shutil.disk_usage(str(ROOT))
    pct = du.used / du.total * 100
    if pct > 90:
        return "high", f"{pct:.0f}%", f"控制机磁盘剩 {du.free/2**30:.1f}GB", "清 logs/ 与旧 reports"
    if pct > 80:
        return "medium", f"{pct:.0f}%", f"控制机磁盘剩 {du.free/2**30:.1f}GB", "清 logs/ 与旧 reports"
    return "ok", f"{pct:.0f}%", f"剩 {du.free/2**30:.1f}GB", ""


def c_lab_state(cs):
    p = ROOT / "lab" / "state.json"
    if not p.exists():
        return "info", "—", "无 lab 状态文件", ""
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "medium", "损坏", "lab/state.json 无法解析", "删掉让它重建"
    groups = st.get("groups") or {}
    total = failed = 0
    reasons = []
    for gid, g in (groups.items() if isinstance(groups, dict) else []):
        for name, w in (g.get("workloads") or {}).items():
            if not isinstance(w, dict):
                continue
            total += 1
            if w.get("status") == "failed":
                rsn = str(w.get("reason") or "")
                if any(t in rsn for t in KNOWN_RETRYABLE):
                    continue
                failed += 1
                reasons.append(f"{gid}/{name}: {rsn[:40]}")
    if failed:
        return "medium", f"{failed}/{total} 组", "；".join(reasons[:3]), "看 reports/lab/ 下的失败明细"
    return "ok", f"{total} 组", "基准组无失败记录", ""


def _yavg(path: Path):
    """首帧亮度均值（0–255）。ffmpeg signalstats，无 PyAV 依赖。"""
    cmd = ("ffmpeg -v error -i " + str(path) +
           " -frames:v 1 -vf signalstats,metadata=print:key=lavfi.signalstats.YAVG "
           "-f null - 2>&1")
    out = _sh(cmd, timeout=45)
    m = re.search(r"YAVG=([\d.]+)", out)
    return float(m.group(1)) if m else None


def c_black_frame(cs):
    """扫描最近视频产物首帧亮度，YAVG<2 视为黑帧（Wan fp8 踩过的坑）。"""
    vids = sorted((ROOT / "reports").rglob("*.mp4"), key=lambda p: -p.stat().st_mtime)[:3]
    if not vids:
        return "ok", "—", "暂无视频产物", ""
    bad = []
    for v in vids:
        y = _yavg(v)
        if y is not None and y < 2.0:
            bad.append(f"{v.name}(YAVG={y:.1f})")
    if bad:
        return "high", f"{len(bad)} 条", f"疑似黑帧: {', '.join(bad)}", \
            "该模型/镜像组合出不了片（fp8 与 ComfyUI 0.3.40 不兼容），换 SVD 或 0.36 镜像"
    return "ok", f"{len(vids)} 条", "最近视频首帧非黑", ""


def c_archive_gap(cs):
    """terminate 之前没有 pull 记录 → 产物可能随实例一起没了。"""
    rows = _ledger_rows()
    term, pulled = {}, set()
    for r in rows:
        ev = r.get("event")
        uh = r.get("uhost")
        if not uh:
            continue
        if ev in ("pull", "archive", "archive_from"):
            pulled.add(uh)
        if ev == "terminate":
            nm = str(r.get("name") or "")
            if nm.startswith("lab-"):
                pulled.add(uh)      # lab 流程内含自动归档
            else:
                term[uh] = r.get("at")
    gap = [u for u in term if u not in pulled]
    if gap:
        return "info", f"{len(gap)} 台", \
            f"{len(gap)} 台实例销毁前账本里没有 pull 记录（pull 自本次起才记账，历史无法追溯）", \
            "销毁前先 `csctl pull <id> /root/ComfyUI/output`，或确认产物无用"
    return "ok", f"{len(term)} 台", "已销毁实例都有归档记录或无可归档内容", ""


def c_model_lib(cs):
    p = Path("/model/comfyui/models")
    if not p.exists():
        return "ok", "—", "本机不挂 UPFS（正常，实例侧才挂）", ""
    return "ok", "—", "UPFS 已挂载", ""


DEEP_SCRIPT = r"""
set -u
echo "PORT8188 $( (timeout 3 bash -c '</dev/tcp/127.0.0.1/8188' && echo up) 2>/dev/null || echo down)"
echo "DISK $(df -h / | awk 'NR==2{print $5}')"
echo "OUTPUTS $(ls /root/ComfyUI/output 2>/dev/null | wc -l)"
echo "MODELS $(ls /root/ComfyUI/models/checkpoints 2>/dev/null | wc -l)"
echo "SHARED $(ls /model/comfyui/models 2>/dev/null | wc -l)"
echo "GPU $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader 2>/dev/null | head -1)"
"""


def c_deep(cs):
    """深度检查：对运行中的实例探一次内部状态。"""
    try:
        import csctl
    except Exception:
        return "info", "—", "无法加载 csctl", ""
    insts = [i for i in cs.instances() if i.get("State") == "Running"]
    if not insts:
        return "info", "—", "无运行实例，跳过深度探测", ""
    i = insts[0]
    rc, out = csctl.ssh_run(i, cs, DEEP_SCRIPT, timeout=40)
    if rc != 0:
        return "medium", "不可达", f"深度探测失败: {out[:80]}", "确认 SSH 可用"
    kv = {}
    for line in out.splitlines():
        if " " in line:
            k, v = line.split(" ", 1)
            kv[k] = v
    issues = []
    if kv.get("PORT8188") == "down":
        issues.append("ComfyUI:8188 未监听")
    try:
        if int(kv.get("MODELS", "0")) == 0 and int(kv.get("SHARED", "0")) == 0:
            issues.append("看不到任何 checkpoint")
    except Exception:
        pass
    if issues:
        return "medium", i.get("Name") or "", "；".join(issues), "bash /start.d/comfyui.sh 重启服务"
    return "ok", i.get("Name") or "", f"8188 up，共享库 {kv.get('SHARED')} 类，输出 {kv.get('OUTPUTS')} 个", ""



def _queue_items():
    """读 P2 队列（不可用时返回 None）；不建库、不惊动主流程。"""
    try:
        from tools.p2 import jobqueue as q
        return q, q.items()
    except Exception:
        return None, []


def c_queue_terminal(cs):
    """R17：队列终态失败 / 已回收（P0/P1 硬化的告警出口）。"""
    q, items = _queue_items()
    if q is None:
        return "info", "—", "P2 队列不可用（跳过）", ""
    if not items:
        return "ok", "0", "队列为空", ""
    tf = [i for i in items if i["state"] == "terminal_failed"]
    rc = [i for i in items if i["state"] == "reclaimed"]
    if tf:
        sample = "；".join(f"{i['job_id']}（{(i.get('last_error') or '-')[:36]}）" for i in tf[:3])
        return "high", f"{len(tf)} 终态失败", f"{sample}", "python3 tools/p2/batchctl.py list --state terminal_failed"
    if rc:
        return "medium", f"{len(rc)} 已回收", "；".join(i["job_id"] for i in rc[:3]), \
            "python3 tools/p2/batchctl.py list --state reclaimed"
    return "ok", "0", f"终态失败 0 · 回收 0（共 {len(items)} 项）", ""


def c_queue_heartbeat(cs):
    """R18：队列心跳超时 / 卡死（P0-2 的监控面）。"""
    q, items = _queue_items()
    if q is None:
        return "info", "—", "P2 队列不可用（跳过）", ""
    try:
        st = q.stale(q.TIMEOUT_S)
    except Exception as e:
        return "info", "—", f"读心跳失败：{type(e).__name__}", ""
    running = [i for i in items if i["state"] in ("claimed", "running")]
    if st:
        return "high", f"{len(st)} 心跳超时", \
            "；".join(f"{i['job_id']}（{(i.get('worker') or '-')}）" for i in st[:3]), \
            "python3 tools/p2/batchctl.py watchdog"
    overdue = [i for i in items if i["state"] == "queued" and (i.get("next_retry_at") or 0) and
               (i["next_retry_at"] or 0) < time.time() - 120]
    if overdue:
        return "medium", f"{len(overdue)} 待重试超时", \
            "；".join(i["job_id"] for i in overdue[:3]), "检查 scheduler 是否在跑"
    return "ok", f"{len(running)} 在跑", f"在跑 {len(running)} 项，心跳正常", ""


# --------------------------------------------------------------- 主流程
def run_checks(deep: bool = False) -> dict:
    from compshare.cs import CS
    cs = CS()
    insts = cs.instances()
    try:
        stop_map = json.loads((ROOT / ".compshare" / "stop_at.json").read_text())
    except Exception:
        stop_map = {}

    checks = []

    def add(cid, name, fn, *a):
        try:
            level, value, detail, action = fn(cs, *a)
        except Exception as e:
            level, value, detail, action = "info", "—", f"检查异常: {type(e).__name__}: {str(e)[:70]}", ""
        checks.append({"id": cid, "name": name, "level": level, "value": value,
                       "detail": detail, "action": action})

    add("R01", "余额不足", c_balance)
    add("R02", "运行中实例无定时关机", c_running_unbounded, insts, stop_map)
    add("R03", "孤儿 lab 实例", c_orphan_lab, insts)
    add("R04", "当日花费超预算", c_day_budget)
    add("R05", "单价越过上限", c_price_spike, insts)
    add("R06", "主力卡无货", c_stock)
    add("R07", "Running 但 SSH 不通", c_ssh_unready, insts)
    add("R08", "常驻服务掉线", c_service)
    add("R09", "定时任务缺失", c_cron)
    add("R10", "凭证权限过宽", c_cred_perm)
    add("R11", "控制机磁盘将满", c_disk)
    add("R12", "基准组失败残留", c_lab_state)
    add("R13", "视频黑帧", c_black_frame)
    add("R14", "模型库不可见", c_model_lib)
    add("R15", "销毁前漏归档", c_archive_gap)
    add("R17", "队列终态失败/回收", c_queue_terminal)
    add("R18", "队列心跳超时", c_queue_heartbeat)
    if deep:
        add("R16", "实例内部健康", c_deep)

    checks.sort(key=lambda c: LEVEL_ORDER.get(c["level"], 9))
    summary = {k: sum(1 for c in checks if c["level"] == k) for k in ("high", "medium", "info", "ok")}
    worst = next((c["level"] for c in checks if c["level"] in ("high", "medium")), "ok")
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "worst": worst,
            "summary": summary, "checks": checks}


def write_alert(d: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 风险告警  {d['ts']}", "",
             f"高危 {d['summary']['high']} · 关注 {d['summary']['medium']} · "
             f"提示 {d['summary']['info']} · 正常 {d['summary']['ok']}", ""]
    for c in d["checks"]:
        if c["level"] in ("high", "medium"):
            lines.append(f"- **[{LEVEL_CN[c['level']]}] {c['id']} {c['name']}** — {c['value']}｜{c['detail']}")
            if c["action"]:
                lines.append(f"  - 处置：`{c['action']}`")
    (OUT_DIR / "alerts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = sys.argv[1:]
    deep = "--deep" in args
    d = run_checks(deep=deep)
    write_alert(d)
    if "--json" in args:
        print(json.dumps(d, ensure_ascii=False))
        return 0
    print(f"风险扫描 {d['ts']}   最坏级别: {LEVEL_CN[d['worst']]}")
    print(f"高危 {d['summary']['high']} / 关注 {d['summary']['medium']} / "
          f"提示 {d['summary']['info']} / 正常 {d['summary']['ok']}")
    print()
    print(f"{'ID':<5}{'级别':<6}{'项目':<26}{'当前值':<16}说明")
    print("-" * 96)
    for c in d["checks"]:
        print(f"{c['id']:<5}{LEVEL_CN[c['level']]:<6}{c['name']:<26}{c['value']:<16}{c['detail'][:44]}")
    print(f"\n写入 {OUT_DIR/'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
