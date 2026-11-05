#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jobctl —— P1 单任务闭环唯一入口。

  python3 tools/p1/jobctl.py tasks                 # 任务清单（15 条）
  python3 tools/p1/jobctl.py next                  # 依赖满足的下一步
  python3 tools/p1/jobctl.py new --spec X.json     # 建一个作业（算 hash、查重、落盘）
  python3 tools/p1/jobctl.py show <job_id>         # 状态链 + 事件 + 花费
  python3 tools/p1/jobctl.py status                # 刷新 reports/p1/status.json（架构图 H 区）
  python3 tools/p1/jobctl.py validate [--deep]     # 校验 tasks / job / manifest

只做单任务：同一时刻只允许一个作业（reports/p1/.job.lock），不排队、不并发。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import ids, store, schema, state, status as status_mod  # noqa: E402
from tools.p1 import verify as verify_mod  # noqa: E402
from tools.p1 import tasks as tasks_mod  # noqa: E402

SPEC_DEFAULTS = {
    "kind": "video",
    "expect": {"ext": "mp4", "min_bytes": 300000, "duration_s": [4.8, 5.2],
               "black_ratio_max": 0.5},
    "budget": {"max_minutes": 45, "max_cost": 3.0},
    "instance": {"required_gpu": ["4090"]},
}


def _die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


def _ok(msg: str):
    print(f"✓ {msg}")


def _fmt_state(s: str) -> str:
    from tools.p1.state import state_badge
    return f"{s}({state_badge(s)})"


# ------------------------------------------------------------------ tasks
def cmd_tasks(a) -> int:
    d = tasks_mod.load()
    errs = schema.validate_tasks(d)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 1 if errs else 0
    p = tasks_mod.progress(d)
    rows = [t for t in d.get("tasks", [])
            if not a.status or t.get("status") == a.status]
    print(f"阶段 {d.get('phase')} · 共 {p['total']} 条 · 完成 {p['done']} · 进行中 {p['doing']}"
          f" · 阻塞 {p['blocked']} · 待做 {p['todo']} · 进度 {p['pct']}%")
    print()
    print(f"{'ID':<10} {'状态':<6} {'步':<3} {'任务':<34} {'依赖':<18} {'预算'}")
    print("-" * 96)
    icon = tasks_mod.STATUS_ICON
    cn = tasks_mod.STATUS_CN
    for t in rows:
        deps = ",".join(t.get("depends_on") or []) or "—"
        title = (t.get("title") or "")[:32]
        print(f"{t['id']:<10} {icon.get(t['status'], '')}{cn.get(t['status'], t['status']):<5} "
              f"{t.get('phase_step'):<3} {title:<34} {deps:<18} ¥{t.get('budget_cny', 0):g}")
    nxt = tasks_mod.next_task(d)
    print()
    print(f"下一步：{nxt['id']} {nxt['title']}" if nxt else "下一步：无（全部完成或均被阻塞）")
    bl = tasks_mod.blocked_items(d)
    if bl:
        print("阻塞：")
        for b in bl:
            print(f"  · {b}")
    if errs:
        print("\n⚠ 清单校验有问题：")
        for e in errs:
            print(f"  · {e}")
        return 1
    return 0


def cmd_next(a) -> int:
    d = tasks_mod.load()
    nxt = tasks_mod.next_task(d)
    if not nxt:
        print("无：全部完成或均被阻塞")
        return 0
    print(f"{nxt['id']}  {nxt['title']}")
    print(f"  步骤 {nxt.get('phase_step')} · 预算 ¥{nxt.get('budget_cny', 0):g}"
          f" · 依赖 {', '.join(nxt.get('depends_on') or []) or '—'}")
    for acc in nxt.get("acceptance") or []:
        print(f"  验收：{acc}")
    with store.JobLock():          # 顺手确认没有作业在跑
        pass
    return 0


# -------------------------------------------------------------------- new
def _load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _die(f"spec 不存在：{path}")
    except json.JSONDecodeError as e:
        _die(f"spec 不是合法 JSON：{e}")
    if not isinstance(spec, dict):
        _die("spec 顶层应为对象")
    return spec


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_same_hash(ph: str) -> list[tuple[str, str]]:
    out = []
    for jid in store.list_jobs():
        j = store.read_json(store.job_file(jid))
        if j and j.get("params_hash") == ph:
            out.append((jid, j.get("state", "?")))
    return out


def cmd_new(a) -> int:
    spec = _load_spec(Path(a.spec))
    kind_hint = spec.get("kind", "video")
    # 视频专属的期望项（时长/冻结/黑帧）不能套到 image 作业上：
    # 否则 png 产物会被拿去测时长，复验必挂（P2.2 导入官方模板时暴露出来的老问题）。
    VIDEO_ONLY_EXPECT = ("duration_s", "freeze")
    for k, v in SPEC_DEFAULTS.items():
        if k not in spec:
            spec[k] = v
        elif isinstance(v, dict) and isinstance(spec.get(k), dict):
            for kk, vv in v.items():
                if k == "expect" and kind_hint != "video" and kk in VIDEO_ONLY_EXPECT:
                    continue
                spec[k].setdefault(kk, vv)

    model = spec.get("model") or {}
    if not model.get("key"):
        _die("spec.model.key 缺失（例：svd_xt / wan21_i2v / ltxv）")

    # workflow_sha256：没给就按路径算
    if not model.get("workflow_sha256"):
        wf = model.get("workflow")
        if not isinstance(wf, str) or not wf:
            _die("spec.model.workflow 缺失")
        p = Path(wf) if Path(wf).is_absolute() else ROOT / wf
        if not p.exists():
            _die(f"workflow 文件不存在，无法计算 sha256：{wf}（先做 P1-T06 入库）")
        model["workflow_sha256"] = _sha256_file(p)
    spec["model"] = model

    params, expect = spec.get("params") or {}, spec.get("expect") or {}
    if not params:
        _die("spec.params 为空（没有任何可跑的参数）")

    # D3 严格管控：视频作业默认启用 freeze 门禁（spec 可显式覆盖阈值）
    kind = spec.get("kind", "video")
    if kind == "video" and "freeze" not in expect:
        expect["freeze"] = {"ratio_max": 0.20, "longest_max_s": 1.0}
    ph = store.params_hash(model, params, expect)

    same = _find_same_hash(ph)
    finished = [x for x in same if x[1] == state.DONE]
    if finished and not a.force:
        _die(f"同参数作业已完成：{finished[0][0]}（state={finished[0][1]}）。"
             f"重复跑会白烧钱；确实要重跑加 --force。")
    if same and not finished:
        print(f"⚠ 同参数作业尚未完成：{', '.join(x[0] for x in same)}（算一次新尝试）")

    # 幂等键落盘（job_id 先定，否则整体校验必然报 ID 非法）
    job = {
        "schema": schema.JOB_SCHEMA,
        "job_id": ids.new_job_id(),
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "branch": a.branch or _branch(),
        "kind": kind,
        "task_ref": a.task or spec.get("task_ref"),
        "model": model,
        "params": params,
        "params_hash": ph,
        "expect": expect,
        "freeze_policy": "strict" if kind == "video" else None,
        "budget": spec.get("budget") or SPEC_DEFAULTS["budget"],
        "instance": spec.get("instance") or SPEC_DEFAULTS["instance"],
        "state": state.CREATED,
        "state_seq": 1,
    }

    errs = schema.validate_job(job)
    if errs:
        print("✗ spec 校验不通过：", file=sys.stderr)
        for e in errs:
            print(f"  · {e}", file=sys.stderr)
        return 1

    # 原子占位：mkdir 撞了换 id（其余字段不变，无需重校验）
    last = None
    for _ in range(ids.MAX_RETRY):
        try:
            store.ensure_job_dir(job["job_id"])
            break
        except store.JobExists as e:
            last = e
            job["job_id"] = ids.new_job_id()
            continue
    else:
        _die(f"job_id 连续 {ids.MAX_RETRY} 次冲突，放弃：{last}")
    jid = job["job_id"]

    state.init_job(job, reason=f"新建作业（{job.get('task_ref') or '未关联任务'}）")
    d = store.job_dir(jid)
    _ok(f"作业已建：{jid}")
    print(f"  目录   {store.rel(d)}")
    print(f"  参数   hash {ph[:19]}…")
    print(f"  任务   {job.get('task_ref') or '—'} · 模型 {model['key']} · 期望 {expect.get('ext')}"
          f" ≥{expect.get('min_bytes')}B")
    print(f"  预算   ¥{job['budget'].get('max_cost')} / {job['budget'].get('max_minutes')} 分钟")
    print(f"  状态   {_fmt_state(job['state'])}")
    print(f"\n下一步：python3 tools/p1/jobctl.py show {jid}")
    return 0


def _branch() -> str:
    return status_mod._branch()


# ------------------------------------------------------------------- show
def cmd_show(a) -> int:
    jid = a.job_id
    if not ids.valid_job_id(jid):
        _die(f"job_id 格式非法：{jid}")
    j = store.read_json(store.job_file(jid))
    if not j:
        _die(f"作业不存在：{jid}")
    evs = state.read_events(jid)
    man = store.read_json(store.job_file(jid, "manifest.json"))

    print(f"作业 {jid}   状态 {_fmt_state(j.get('state'))}   seq={j.get('state_seq')}")
    print(f"  模型 {j['model']['key']} · {j['model']['workflow']} · 参数hash {j['params_hash'][:19]}…")
    print(f"  参数 {json.dumps(j.get('params'), ensure_ascii=False)[:110]}")
    print(f"  期望 ≥{j['expect'].get('min_bytes')}B {j['expect'].get('ext')}"
          f" 时长{j['expect'].get('duration_s')}")
    print(f"  预算 ¥{j['budget'].get('max_cost')} / {j['budget'].get('max_minutes')}min"
          f" · 任务 {j.get('task_ref') or '—'}")
    print()
    print("  事件时间线：")
    for e in evs:
        frm = e.get("from") or "—"
        print(f"   #{e.get('seq')} {e.get('at', '')}  {frm:>16} → {e.get('to'):<16} {e.get('reason', '')}")
    if man:
        print()
        print(f"  manifest: verdict={man.get('verdict')} · 产物 {len(man.get('artifacts') or [])} 个"
              f" · 实例 {man.get('instance', {}).get('uhost_id')}"
              f" · ¥{man.get('cost', {}).get('amount')}")
    cost = status_mod._ledger_cost(jid)
    if cost:
        print(f"  账本花费 ¥{cost}")
    return 0


# ----------------------------------------------------------------- verify
def cmd_verify(a) -> int:
    jid = a.job_id
    if not ids.valid_job_id(jid):
        _die(f"job_id 格式非法：{jid}")
    job = store.read_json(store.job_file(jid))
    if not job:
        _die(f"作业不存在：{jid}")
    jdir = store.job_dir(jid)
    wf = job.get("model", {}).get("workflow")
    wf_path = (ROOT / wf) if wf else None
    if wf_path and not wf_path.exists():
        print(f"⚠ workflow 不存在：{wf}（params 项将跳过）")

    res = verify_mod.verify_job(job, jdir / "artifacts", wf_path)
    print(f"作业 {jid} · workflow {wf or '—'}")
    if res.get("reason"):
        print(f"✗ {res['reason']}")
        return 1

    icon = {True: "✓", False: "✗", None: "—"}
    for art in res["artifacts"]:
        print(f"\n  {art['file']}  →  {art['verdict']}")
        for k in verify_mod.CHECK_ORDER:
            v = art["checks"].get(k)
            det = art["details"].get(k) or {}
            brief = ""
            if k == "geometry" and det.get("actual"):
                brief = f"{det['actual'][0]}x{det['actual'][1]}"
            elif k == "frames":
                brief = f"{det.get('duration')}s / {det.get('frames')}帧"
            elif k == "size" and det.get("bytes"):
                brief = f"{det['bytes']}B ≥ {det.get('min_bytes')}B"
            elif k == "black" and det.get("black_ratio") is not None:
                brief = f"黑帧 {det['black_ratio']:.1%} ≤ {det.get('max')}"
            elif k == "freeze" and det.get("ratio_max") is not None:
                brief = f"冻结 {det['ratio']:.1%} ≤ {det.get('ratio_max')}"
            elif k == "freeze" and v is None:
                brief = "未启用门禁（legacy spec）"
            elif k == "params" and det.get("diffs"):
                brief = "；".join(det["diffs"])[:60]
            elif v is None:
                brief = det.get("skipped", "不适用")
            print(f"    {icon.get(v)} {k:<9} {verify_mod.TASK_CHECKS.get(k, '')}  {brief}")

    warns = [w for a in res["artifacts"] for w in (a.get("warnings") or [])]
    if warns:
        print("\n  ⚠ 内容告警（不影响 PASS/FAIL，但请人眼确认）：")
        for w in warns:
            print(f"    · {w}")

    print()
    print(("✓ PASS：全部复验项通过" if res["verdict"] == "PASS"
           else f"✗ FAIL：{', '.join(res['artifacts'][0]['failed']) or '见上'}"))
    if warns:
        print("  ⚠ 注意：格式合格 ≠ 内容可用，上面有内容告警")
    return 0 if res["verdict"] == "PASS" else 1


# ----------------------------------------------------------------- status
def cmd_status(a) -> int:
    d = status_mod.write()
    t = d["tasks"]
    print(f"✓ 已刷新 {store.rel(store.STATUS_JSON)}")
    print(f"  任务 {t['done']}/{t['total']}（{t['pct']}%）· 进行中 {t['doing']} · 阻塞 {t['blocked']}"
          f" · 当前 {t.get('current') or '—'} · 下一步 {t.get('next') or '—'}")
    print(f"  作业 {d['job']['job_id']} · 状态 {d['job']['state']} · 花费 ¥{d['cost']['spent']}"
          f" / 预算 ¥{d['cost']['budget']}")
    if d["blockers"]:
        print("  阻塞：")
        for b in d["blockers"]:
            print(f"    · {b}")
    print("  架构图：python3 tools/arch.py（或等 cron */10 自动刷新）")
    return 0


# --------------------------------------------------------------- validate
def cmd_validate(a) -> int:
    res = schema.validate_all(deep=a.deep, job_id=a.job)
    bad = 0
    for name, errs in res.items():
        if errs:
            bad += 1
            print(f"✗ {name}")
            for e in errs:
                print(f"    · {e}")
        else:
            print(f"✓ {name}")
    print()
    print(f"{'全部通过' if not bad else f'{bad} 个对象有问题'}（共 {len(res)} 个）")
    return 1 if bad else 0


# ------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobctl", description="P1 单任务闭环 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("tasks", help="任务清单")
    sp.add_argument("--status", choices=list(tasks_mod.STATUS_CN))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_tasks)

    sub.add_parser("next", help="依赖满足的下一步").set_defaults(fn=cmd_next)

    sp = sub.add_parser("new", help="新建作业")
    sp.add_argument("--spec", required=True, help="作业 spec JSON")
    sp.add_argument("--task", help="关联开发任务，如 P1-T09")
    sp.add_argument("--branch", help="覆盖分支名（默认取 git HEAD）")
    sp.add_argument("--force", action="store_true", help="同参数已完成也允许再跑")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("show", help="查看作业")
    sp.add_argument("job_id")
    sp.set_defaults(fn=cmd_show)

    sub.add_parser("status", help="刷新 status.json").set_defaults(fn=cmd_status)

    sp = sub.add_parser("verify", help="校验作业产物（6 条复验项）")
    sp.add_argument("job_id")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("validate", help="校验 tasks / job / manifest")
    sp.add_argument("--deep", action="store_true", help="核对产物大小与 sha256")
    sp.add_argument("--job", help="只校验指定作业")
    sp.set_defaults(fn=cmd_validate)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
