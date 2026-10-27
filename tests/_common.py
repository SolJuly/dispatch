#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/_common.py — 三个边界场景测试的公共设施。

约束（与项目 Phase 0 安全规则一致）：
  · 不修改 csctl.py 的任何行为，只调用它的 CLI
  · 不打印 API Token / SSH 密码 / Jupyter token（统一走 redact()）
  · 所有创建出来的实例都必须被清理，残留即视为测试失败
  · 支持 --dry-run：只打印计划，不做任何云端变更
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSCTL = ROOT / "csctl.py"
REPORTS = ROOT / "reports"
LEDGER = ROOT / ".compshare" / "ledger.jsonl"
COST_JSON = REPORTS / "test_cost_summary.json"
COST_MD = REPORTS / "test_cost_summary.md"

sys.path.insert(0, str(ROOT))

# 测试专用实例名前缀：所有脚本只碰这个前缀下的实例
PREFIX = "test-"

# 官方状态枚举（来源 compshare/cs.py STATE_CN，控制台中文 ← API 英文）
STATE_CN = {
    "Running": "运行中",
    "Stopped": "已关机",
    "Starting": "启动中",
    "Stopping": "关机中",
    "Install": "安装/部署中",
    "Rebooting": "重启中",
    "Interrupted": "已中断",
    "Terminating": "删除中",
    "Unknown": "未知",
}
DOCUMENTED_STATES = set(STATE_CN)

_TRANSIENT = {"Starting", "Stopping", "Install", "Rebooting", "Terminating"}


# ---------------------------------------------------------------- 脱敏
_SECRET_PATTERNS = [
    (re.compile(r"(?im)^(\s*密码\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(?im)^(\s*password\s*:\s*)\S+"), r"\1***"),
    (re.compile(r"(token=)[A-Za-z0-9%._\-]+"), r"\1***"),
    (re.compile(r"(?i)(AccessKey|SecretKey|PublicKey|PrivateKey)\s*[=:]\s*\S+"), r"\1=***"),
]


def redact(text: str) -> str:
    """抹掉密码 / token / 密钥，保证日志与报告里不出现凭据。"""
    if not text:
        return ""
    out = text
    for pat, rep in _SECRET_PATTERNS:
        out = pat.sub(rep, out)
    return out


# ---------------------------------------------------------------- 时间
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hms(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time()).strftime("%H:%M:%S")


# ---------------------------------------------------------------- csctl 调用
class Result:
    __slots__ = ("cmd", "t_start", "t_end", "rc", "out", "err", "ok", "skipped")

    def __init__(self, cmd, t_start, t_end, rc, out, err, skipped=False):
        self.cmd, self.t_start, self.t_end = cmd, t_start, t_end
        self.rc, self.out, self.err = rc, out, err
        self.skipped = skipped
        self.ok = (rc == 0 and not skipped)

    @property
    def dur(self) -> float:
        return self.t_end - self.t_start

    def brief(self, n: int = 200) -> str:
        s = (self.out or "").strip() or (self.err or "").strip()
        return s[:n].replace("\n", " ⏎ ")


def csctl(*args: str, timeout: int = 900, dry_run: bool = False) -> Result:
    """执行 csctl 子命令。dry_run 时不真正运行，只返回计划。"""
    cmd = [sys.executable, str(CSCTL), *args]
    if dry_run:
        t = time.time()
        return Result(cmd, t, t, 0, "[dry-run] " + " ".join(cmd[2:]), "", skipped=True)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(ROOT))
        return Result(cmd, t0, time.time(), r.returncode, redact(r.stdout), redact(r.stderr))
    except subprocess.TimeoutExpired as e:
        return Result(cmd, t0, time.time(), 124,
                      redact((e.stdout or b"").decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")),
                      "timeout")


def popen_csctl(*args: str):
    """启动 csctl 子命令但不等待（用于「创建后立刻动手」的场景）。

    加 -u 关闭 Python 输出缓冲，否则管道下 print 会被块缓冲，
    readline 拿不到「实例已创建」那行。
    """
    cmd = [sys.executable, "-u", str(CSCTL), *args]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(ROOT), bufsize=1)


class StreamWatch:
    """后台线程持续读进程 stdout，主线程可带超时地等某一行出现。"""

    _UID_RE = re.compile(r"实例已创建:\s*(uhost-[A-Za-z0-9]+)")

    def __init__(self, proc):
        import queue
        import threading
        self.proc = proc
        self.q: "queue.Queue[str]" = queue.Queue()
        self.lines: list[str] = []
        self.err_lines: list[str] = []
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()
        self._t2 = threading.Thread(target=self._err_reader, daemon=True)
        self._t2.start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                self.lines.append(line)
                self.q.put(line)
        except Exception:
            pass

    def _err_reader(self):
        try:
            for line in self.proc.stderr:
                self.err_lines.append(line)
        except Exception:
            pass

    def stderr_text(self) -> str:
        return redact("".join(self.err_lines))

    def wait_uid(self, timeout: int = 300) -> str | None:
        """等「实例已创建: uhost-xxx」出现，返回 uid。"""
        import queue
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            m = self._UID_RE.search(line)
            if m:
                return m.group(1)
        return None

    def drain(self, timeout: float = 2.0) -> str:
        """收集剩余输出（不阻塞太久）。"""
        import queue
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.q.get(timeout=0.3)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
        return redact("".join(self.lines))

    def text(self) -> str:
        out = redact("".join(self.lines))
        err = self.stderr_text()
        if err.strip():
            out += "\n[stderr]\n" + err
        return out


# ---------------------------------------------------------------- 实例查询（只读）
_CS_CACHE = {}


def _cs():
    if "cs" not in _CS_CACHE:
        from compshare.cs import CS
        _CS_CACHE["cs"] = CS()
    return _CS_CACHE["cs"]


def instances(refresh: bool = True) -> list[dict]:
    """只读拉实例列表。异常时返回空列表并标记。"""
    try:
        return list(_cs().instances(refresh=refresh))
    except Exception:
        return []


def by_name(name: str) -> dict | None:
    for i in instances():
        if (i.get("Name") or "") == name:
            return i
    return None


def by_id(uid: str) -> dict | None:
    for i in instances():
        if i.get("UHostId") == uid:
            return i
    return None


def state_of(uid: str) -> str:
    i = by_id(uid)
    return (i or {}).get("State") or "MISSING"


def test_instances(prefix: str = PREFIX) -> list[dict]:
    return [i for i in instances() if (i.get("Name") or "").startswith(prefix)]


# ---------------------------------------------------------------- 等待
def wait_until(pred, timeout: int = 600, interval: float = 3.0, desc: str = ""):
    """轮询直到 pred() 为真。返回 (是否成功, 已等待秒数)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pred():
                return True, time.time() - t0
        except Exception:
            pass
        time.sleep(interval)
    return False, time.time() - t0


def wait_absent(uid: str, timeout: int = 300) -> tuple[bool, float]:
    return wait_until(lambda: by_id(uid) is None, timeout=timeout, interval=3.0)


def wait_running(uid: str, timeout: int = 900) -> tuple[bool, float]:
    return wait_until(lambda: state_of(uid) == "Running", timeout=timeout, interval=5.0)


# ---------------------------------------------------------------- 清理
def cleanup(names: list[str], dry_run: bool = False, log=None) -> list[dict]:
    """删除指定名称的测试实例。幂等：不存在的跳过。返回处理明细。"""
    rows = []
    for n in names:
        inst = by_name(n)
        if not inst:
            rows.append({"name": n, "id": "-", "action": "skip", "note": "不存在"})
            if log:
                log(f"  清理 {n}: 不存在，跳过")
            continue
        uid = inst.get("UHostId")
        r = csctl("rm", uid, "--yes", dry_run=dry_run, timeout=600)
        rows.append({"name": n, "id": uid, "action": "rm",
                     "rc": r.rc, "note": redact(r.brief(120))})
        if log:
            log(f"  清理 {n} ({uid}): rc={r.rc}")
        if not dry_run:
            ok, waited = wait_absent(uid, timeout=240)
            rows[-1]["absent"] = ok
            rows[-1]["waited"] = round(waited, 1)
    return rows


def assert_no_residue(prefix: str = PREFIX) -> list[dict]:
    """返回残留的测试实例（应为空）。"""
    return test_instances(prefix)


# ---------------------------------------------------------------- 费用
def ledger_terminate_costs(uhosts: set[str]) -> tuple[float, list[dict]]:
    """从 .compshare/ledger.jsonl 汇总本批实例的 terminate 记账花费。"""
    total, rows = 0.0, []
    if not LEDGER.exists():
        return 0.0, rows
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "terminate" and r.get("uhost") in uhosts:
            c = float(r.get("cost") or 0)
            total += c
            rows.append({"uhost": r.get("uhost"), "name": r.get("name"),
                         "run_h": r.get("run_h"), "cost": round(c, 3), "at": r.get("at")})
    return round(total, 3), rows


def record_cost(test: str, items: list[dict]) -> None:
    """把本测试的费用写入 reports/test_cost_summary.json 并重渲染 md（幂等覆盖）。"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    data = {"tests": {}}
    if COST_JSON.exists():
        try:
            data = json.loads(COST_JSON.read_text(encoding="utf-8"))
        except Exception:
            data = {"tests": {}}
    total = round(sum(float(i.get("cost") or 0) for i in items), 3)
    data.setdefault("tests", {})[test] = {"at": now(), "items": items, "total": total}
    data["grand_total"] = round(sum(float(v.get("total") or 0)
                                    for v in data["tests"].values()), 3)
    COST_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 压力测试费用汇总", "",
          f"> 更新：{now()}  ",
          f"> 数据源：`.compshare/ledger.jsonl` 的 terminate 记账  ",
          f"> **累计 ¥{data['grand_total']:.3f}**", ""]
    for name, v in sorted(data["tests"].items()):
        md.append(f"## {name}")
        md.append("")
        md.append(f"> 最近一次运行：{v.get('at')}　小计 **¥{float(v.get('total') or 0):.3f}**")
        md.append("")
        if v.get("items"):
            md.append("| 实例 | 名称 | 运行小时 | 花费 |")
            md.append("|---|---|---|---|")
            for it in v["items"]:
                md.append(f"| `{it.get('uhost','-')}` | {it.get('name','-')} | "
                          f"{float(it.get('run_h') or 0):.3f} | ¥{float(it.get('cost') or 0):.3f} |")
        else:
            md.append("（本批无实例产生费用）")
        md.append("")
    COST_MD.write_text("\n".join(md), encoding="utf-8")


# ---------------------------------------------------------------- 报告
def write_report(name: str, title: str, meta: dict, sections: list[str]) -> Path:
    """写 reports/<name>.md，覆盖式（幂等）。"""
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / name
    lines = [f"# {title}", ""]
    lines.append(f"> 生成时间：{now()}（Asia/Shanghai）  ")
    for k, v in meta.items():
        lines.append(f"> {k}：{v}  ")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.extend(sections)
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def table(headers: list[str], rows: list[list]) -> str:
    h = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join(["---"] * len(headers)) + "|"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([h, sep, body]) if rows else "\n".join([h, sep, "| （无） |" + " |" * (len(headers) - 1)])


def fence(s: str, lang: str = "") -> str:
    return f"```{lang}\n{s.rstrip()}\n```"


# ---------------------------------------------------------------- CLI 参数
def parse_args(description: str):
    import argparse
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不做任何云端变更")
    p.add_argument("--gpu-type", default="V100S",
                   help="测试用卡型（默认 V100S：现货最充足且比 4090 便宜）")
    p.add_argument("--fallback", default="4090,3090",
                   help="首选卡无货时的顺延卡型，逗号分隔（默认 4090,3090）")
    p.add_argument("--hours", type=float, default=1.0, help="自动关机兜底小时数（默认 1h）")
    p.add_argument("--count", type=int, default=0, help="覆盖并发数量（默认按各测试设定）")
    p.add_argument("--keep", action="store_true", help="保留实例不清理（调试用，慎用）")
    return p.parse_args()


def banner(text: str) -> None:
    print(f"\n{'=' * 66}\n{text}\n{'=' * 66}", flush=True)


def say(msg: str) -> None:
    print(f"[{hms()}] {msg}", flush=True)
