#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""csctl —— 优云智算 GPU 实例管理 CLI（省时 + 省钱）

常用：
  python3 csctl.py ls                        # 看所有实例 / 状态 / 单价 / 花费
  python3 csctl.py price --all               # 全卡型分计费方式价格（挑最便宜的组合）
  python3 csctl.py stock --gpu 4090 --watch  # 盯库存，有货就自动下单部署
  python3 csctl.py up --gpu 4090 --hours 4   # 一键部署：等库存→创建→等就绪→设定时关机
  python3 csctl.py watch --idle-minutes 15   # 空闲守护：GPU 闲置 15 分钟自动关机（关机不计算力费）
  python3 csctl.py stop <名字|ID>            # 立刻关机，GPU/CPU/内存费用归零
  python3 csctl.py rm <名字|ID> --yes        # 彻底释放
"""
from __future__ import annotations

import argparse
import base64
import uuid
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.transfer import Transfer, human as thuman  # noqa: E402

from compshare.cs import (  # noqa: E402
    CS, CompShareError, SPOT_UNSUPPORTED, learned_stop_at, load_state, save_state,
)

WATCH_STATE = Path(__file__).resolve().parent / ".compshare" / "watch_state.json"
LEDGER = Path(__file__).resolve().parent / ".compshare" / "ledger.jsonl"
UP_LOCK = Path(__file__).resolve().parent / ".compshare" / "up.lock"


def ledger(event: str, **kw) -> None:
    """成本/生命周期账本：append-only，一行一个事件，便于事后对账。"""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": int(time.time()), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "event": event, **{k: v for k, v in kw.items() if v is not None}}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ledger_today_cost() -> float:
    if not LEDGER.exists():
        return 0.0
    today = dt.date.today().isoformat()
    total = 0.0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "cost" and str(r.get("at", ""))[:10] == today:
            total += float(r.get("cost") or 0)
    return total


def ledger_recent(n: int = 20) -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def acquire_up_lock():
    """同一时间只允许一个 up（防重复下单）。返回文件句柄，进程退出自动释放。"""
    import fcntl
    UP_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = UP_LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise CompShareError("另一个 up 正在运行（.compshare/up.lock 被占用），已中止")
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# --------------------------------------------------------------------- 输出
def _w(s: str) -> int:
    """终端显示宽度（中文算 2 格）。"""
    n = 0
    for ch in str(s):
        n += 2 if ord(ch) > 0x2E7F else 1
    return n


def table(headers: list[str], rows: list[list], max_width: dict[int, int] | None = None) -> None:
    max_width = max_width or {}
    cols = len(headers)
    data = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = []
    for i, h in enumerate(headers):
        w = max([_w(h)] + [_w(r[i]) for r in data]) if data else _w(h)
        cap = max_width.get(i)
        widths.append(min(w, cap) if cap else w)
    def fmt(cells):
        out = []
        for i, c in enumerate(cells):
            pad = widths[i] - _w(c)
            if pad < 0:
                # 简单截断
                while _w(c) > widths[i]:
                    c = c[:-2] + "…" if _w(c) > widths[i] else c
                pad = max(0, widths[i] - _w(c))
            out.append(c + " " * pad)
        return "  ".join(out).rstrip()
    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in data:
        print(fmt(r))


def fmt_ts(ts) -> str:
    try:
        ts = int(ts)
        if ts <= 0:
            return "-"
        return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def money(v: float | None) -> str:
    return "-" if v in (None, "") else f"¥{float(v):.2f}"


def _arrow(inst: dict) -> str:
    st = inst.get("State")
    return {"Running": "●", "Stopped": "○", "Starting": "◐", "Stopping": "◑",
            "Install": "◔", "Rebooting": "◓"}.get(st, "·")


def _elapsed(inst: dict) -> float:
    """本计费周期已运行小时数（关机状态为 0，关机不收算力费）。"""
    if inst.get("State") not in ("Running", "Rebooting"):
        return 0.0
    t = inst.get("StartTime") or inst.get("CreateTime") or 0
    return max(0.0, (time.time() - float(t)) / 3600.0) if t else 0.0


def _cost_so_far(inst: dict) -> float:
    hourly = float(inst.get("_hourly") or 0) - float(inst.get("_disk_hourly") or 0)
    disk_h = float(inst.get("_disk_hourly") or 0)
    ct = inst.get("CreateTime") or 0
    disk_cost = disk_h * ((time.time() - float(ct)) / 3600.0) if ct else 0.0
    return hourly * _elapsed(inst) + disk_cost


# --------------------------------------------------------------------- 远程探测
def _ssh_cmd(inst: dict, cs: CS) -> tuple[str, str, str, str] | None:
    """(user, ip, password, port)：从 SshLoginCommand 解析用户与端口（端口可能是 22/23 等）。"""
    ip = cs.public_ip(inst)
    pw = cs.password_of(inst)
    if not ip or not pw:
        return None
    login = (cs.ssh_of(inst) or f"ssh root@{ip}").split()
    user, port = "root", "22"
    for i, tok in enumerate(login):
        if tok == "-p" and i + 1 < len(login):
            port = login[i + 1]
        if "@" in tok and not tok.startswith("-"):
            user = tok.split("@")[0] or "root"
            ip = tok.split("@")[1] or ip
    return user, ip, pw, port


def ssh_run(inst: dict, cs: CS, remote_cmd: str, timeout: int = 120,
            detach: bool = False, log: str = "") -> tuple[int, str]:
    """执行远程命令，返回 (exit_code, output)。detach=True 用 setsid+nohup 后台跑，断开不影响。"""
    cred = _ssh_cmd(inst, cs)
    if not cred:
        return 127, "缺少 IP/密码"
    user, ip, pw, port = cred
    if detach:
        remote_cmd = (f"setsid nohup bash -lc {json.dumps(remote_cmd)} "
                      f"> {log or '/root/csctl_job.log'} 2>&1 < /dev/null & echo started:$!")
    try:
        r = subprocess.run(
            ["sshpass", "-p", pw, "ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
             f"{user}@{ip}", remote_cmd],
            capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        if r.returncode != 0 and (r.stderr or "").strip():
            out += ("\n[stderr] " + r.stderr.strip()[-500:])
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"


def ssh_exec(inst: dict, cs: CS, remote_cmd: str, timeout: int = 20) -> str | None:
    cred = _ssh_cmd(inst, cs)
    if not cred or not shutil.which("sshpass"):
        return None
    user, ip, pw, port = cred
    try:
        r = subprocess.run(
            ["sshpass", "-p", pw, "ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
             f"{user}@{ip}", remote_cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def gpu_probe(inst: dict, cs: CS) -> dict | None:
    """SSH 里读真实 GPU 状态：利用率 + 正在跑的 GPU 进程数（比平台监控更准）。"""
    # 注意：ComfyUI 常驻会持有 CUDA 上下文（~300MiB）但并未跑任务，
    # 所以「有进程」不等于「在干活」——只把占用 >1GiB 的进程算作真实任务。
    out = ssh_exec(inst, cs, "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader; "
                             "echo ---; nvidia-smi --query-compute-apps=used_memory --format=csv,noheader,nounits "
                             "| awk '{s+=$1} END {print int(s/1024)}'; "
                             "echo ---; pgrep -c -f 'python|comfy|ffmpeg' || true")
    if not out:
        return None
    parts = [x.strip() for x in out.split("---")]
    try:
        utils = [float(x.replace("%", "").split()[0]) for x in parts[0].splitlines() if x.strip()]
        used_mib = float(parts[1].strip()) * 1024 if len(parts) > 1 and parts[1].strip() else 0.0
        procs = 1 if used_mib > 1024 else 0  # 只算真实占用显存的任务
        busy = int(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 0
        return {"gpu": max(utils) if utils else None, "gpu_procs": procs, "user_procs": busy}
    except Exception:
        return None


# --------------------------------------------------------------------- 命令
def cmd_ls(cs: CS, a) -> None:
    ins = cs.instances()
    if not ins:
        print("没有实例。（部署: python3 csctl.py up --gpu 4090 --hours 4）")
        return
    rows = []
    for i in ins:
        rows.append([
            _arrow(i), i.get("UHostId"), (i.get("Name") or "")[:22], i.get("_state_cn"),
            f"{i.get('GpuType')}x{i.get('GPU')}", f"{i.get('CPU')}C/{int(i.get('Memory',0))//1024}G",
            (i.get("ChargeType") or "") + ("*S" if i.get("IsSpot") else ""),
            money(i.get("_hourly")) + "/h",
            f"{_elapsed(i):.2f}h", money(_cost_so_far(i)),
            cs.public_ip(i) or "-",
            fmt_ts(i.get("SchedulerStopTime") or learned_stop_at(i["UHostId"])),
        ])
    table(["", "ID", "名称", "状态", "卡", "规格", "计费", "当前价", "已跑", "已花", "公网IP", "定时关机"], rows)
    run = [i for i in ins if i.get("State") == "Running"]
    burn = sum(float(i.get("_hourly") or 0) for i in run)
    print(f"\n运行中 {len(run)}/{len(ins)} 台，当前烧钱速度 {money(burn)}/小时 ≈ {money(burn*24)}/天")
    try:
        b = cs.balance()
        print(f"余额 ¥{b.get('AmountAvailable')}  → 满负荷还能跑 "
              f"{(float(b.get('AmountAvailable') or 0)/burn):.1f} 小时" if burn else
              f"余额 ¥{b.get('AmountAvailable')}")
    except CompShareError:
        pass


def cmd_price(cs: CS, a) -> None:
    types = cs.call("DescribeAvailableCompShareInstanceTypes", {"Region": cs.region, "Zone": cs.zone})
    combos: dict[str, tuple[int, int, int]] = {}
    for it in types.get("AvailableInstanceTypes") or []:
        g = it.get("Name")
        for ms in it.get("MachineSizes") or []:
            if ms.get("Gpu") != (a.gpu if a.gpu else 1):
                continue
            for col in ms.get("Collection") or []:
                combos.setdefault(g, (ms["Gpu"], col["Cpu"], col["Memory"][0]))
    if a.all:
        cand = sorted(combos.items(), key=lambda kv: float(kv[1][1]))
    else:
        key = a.gpu_type or "4090"
        cand = [(key, combos.get(key, (1, 16, 64)))]
    rows = []
    for g, (n, cpu, mem) in cand:
        if g in ("", ) or n != 1:
            continue
        cells = [g, f"{cpu}C/{mem}G"]
        spot_ok = g not in SPOT_UNSUPPORTED
        for ct in ("Postpay", "Spot", "Day", "Month"):
            if ct == "Spot" and not spot_ok:
                cells.append("不支持")
                continue
            try:
                p = cs.price(g, n, cpu, mem, ct, a.disk)
                cells.append(money(p["total"]))
            except CompShareError:
                cells.append("-")
        if not spot_ok:
            cells.append("-")
        else:
            try:
                pos = cs.price(g, n, cpu, mem, "Postpay", a.disk)["total"]
                spo = cs.price(g, n, cpu, mem, "Spot", a.disk)["total"]
                cells.append(f"-{100*(1-spo/pos):.0f}%" if pos and spo else "-")
            except CompShareError:
                cells.append("-")
        stock_e = cs.stock(g, False)
        cells.append(f"{stock_e}")
        rows.append(cells)
    table(["卡型", "规格", "按量/h", "抢占/h", "包日", "包月", "抢占省", f"现货({cs.zone[-2:]})"], rows)
    print(f"\n提示：系统盘 ≤100G 免费，默认 --disk 100 即 0 元盘费；抢占式被回收 <1h 免单。"
          f"\n      无卡模式（关机后 CPU 开机）：2C4G ¥0.15/h，8C16G ¥0.60/h —— 装环境/下模型用它。")


def cmd_stock(cs: CS, a) -> None:
    raw = cs.inventory_raw()
    inv = raw.get("GpuInventory") or {}
    zid = cs._zone_id(cs.zone)
    ex = (inv.get("Exclusive") or {}).get(zid, {})
    sp = (inv.get("Spot") or {}).get(zid, {})
    rows = [[g, ex.get(g, "-"), sp.get(g, "-"),
             "否" if g in SPOT_UNSUPPORTED else "是"] for g in sorted(set(ex) | set(sp))]
    table([f"{cs.zone} 卡型", "独占现货", "抢占现货", "支持抢占"], rows)
    print(f"\n库存更新时间: {fmt_ts(raw.get('UpdateTime'))}；现货为 0 说明暂时抢不到卡。")


def cmd_images(cs: CS, a) -> None:
    ims = cs.images(a.keyword)
    if a.type:
        ims = [i for i in ims if (i.get("ImageType") or "").lower() == a.type.lower()]
    ims.sort(key=lambda i: -(i.get("CreateTime") or 0))
    rows = [[i.get("CompShareImageId"), (i.get("Name") or "")[:40], i.get("ImageType"),
             (i.get("Softwares") or {}).get("CUDAVersion") or "-",
             money(i.get("Price")) if i.get("Price") else "免费",
             fmt_ts(i.get("CreateTime"))] for i in ims[: a.limit]]
    table(["镜像ID", "名称", "类型", "CUDA", "镜像费", "创建"], rows)
    print(f"\n共 {len(ims)} 个匹配；--keyword 过滤，如 --keyword ComfyUI / --keyword video")


def _pick_image(cs: CS, spec: str, gpu_type: str) -> dict:
    ims = cs.images(spec)
    if not ims:
        raise CompShareError(f"没有匹配镜像: {spec}")
    want_5090 = "5090" in gpu_type
    def score(i):
        n = i.get("Name") or ""
        has50 = "5090" in n or "50系" in n
        return (0 if has50 == want_5090 else 1, -(i.get("CreateTime") or 0))
    ims.sort(key=score)
    return ims[0]


def _auto_spec(cs: CS, gpu_type: str, count: int) -> tuple[int, int]:
    """该卡型最省钱（最小）的合法 CPU/内存组合。"""
    types = cs.call("DescribeAvailableCompShareInstanceTypes", {"Region": cs.region, "Zone": cs.zone})
    best = None
    for it in types.get("AvailableInstanceTypes") or []:
        if it.get("Name") != gpu_type:
            continue
        for ms in it.get("MachineSizes") or []:
            if ms.get("Gpu") != count:
                continue
            for col in ms.get("Collection") or []:
                if col["Cpu"] >= 8 and (best is None or col["Cpu"] < best[0]):
                    best = (col["Cpu"], col["Memory"][0])
    return best or (16, 64)


def cmd_up(cs: CS, a) -> None:
    lock = acquire_up_lock()          # 并发保护，进程退出自动释放
    try:
        _cmd_up_inner(cs, a)
    finally:
        lock.close()


def _cmd_up_inner(cs: CS, a) -> None:
    gpu_type = a.gpu_type
    spot = a.charge.lower() == "spot"
    if spot and gpu_type in SPOT_UNSUPPORTED:
        print(f"⚠ {gpu_type} 不支持抢占式，自动改为按量")
        spot = False
    charge = "Spot" if spot else "Postpay"
    img = _pick_image(cs, a.image, gpu_type)
    auto_cpu, auto_mem = _auto_spec(cs, gpu_type, a.count)
    cpu, mem = a.cpu or auto_cpu, a.mem or auto_mem

    # 0) 选卡：首选卡没货时按 --fallback 顺延，省掉人工换卡的时间
    candidates = [gpu_type] + [x.strip() for x in (a.fallback or "").split(",") if x.strip()]
    chosen, t0 = None, time.time()
    while True:
        for g in candidates:
            if cs.stock(g, spot) > 0:
                chosen = g
                break
        if chosen:
            break
        print(f"[{dt.datetime.now():%H:%M:%S}] {'/'.join(candidates)} 暂无{charge}现货，"
              f"{'直接尝试下单' if (a.no_wait_stock or a.dry_run) else f'每 {a.poll}s 重试'}",
              flush=True)
        if a.no_wait_stock or a.dry_run:  # dry-run 不阻塞等货，只报告
            chosen = candidates[0]
            break
        if a.stock_timeout and time.time() - t0 > a.stock_timeout:
            raise CompShareError(f"等库存超时({a.stock_timeout}s)，可加大 --stock-timeout 或换 --fallback")
        time.sleep(a.poll)
    if chosen != gpu_type:
        print(f"→ 切换到有货的 {chosen}（原计划 {gpu_type} 无货）")
        gpu_type = chosen
        cpu, mem = _auto_spec(cs, gpu_type, a.count)

    p = cs.price(gpu_type, a.count, cpu, mem, charge, a.disk, a.data_disk,
                 image_id=img["CompShareImageId"])
    print(f"计划: {gpu_type} x{a.count} {cpu}C/{mem}G "
          f"盘{a.disk}G{('+数据盘%gG' % a.data_disk) if a.data_disk else ''} "
          f"镜像[{img.get('Name')}] 计费={charge} 单价≈{money(p['total'])}/小时")
    est = p["total"] * a.hours if a.hours else 0
    if a.hours:
        print(f"预估上限: {money(est)}（{a.hours}h 后会自动关机；余额 ¥{cs.balance().get('AmountAvailable')}）")
    # 预算熔断：余额下限 + 当日花费上限
    if a.balance_floor:
        bal = float(cs.balance().get("AmountAvailable") or 0)
        if bal < a.balance_floor:
            raise CompShareError(f"余额 ¥{bal:.2f} 低于下限 ¥{a.balance_floor}，已中止创建")
    if a.daily_budget:
        spent = ledger_today_cost()
        if spent + p["total"] * (a.hours or 1) > a.daily_budget:
            raise CompShareError(f"今日已花 ¥{spent:.2f}，再加本次预估 ¥{p['total'] * (a.hours or 1):.2f} "
                                 f"超过 --daily-budget ¥{a.daily_budget}，已中止")
    if a.max_hourly and p["total"] > a.max_hourly:
        raise CompShareError(f"单价 {p['total']}/h 超过 --max-hourly {a.max_hourly}，已中止"
                             f"（换卡型或 --charge spot）")
    if a.dry_run:
        print("--dry-run：不实际创建。")
        return

    # 1) 创建前查重：同名/同规格已在跑就不再下单
    for inst in cs.instances():
        same_name = (inst.get("Name") or "") == a.name
        same_spec = (inst.get("GpuType") == gpu_type and inst.get("State") in
                     ("Running", "Starting", "Install", "Initializing"))
        if a.name and same_name and inst.get("State") in ("Running", "Starting", "Install", "Initializing"):
            raise CompShareError(f"已有同名实例在跑: {inst['UHostId']} ({inst.get('State')})，未重复创建")

    print(f"[{dt.datetime.now():%H:%M:%S}] 下单创建 …")
    ids = cs.create(image_id=img["CompShareImageId"], gpu_type=gpu_type, gpu=a.count,
                    cpu=cpu, memory_gb=mem, name=a.name, charge_type=charge,
                    disk_gb=a.disk, data_disk_gb=a.data_disk,
                    password=a.password, remark=f"csctl {charge}")
    uid = ids[0]
    job_id = uuid.uuid4().hex[:10]
    print(f"实例已创建: {uid}（job {job_id}，其余 {len(ids)-1} 台略）")
    ledger("create", job=job_id, uhost=uid, name=a.name, gpu=gpu_type, count=a.count,
           charge=charge, hourly=p["total"], zone=a.zone or cs.zone)
    save_state({"last_instance": uid, "created_at": int(time.time()),
                "plan": {k: v for k, v in vars(a).items() if not callable(v)}})

    # 2) 等就绪
    print("等待就绪（Install → Running）…")
    inst = cs.wait_state(uid, {"running"}, timeout=a.wait_timeout)

    # 3) 定时关机兜底（忘关也不会烧钱）
    if a.hours:
        ts = int(time.time() + a.hours * 3600)
        try:
            cs.stop_at(uid, ts)
            ledger("schedule_stop", job=job_id, uhost=uid, stop_at=ts)
            print(f"已设定时关机: {fmt_ts(ts)}（{a.hours}h 后）")
        except CompShareError as e:
            print(f"⚠ 定时关机设置失败: {e}（用 python3 csctl.py stop-at {uid} --in {a.hours} 重试）")

    _print_access(cs, inst)


def _print_access(cs: CS, inst: dict) -> None:
    ip = cs.public_ip(inst)
    print("\n" + "=" * 68)
    print(f"实例  : {inst.get('UHostId')}  {inst.get('Name')}")
    print(f"规格  : {inst.get('GpuType')} x{inst.get('GPU')} / {inst.get('CPU')}C "
          f"{int(inst.get('Memory', 0))//1024}G  镜像: {inst.get('CompShareImageName')}")
    print(f"公网IP: {ip}")
    ssh = cs.ssh_of(inst)
    if ssh:
        print(f"登录  : {ssh}")
    pw = cs.password_of(inst)
    if pw and pw not in ("", "-"):
        print(f"密码  : {pw}")
    for sw in inst.get("Softwares") or []:
        url = sw.get("URL") or sw.get("Url") or ""
        print(f"应用  : {sw.get('Name') or sw.get('Software')} {url}")
    print(f"状态  : {inst.get('_state_cn')}   当前价 {money(inst.get('_hourly'))}/小时"
          f"   定时关机 {fmt_ts(inst.get('SchedulerStopTime') or learned_stop_at(inst['UHostId']))}")
    print("=" * 68)
    print(f"关机省钱: python3 csctl.py stop {inst.get('UHostId')}    "
          f"空闲自动关机: python3 csctl.py watch --idle-minutes 15")
    print(f"提醒: 关机后 GPU/CPU/内存免费，系统盘≤100G 免费，机器会保留；"
          f"长期不用请 `python3 csctl.py rm {inst.get('UHostId')} --yes`")


def cmd_start(cs: CS, a) -> None:
    i = cs.settle(a.id)
    if i.get("State") == "Running":
        print(f"{i['UHostId']} 已在运行")
        _print_access(cs, i)
        return
    print(f"启动 {i['UHostId']} …")
    cs.start(i["UHostId"])
    i = cs.wait_state(i["UHostId"], {"running"}, timeout=a.wait_timeout)
    _print_access(cs, i)


def cmd_stop(cs: CS, a) -> None:
    i = cs.settle(a.id)
    if i.get("State") == "Stopped":
        print(f"{i['UHostId']} 已是关机状态（算力费已停）")
        return
    if i.get("IsSpot"):
        print("⚠ 抢占式实例不支持关机，只能删除（rm）或等回收。")
    print(f"关机 {i['UHostId']} …（关机后 GPU/CPU/内存停收，仅保留磁盘）")
    cs.stop(i["UHostId"])
    ledger("stop", uhost=i["UHostId"], name=i.get("Name"), hourly=i.get("_hourly"))
    if not a.no_wait:
        cs.wait_state(i["UHostId"], {"stopped"}, timeout=600)


def cmd_reboot(cs: CS, a) -> None:
    i = cs.get(a.id)
    cs.reboot(i["UHostId"])
    print(f"已重启 {i['UHostId']}")


def push_file(inst: dict, cs: CS, local: str, remote: str) -> bool:
    return Transfer(cs, inst, quiet=True).push(local, remote)


def archive_from(inst: dict, cs: CS, remote: str, local: str) -> bool:
    """销毁前把远端产物拉回本地（rsync over sshpass）。"""
    cred = _ssh_cmd(inst, cs)
    if not cred:
        return False
    Path(local).mkdir(parents=True, exist_ok=True)
    ok = Transfer(cs, inst, quiet=True).pull(remote, local)
    if not ok:
        print(f"  ⚠ 归档失败（远端 {remote}）")
    return ok


def cmd_rm(cs: CS, a) -> None:
    i = cs.settle(a.id)
    if not a.yes:
        print(f"将彻底删除 {i['UHostId']} {i.get('Name')}（数据不可恢复）。加 --yes 执行。")
        return
    if a.archive:
        dest = Path(a.archive) if a.archive not in ("1", "true") else \
            Path("reports") / "archive" / i["UHostId"]
        print(f"  先归档 {a.archive_path} → {dest} …")
        archive_from(i, cs, a.archive_path, str(dest))
    run_h = max(0.0, (time.time() - float(i.get("StartTime") or time.time())) / 3600)
    cost = float(i.get("_hourly") or 0) * run_h
    cs.terminate(i["UHostId"])
    ledger("terminate", uhost=i["UHostId"], name=i.get("Name"), run_h=round(run_h, 3),
           cost=round(cost, 3))
    print(f"已提交删除 {i['UHostId']}（本周期约 ¥{cost:.2f} 已记账）")


def cmd_stop_at(cs: CS, a) -> None:
    i = cs.get(a.id)
    if a.at:
        hh, mm = a.at.split(":")
        now = dt.datetime.now()
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        ts = int(target.timestamp())
    else:
        ts = int(time.time() + a.in_hours * 3600)
    ts = max(ts, int(time.time()) + 300)
    cs.stop_at(i["UHostId"], ts)
    print(f"{i['UHostId']} 定时关机 = {fmt_ts(ts)}（平台最短 5 分钟后）")


def cmd_cancel_stop(cs: CS, a) -> None:
    cs.cancel_stop_at(cs.get(a.id)["UHostId"])
    print("已取消定时关机")


def cmd_watch(cs: CS, a) -> None:
    """空闲守护：GPU/CPU 长时间低于阈值 → 自动关机，省掉整夜空转的钱。"""
    st = {}
    if WATCH_STATE.exists():
        try:
            st = json.loads(WATCH_STATE.read_text())
        except Exception:
            st = {}
    while True:
        try:
            targets = cs.instances()
            if a.id:
                targets = [i for i in targets if cs.get(a.id)["UHostId"] == i["UHostId"]]
            now = time.time()
            for i in targets:
                uid = i["UHostId"]
                if (i.get("Name") or "").startswith("lab-"):
                    continue          # 基准实验室的机器由 lab_runner 自己管
                if i.get("State") != "Running":
                    st.pop(uid, None)
                    continue
                m = cs.monitor(uid)
                gpu = m.get("gpu")
                cpu = m.get("cpu") or 0
                procs = None
                if a.ssh:
                    pr = gpu_probe(i, cs)
                    if pr:
                        gpu = pr["gpu"] if pr["gpu"] is not None else gpu
                        procs = pr["gpu_procs"]
                egress = ""
                if procs is not None:
                    egress = f" gpuprocs={procs}"
                idle = (gpu is not None and gpu <= a.idle_gpu_pct and (procs in (None, 0))) or \
                       (gpu is None and cpu <= a.idle_cpu_pct)
                rec = st.setdefault(uid, {"idle_since": None, "samples": 0})
                if idle:
                    rec["idle_since"] = rec["idle_since"] or now
                    rec["samples"] = rec.get("samples", 0) + 1
                else:
                    rec["idle_since"] = None
                    rec["samples"] = 0
                idle_min = (now - rec["idle_since"]) / 60 if rec["idle_since"] else 0
                hours = _elapsed(i)
                print(f"[{dt.datetime.now():%H:%M:%S}] {uid} {i.get('Name','')[:16]:16s} "
                      f"gpu={('%.0f%%' % gpu) if gpu is not None else ' n/a '} cpu={cpu:.0f}%{egress} "
                      f"idle={idle_min:.0f}m run={hours:.2f}h 花={money(_cost_so_far(i))}", flush=True)
                if idle_min >= a.idle_minutes and rec["samples"] >= 2:
                    print(f"  → 空闲 {idle_min:.0f} 分钟，执行{a.stop_action} 省钱")
                    try:
                        if a.stop_action == "stop":
                            cs.stop(uid)
                        elif a.stop_action == "terminate":
                            cs.terminate(uid)
                        elif a.stop_action == "cardless":
                            cs.stop(uid)
                            time.sleep(10)
                            _try_cardless_start(cs, uid)
                        st.pop(uid, None)
                    except CompShareError as e:
                        print(f"  ⚠ {a.stop_action} 失败: {e}")
                if a.max_hours and hours >= a.max_hours:
                    print(f"  → 已运行 {hours:.2f}h 达到 --max-hours，关机")
                    try:
                        cs.stop(uid)
                    except CompShareError as e:
                        print(f"  ⚠ 关机失败: {e}")
                    st.pop(uid, None)
            WATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
            WATCH_STATE.write_text(json.dumps(st, indent=2))
        except CompShareError as e:
            print(f"[{dt.datetime.now():%H:%M:%S}] 警告: {e}", flush=True)
        if a.once:
            return
        time.sleep(a.interval)


def _try_cardless_start(cs: CS, uid: str) -> bool:
    """无卡模式开机：关机后用 2C4G/8C16G CPU 开机（0.15/0.6 元每小时）做下载、装环境。"""
    inst = cs.get(uid)
    if not inst.get("SupportWithoutGpuStart", True):
        print("  该实例不支持无卡开机（A800 实例不支持）")
        return False
    last = None
    for param in ("WithoutGpu", "WithoutGPU"):
        try:
            cs.call("StartCompShareInstance", {**cs.rz(inst), param: True})
            print(f"  已用无卡模式开机（{param}=true），约 ¥0.15~0.6/h")
            return True
        except CompShareError as e:
            last = e
    print(f"  无卡模式 API 未开放（{last}），请在控制台点“无卡模式开机”")
    return False


def cmd_cardless(cs: CS, a) -> None:
    i = cs.settle(a.id)
    if a.mode == "on":
        if i.get("State") != "Stopped":
            cs.stop(i["UHostId"])
            cs.wait_state(i["UHostId"], {"stopped"}, timeout=600)
        _try_cardless_start(cs, i["UHostId"])
    else:
        print("退出无卡模式 = 关机后用 GPU 规格重开（开始按 GPU 计费）")
        if i.get("State") != "Stopped":
            cs.stop(i["UHostId"])
            cs.wait_state(i["UHostId"], {"stopped"}, timeout=600)
        cs.start(i["UHostId"])
        _print_access(cs, cs.wait_state(i["UHostId"], {"running"}, timeout=a.wait_timeout))


def cmd_status(cs: CS, a) -> None:
    ins = cs.instances()
    b = {}
    try:
        b = cs.balance()
    except CompShareError:
        pass
    run = [i for i in ins if i.get("State") == "Running"]
    burn = sum(float(i.get("_hourly") or 0) for i in run)
    disk = sum(float(i.get("DiskPrice") or 0) for i in ins)
    print(f"账户余额 : ¥{b.get('AmountAvailable', '?')}  (冻结 ¥{b.get('AmountFreeze', 0)})")
    print(f"实例     : {len(ins)} 台，运行中 {len(run)} 台")
    print(f"算力 burn: {money(burn)}/小时  ≈ {money(burn*24)}/天")
    print(f"磁盘 burn: {money(disk)}/小时（≤100G 系统盘免费，长期不用请删除实例）")
    if burn and b.get("AmountAvailable"):
        print(f"续航     : 约 {float(b['AmountAvailable'])/burn:.1f} 小时（满负荷）")
    if ins:
        print()
        for i in ins:
            print(f"  {_arrow(i)} {i['UHostId']} {i.get('Name','')}  {i.get('_state_cn')}  "
                  f"{i.get('GpuType')}x{i.get('GPU')}  {money(i.get('_hourly'))}/h  "
                  f"已跑 {_elapsed(i):.2f}h  已花 {money(_cost_so_far(i))}  "
                  f"关机时间 {fmt_ts(i.get('SchedulerStopTime') or learned_stop_at(i['UHostId']))}")


def cmd_ssh(cs: CS, a) -> None:
    i = cs.get(a.id)
    ip = cs.public_ip(i)
    pw = cs.password_of(i)
    if a.exec:
        rc, out = ssh_run(i, cs, a.exec, timeout=a.timeout, detach=a.detach, log=a.log or "")
        print(out if out else "（无输出）")
        print(f"[exit={rc}]" + (f" 远端日志: {a.log}" if a.detach and a.log else ""))
        ledger("ssh_exec", uhost=i["UHostId"], rc=rc, detach=bool(a.detach))
        return
    print(cs.ssh_of(i) or f"ssh root@{ip}")
    if pw:
        print(f"# 密码: {pw}")
    cred = _ssh_cmd(i, cs)
    if cred and cred[3] != "22":
        print(f"# 注意：该实例 SSH 端口不是 22，是 {cred[3]}")


def cmd_cost(cs: CS, a) -> None:
    rows = ledger_recent(a.limit)
    if not rows:
        print("账本还是空的（.compshare/ledger.jsonl）。之后每次 up/stop/rm 都会自动记一条。")
        return
    table(["时间", "事件", "实例", "名称", "卡型", "小时价", "花费", "job"],
          [[r.get("at", ""), r.get("event"), r.get("uhost", "-"), (r.get("name") or "-")[:14],
            r.get("gpu", "-"), money(r.get("hourly")), money(r.get("cost")), r.get("job", "-")]
           for r in rows])
    print(f"\n今日累计花费 ¥{ledger_today_cost():.2f}；余额 ¥{cs.balance().get('AmountAvailable')}")


def cmd_jobs(cs: CS, a) -> None:
    rows = [r for r in ledger_recent(200) if r.get("event") == "create"]
    if not rows:
        print("还没有 job 记录")
        return
    table(["job", "创建时间", "实例", "名称", "卡型", "小时价"],
          [[r.get("job", "-"), r.get("at", ""), r.get("uhost", "-"),
            (r.get("name") or "-")[:16], r.get("gpu", "-"), money(r.get("hourly"))]
           for r in rows[-a.limit:]])


# ---------------------------------------------------------------- 模型准备
MODEL_REGISTRY = {
    "wan21_i2v": {
        "name": "Wan2.1 i2v（图生视频）",
        "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        "need": {
            "unet": ["Wan2.1_i2v_720p_scaled_fp8_e4m3_lightx2v.safetensors",
                     "wan2.1_i2v_480p_14B_fp8_e4m3fn.safetensors"],
            "vae": ["wan_2.1_vae.safetensors"],
            "clip": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                     "umt5_xxl_int8_convrot.safetensors"],
        },
        "files": {"vae": ("split_files/vae/wan_2.1_vae.safetensors", "vae"),
                  "clip": ("split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                           "text_encoders"),
                  "unet": ("split_files/diffusion_models/wan2.1_i2v_480p_14B_fp8_e4m3fn.safetensors",
                           "diffusion_models")},
    },
    "ltxv": {
        "name": "LTXV 2B（图生视频）",
        "repo": "Lightricks/LTX-Video",
        "need": {"unet": ["ltx-2.3-22b-distilled-1.1_transformer_only-int4_convrot.safetensors",
                          "ltx2522BCutDownToFit_24gbGpus.safetensors",
                          "ltx-video-2b-v0.9.5.safetensors"],
                 "vae": ["ltx-2.3-22b-distilled_video_vae.safetensors",
                         "ltxv-2b-0.9.6-distilled-04-25.safetensors"],
                 "clip": ["t5xxl_fp16.safetensors"]},
        "files": {},
    },
    "hunyuan_i2v": {
        "name": "HunyuanVideo i2v",
        "repo": "Comfy-Org/HunyuanVideo_repackaged",
        "need": {"unet": ["hunyuan_video_image_to_video_720p_bf16.safetensors"],
                 "vae": ["hunyuan_video_vae_bf16.safetensors"],
                 "clip": ["llava_llama3_fp16.safetensors", "llava_llama3_vision.safetensors"]},
        "files": {},
    },
}

EXTRA_PATHS_BLOCK = """# csctl provision 生成：把共享模型库挂进 ComfyUI
cs_shared:
  base_path: /model/comfyui/models
  checkpoints: checkpoints
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  clip: clip
  vae: vae
  loras: loras
  controlnet: controlnet
  clip_vision: clip_vision
  upscale_models: upscale_models
"""


def detect_env(cs: CS, inst: dict) -> dict:
    """探测 ComfyUI 目录与解释器（镜像里 pgrep 可能匹配到 bash 包装）。"""
    rc, out = ssh_run(inst, cs, "pid=$(pgrep -f 'main.py' | head -1); "
                               "echo EXE=$(readlink /proc/$pid/exe 2>/dev/null); "
                               "echo CWD=$(readlink /proc/$pid/cwd 2>/dev/null)", timeout=60)
    env = {"comfy_dir": "/root/ComfyUI", "python": ""}
    for line in out.splitlines():
        if line.startswith("EXE="):
            env["python"] = line[4:].strip()
        elif line.startswith("CWD="):
            env["comfy_dir"] = line[4:].strip() or env["comfy_dir"]
    if "python" not in os.path.basename(env.get("python") or "").lower():
        env["python"] = ""
    if not env["python"]:
        rc, probe = ssh_run(inst, cs, "for p in /root/miniconda3/envs/comfyui/bin/python "
                                     "/opt/conda/bin/python /usr/bin/python3; do "
                                     "[ -x $p ] && $p -c 'import torch' 2>/dev/null && "
                                     "echo GOOD=$p && break; done", timeout=120)
        good = [l.split("=", 1)[1].strip() for l in probe.splitlines() if l.startswith("GOOD=")]
        env["python"] = good[0] if good else "/usr/bin/python3"
    return env


def _comfy_restart(cs: CS, inst: dict, env: dict) -> bool:
    """重启 ComfyUI（镜像里没有 systemd，靠 /start.d 脚本拉起）。"""
    ssh_run(inst, cs, "pkill -f 'main.py --listen' || true; sleep 2", timeout=60)
    rc, out = ssh_run(inst, cs,
                      "cd /root/ComfyUI && setsid nohup bash /start.d/comfyui.sh "
                      "> /root/ComfyUI/comfylog.txt 2>&1 < /dev/null & echo started",
                      timeout=60)
    for _ in range(60):
        time.sleep(3)
        rc2, o2 = ssh_run(inst, cs,
                          f"{env['python']} -c \"import urllib.request as u;"
                          f"u.urlopen('http://127.0.0.1:8188/system_stats',timeout=3)\" "
                          f"&& echo UP", timeout=30)
        if "UP" in o2:
            return True
    return False


def _loader_options(cs: CS, inst: dict, env: dict) -> dict:
    code = ("import json,urllib.request as u;"
            "i=json.loads(u.urlopen('http://127.0.0.1:8188/object_info',timeout=25).read());"
            "r={};"
            "[r.setdefault(n, list(i[n]['input']['required'].get(f,[['']])[0] "
            "if isinstance(i[n]['input']['required'].get(f,[['']])[0], list) else [])) "
            "for n,f in (('UNETLoader','unet_name'),('VAELoader','vae_name'),"
            "('CLIPLoader','clip_name')) if n in i];"
            "print(json.dumps(r))")
    rc, out = ssh_run(inst, cs, f"{env['python']} -c \"{code}\"", timeout=120)
    try:
        line = [l for l in out.splitlines() if l.startswith("{")]
        return json.loads(line[-1]) if line else {}
    except Exception:
        return {}


def _match(pool: list[str], wants: list[str]) -> str | None:
    """按 basename 模糊匹配（共享库里文件常带 Wan/、Ltx/ 这类子目录前缀）。"""
    def base(x: str) -> str:
        return x.replace("\\", "/").split("/")[-1].lower()
    for w in wants:
        wb = base(w)
        for p in pool:
            pb = base(p)
            if wb == pb or wb in pb or pb in wb:
                return p
    return None


def cmd_provision(cs: CS, a) -> None:
    """确保视频模型可用：优先挂共享库，缺的再用无卡模式下载。"""
    i = cs.settle(a.id)
    if i.get("State") != "Running":
        raise CompShareError(f"实例未运行（{i.get('State')}），先 start")
    env = detect_env(cs, i)
    print(f"实例 {i['UHostId']} · ComfyUI {env['comfy_dir']} · python {env['python']}")

    rc, out = ssh_run(i, cs, "test -d /model/comfyui/models && echo MOUNT_OK || echo NO_MOUNT; "
                             "ls /model/comfyui/models 2>/dev/null | head -3", timeout=60)
    mounted = "MOUNT_OK" in out
    print(f"共享模型库 /model: {'已挂载' if mounted else '未挂载'}")

    yaml_path = f"{env['comfy_dir']}/extra_model_paths.yaml"
    rc, cur = ssh_run(i, cs, f"cat {yaml_path} 2>/dev/null | head -40", timeout=60)
    changed = False
    if mounted and EXTRA_PATHS_BLOCK.strip().splitlines()[0] not in (cur or ""):
        tmp = Path("/tmp/extra_model_paths.yaml")
        tmp.write_text((cur or "") + "\n" + EXTRA_PATHS_BLOCK, encoding="utf-8")
        if push_file(i, cs, str(tmp), yaml_path):
            changed = True
            print(f"已写入 {yaml_path}（挂载共享库）")

    if changed or a.restart:
        print("重启 ComfyUI …")
        if not _comfy_restart(cs, i, env):
            raise CompShareError("ComfyUI 重启后未就绪")
        print("ComfyUI 已就绪")

    opts = _loader_options(cs, i, env)
    unets, vaes, clips = opts.get("UNETLoader", []), opts.get("VAELoader", []), opts.get("CLIPLoader", [])
    print(f"可见：unet {len(unets)} · vae {len(vaes)} · clip {len(clips)}")

    report = {"uhost": i["UHostId"], "mounted": mounted, "yaml_written": changed, "models": {}}
    for key in [x.strip() for x in a.ensure.split(",") if x.strip()]:
        reg = MODEL_REGISTRY.get(key)
        if not reg:
            print(f"⚠ 未注册的模型: {key}")
            continue
        rows, missing = [], []
        for kind, pool in (("unet", unets), ("vae", vaes), ("clip", clips)):
            hit = _match(pool, reg["need"].get(kind, []))
            rows.append([kind, hit or "—", "✅" if hit else "❌"])
            if not hit:
                missing.append((kind, reg["need"].get(kind, [])))
        table([f"{reg['name']} · 组件", "匹配到的文件", "状态"], rows)
        report["models"][key] = {"missing": [k for k, _ in missing],
                                 "ok": not missing}
        if missing and a.download:
            print(f"\n→ 缺 {len(missing)} 项，切无卡模式下载（{reg['repo']}）")
            _download_missing(cs, i, env, reg, missing)
        elif missing:
            print("→ 缺：" + "、".join(f"{k}({v[0]})" for k, v in missing))
            print("  可加 --download 用无卡模式自动下载，或手动放到 "
                  f"{env['comfy_dir']}/models/ 下")
    out_json = Path(a.report or f"reports/provision-{i['UHostId']}.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out_json}")


def _download_missing(cs: CS, inst: dict, env: dict, reg: dict, missing: list) -> None:
    """无卡模式（CPU 0.15~0.6 元/时）下载缺失权重。"""
    cmds = ["pip install -q huggingface_hub 2>&1 | tail -1"]
    for kind, _ in missing:
        f = reg["files"].get(kind)
        if not f:
            print(f"  ⚠ {kind} 没有登记下载源，需手工放文件")
            continue
        rel, dest = f
        cmds.append(f"hf download {reg['repo']} {rel} --local-dir /root/hf_dl 2>&1 | tail -2")
        cmds.append(f"mkdir -p {env['comfy_dir']}/models/{dest} && "
                    f"cp /root/hf_dl/{rel} {env['comfy_dir']}/models/{dest}/")
    print("  切无卡模式 …")
    cs.stop(inst["UHostId"])
    cs.wait_state(inst["UHostId"], {"stopped"}, timeout=600)
    if not _try_cardless_start(cs, inst["UHostId"]):
        print("  ✗ 无卡模式不可用，改为在 GPU 模式下载（会按 GPU 计费）")
        cs.start(inst["UHostId"])
        _download_missing(cs, cs.get(inst["UHostId"]), env, reg, missing)
        return
    cs.wait_state(inst["UHostId"], {"running"}, timeout=900)
    inst = cs.get(inst["UHostId"])
    for c in cmds:
        rc, out = ssh_run(inst, cs, c, timeout=1800)
        print(f"  $ {c[:70]}… rc={rc} {out.strip().splitlines()[-1][:90] if out.strip() else ''}")
    print("  下载完成，切回 GPU 模式 …")
    cs.stop(inst["UHostId"])
    cs.wait_state(inst["UHostId"], {"stopped"}, timeout=600)
    cs.start(inst["UHostId"])
    cs.wait_state(inst["UHostId"], {"running"}, timeout=900)


def cmd_pull(cs: CS, a) -> None:
    inst = cs.get(a.id)
    t = Transfer(cs, inst, workers=a.workers)
    ok = t.pull(a.remote, a.local)
    ledger("pull", uhost=inst["UHostId"], name=inst.get("Name"),
           remote=a.remote, ok=bool(ok))
    raise SystemExit(0 if ok else 1)


def cmd_push(cs: CS, a) -> None:
    t = Transfer(cs, cs.get(a.id), workers=a.workers)
    ok = t.push(a.local, a.remote)
    raise SystemExit(0 if ok else 1)


def cmd_save_image(cs: CS, a) -> None:
    i = cs.get(a.id)
    r = cs.save_image(i["UHostId"], a.name, a.description)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("镜像做好后可删除实例；下次用 `up --image <镜像名>` 秒级复用环境。")


def cmd_init(cs: CS, a) -> None:
    print(f"Region={cs.region} Zone={cs.zone} Project={cs.project()}")
    inv = cs.inventory()
    zid = cs._zone_id(cs.zone)
    print("现货:", {k: v for k, v in (inv.get("Exclusive") or {}).get(zid, {}).items() if v})
    print("抢占现货:", {k: v for k, v in (inv.get("Spot") or {}).get(zid, {}).items() if v})
    print("余额:", cs.balance())


# --------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser("csctl", description="优云智算 GPU 实例管理（省时省钱）")
    p.add_argument("--zone", default=None, help="可用区，默认 cn-wlcb-01")
    p.add_argument("--debug", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ls", help="实例列表").set_defaults(fn=cmd_ls)
    sub.add_parser("status", help="账户/花费总览").set_defaults(fn=cmd_status)
    sub.add_parser("init", help="连通性+现货+余额自检").set_defaults(fn=cmd_init)

    sp = sub.add_parser("price", help="价格表")
    sp.add_argument("--all", action="store_true", help="所有卡型 1 卡最小规格")
    sp.add_argument("--gpu-type", default="4090")
    sp.add_argument("--gpu", type=int, default=1, help="卡数(1/2/4/8)")
    sp.add_argument("--disk", type=int, default=100)
    sp.set_defaults(fn=cmd_price)

    sp = sub.add_parser("stock", help="现货库存")
    sp.set_defaults(fn=cmd_stock)

    sp = sub.add_parser("images", help="镜像检索")
    sp.add_argument("--keyword", default="ComfyUI")
    sp.add_argument("--type", default=None, help="System/App/Custom/Other")
    sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(fn=cmd_images)

    sp = sub.add_parser("up", help="一键部署（等库存→创建→等就绪→定时关机）")
    sp.add_argument("--gpu-type", default="4090", help="4090/5090/3090/4090_48G/A100/A800/H20/V100S/3080Ti…")
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--cpu", type=int, default=0, help="0=自动取最小合法规格")
    sp.add_argument("--mem", type=int, default=0, help="GB，0=自动")
    sp.add_argument("--image", default="ComfyUI基础镜像纯净版", help="镜像名关键字或 ID")
    sp.add_argument("--charge", default="Postpay", choices=["Postpay", "Spot"], help="Spot 更便宜但可能被回收")
    sp.add_argument("--disk", type=int, default=100, help="系统盘GB，≤100 免费")
    sp.add_argument("--data-disk", type=int, default=0, help="额外数据盘GB（额外收费）")
    sp.add_argument("--name", default=f"cs-{dt.datetime.now():%m%d%H%M}")
    sp.add_argument("--password", default=None, help="不给则用镜像默认随机密码")
    sp.add_argument("--hours", type=float, default=4, help="自动定时关机兜底小时数，0=不设")
    sp.add_argument("--poll", type=int, default=60, help="库存轮询秒")
    sp.add_argument("--stock-timeout", type=int, default=0, help="等库存上限秒，0=一直等")
    sp.add_argument("--fallback", default="", help="首选卡没货时顺延的卡型，如 3090,V100S")
    sp.add_argument("--no-wait-stock", action="store_true")
    sp.add_argument("--wait-timeout", type=int, default=1200)
    sp.add_argument("--max-hourly", type=float, default=0, help="每小时花费上限，超过则中止")
    sp.add_argument("--balance-floor", type=float, default=5.0, help="余额低于此值拒绝创建，0=关闭")
    sp.add_argument("--daily-budget", type=float, default=20.0, help="当日本工具花费上限（元），0=关闭")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_up)

    for name, fn, helptext in (("start", cmd_start, "开机"), ("stop", cmd_stop, "关机(停止计费)"),
                               ("reboot", cmd_reboot, "重启")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("id")
        sp.add_argument("--wait-timeout", type=int, default=900)
        sp.add_argument("--no-wait", action="store_true")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("rm", help="删除实例")
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--archive", metavar="LOCAL_DIR", default="",
                    help="销毁前把远端目录拉回本地，如 reports/archive")
    sp.add_argument("--archive-path", default="/root/ComfyUI/output",
                    help="要归档的远端路径，默认 ComfyUI 输出目录")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("stop-at", help="设定时关机")
    sp.add_argument("id")
    sp.add_argument("--in", dest="in_hours", type=float, default=1.0)
    sp.add_argument("--at", default=None, help="HH:MM（今天或明天）")
    sp.set_defaults(fn=cmd_stop_at)

    sp = sub.add_parser("cancel-stop", help="取消定时关机")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_cancel_stop)

    sp = sub.add_parser("watch", help="空闲自动关机守护")
    sp.add_argument("--id", default=None)
    sp.add_argument("--idle-gpu-pct", type=float, default=5.0)
    sp.add_argument("--idle-cpu-pct", type=float, default=5.0)
    sp.add_argument("--idle-minutes", type=float, default=15)
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("--max-hours", type=float, default=0)
    sp.add_argument("--stop-action", default="stop", choices=["stop", "cardless", "terminate"])
    sp.add_argument("--ssh", action="store_true",
                    help="额外走 SSH 看真实 GPU 利用率与进程数（需 sshpass），判断更准")
    sp.add_argument("--once", action="store_true", help="只跑一轮（给 crontab 用）")
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("cardless", help="无卡模式开机/退出（CPU 挂机省钱）")
    sp.add_argument("id")
    sp.add_argument("mode", choices=["on", "off"])
    sp.add_argument("--wait-timeout", type=int, default=900)
    sp.set_defaults(fn=cmd_cardless)

    sp = sub.add_parser("ssh", help="打印登录方式 / 远程执行命令")
    sp.add_argument("id")
    sp.add_argument("--exec", default=None, help="在实例里执行的命令，如 'nvidia-smi'")
    sp.add_argument("--timeout", type=int, default=60)
    sp.add_argument("--detach", action="store_true", help="setsid+nohup 后台跑，SSH 断开也不停")
    sp.add_argument("--log", default="", help="detach 时的远端日志路径，如 /root/train.log")
    sp.set_defaults(fn=cmd_ssh)

    sp = sub.add_parser("cost", help="账本/花费明细")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_cost)

    sp = sub.add_parser("jobs", help="列出创建过的 job")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_jobs)

    sp = sub.add_parser("provision", help="准备视频模型：挂共享库 / 缺则无卡下载")
    sp.add_argument("id")
    sp.add_argument("--ensure", default="wan21_i2v", help="模型键，逗号分隔: wan21_i2v,ltxv,hunyuan_i2v")
    sp.add_argument("--download", action="store_true", help="缺文件时自动切无卡模式下载")
    sp.add_argument("--restart", action="store_true", help="强制重启 ComfyUI")
    sp.add_argument("--report", default="", help="报告输出路径")
    sp.set_defaults(fn=cmd_provision)

    sp = sub.add_parser("pull", help="从实例拉文件/目录（tar 流或 rsync，带校验）")
    sp.add_argument("id")
    sp.add_argument("remote")
    sp.add_argument("local")
    sp.add_argument("--workers", type=int, default=4)
    sp.set_defaults(fn=cmd_pull)

    sp = sub.add_parser("push", help="上传文件/目录到实例")
    sp.add_argument("id")
    sp.add_argument("local")
    sp.add_argument("remote")
    sp.add_argument("--workers", type=int, default=4)
    sp.set_defaults(fn=cmd_push)

    sp = sub.add_parser("save-image", help="把实例打包成自制镜像（删机不丢环境）")
    sp.add_argument("id")
    sp.add_argument("--name", required=True)
    sp.add_argument("--description", default="")
    sp.set_defaults(fn=cmd_save_image)
    return p


def main() -> int:
    a = build_parser().parse_args()
    cfg = None
    if a.zone:
        os.environ["CS_ZONE"] = a.zone
        os.environ["CS_PROJECT_ID"] = os.environ.get("CS_PROJECT_ID", "")
    cs = CS(debug=a.debug)
    try:
        a.fn(cs, a)
        return 0
    except CompShareError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断（实例仍在运行，记得 stop）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
