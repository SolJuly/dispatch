#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jobctl —— P1 单任务闭环唯一入口。

  python3 tools/p1/jobctl.py tasks                 # 任务清单（15 条）
  python3 tools/p1/jobctl.py next                  # 依赖满足的下一步
  python3 tools/p1/jobctl.py new --spec X.json     # 建一个作业（算 hash、查重、落盘）
  python3 tools/p1/jobctl.py show <job_id>         # 状态链 + 事件 + 花费
  python3 tools/p1/jobctl.py status                # 刷新 reports/p1/status.json（架构图 H 区）
  python3 tools/p1/jobctl.py validate [--deep]     # 校验 tasks / job / manifest
  python3 tools/p1/jobctl.py rm <job_id> [...]     # 删除作业（默认进 .trash，--purge 彻底删）

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

# 提交锁排队窗口（秒）：并发/双击提交时，第二个请求等第一个建完再查重，
# 而不是「两个都查到没重复、各建一个作业各开一台 GPU」。
SUBMIT_LOCK_WAIT = 15
# 只有「主链中还没走到终态」的作业才叫进行中（幂等复用）。
# FAILED_* / FAILED / ABORTED / RESUMING 不在主链：失败作业不该被复用，
# 否则用户重提一个失败任务会拿到那个死掉的 job_id 且什么都不执行（2026-11-08 审计实测）。
DEDUP_ACTIVE_STATES = set(state.MAIN_CHAIN) - {state.DONE}
# 复用标记：webui / p20ctl 据此识别「这次没有新建作业」，避免重复 spawn run.py
DEDUP_REUSE_MARK = "DEDUP_REUSE"

SPEC_DEFAULTS = {
    "kind": "video",
    "expect": {"ext": "mp4", "min_bytes": 300000, "duration_s": [4.8, 5.2],
               "black_ratio_max": 0.5},
    "budget": {"max_minutes": 45, "max_cost": 3.0},
    "instance": {"required_gpu": ["4090"]},
}

# 生命周期相关字段不放进 SPEC_DEFAULTS：默认值由 tools/p2/lifecycle.py 一处算，
# 放进这里就变成"两个地方各有一份默认"，迟早漂移。
LIFECYCLE_KEYS = ("task_class", "task_class_source", "idle_tier", "idle_after_done_s")

# 不是生命周期字段，但同样由 spec 带入 job.json：用户"想让这个作业跑"的意图。
# 和 LIFECYCLE_KEYS 分开列，是因为它不参与"实例留多久"的计算，只影响补位。
INTENT_KEYS = ("auto_run",)


def _die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


def _ok(msg: str):
    print(f"✓ {msg}")


def _fmt_state(s: str) -> str:
    from tools.p1.state import state_badge
    return f"{s}({state_badge(s)})"


def _lifecycle_fields(spec: dict) -> dict:
    """从 spec 算出写进 job.json 的生命周期字段。

    策略只在这里算一次（`tools/p2/lifecycle.py`），reaper / webui / run 都读结果 ——
    否则同一个"长任务该空转多久"会在四个地方各算一遍，改一处漏三处。

    spec 里没有生命周期字段时按短任务处理：省钱方向，判错只是少留一会儿。
    """
    from tools.p2 import lifecycle
    tc, src = lifecycle.normalize_task_class(spec.get("task_class"))
    # 显式给了 task_class 但来源被标成 estimated 的（以后估算引入）保留原标记
    src = str(spec.get("task_class_source") or src)
    tier = lifecycle.normalize_tier(spec.get("idle_tier"), task_class=tc)
    return {
        "task_class": tc,
        "task_class_source": src,
        "idle_tier": tier,
        "idle_after_done_s": lifecycle.long_idle_s(tc, tier),
    }


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


def cmd_new(a) -> int:
    spec = _load_spec(Path(a.spec))
    kind_hint = spec.get("kind", "video")
    # 视频专属的期望项（时长/冻结/黑帧）不能套到 image 作业上：
    # 否则 png 产物会被拿去测时长，复验必挂（P2.2 导入官方模板时暴露出来的老问题）。
    VIDEO_ONLY_EXPECT = ("duration_s", "freeze")
    # registry 工作流的产出来自输入素材（如放大的视频），时长/冻结阈值不该套模板默认值；
    # 以 workflow 配置里的 expect_defaults 为准，缺省就交给 verify 跳过。
    registry_wf = bool((spec.get("model") or {}).get("registry"))
    for k, v in SPEC_DEFAULTS.items():
        if k not in spec:
            spec[k] = v
        elif isinstance(v, dict) and isinstance(spec.get(k), dict):
            for kk, vv in v.items():
                if k == "expect" and (kind_hint != "video" or registry_wf) and kk in VIDEO_ONLY_EXPECT:
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
    dedup = store.task_dedup_key(model, params)

    # 提交锁：把「查重 → 决定复用/拒绝 → mkdir 占位」变成一个原子操作。
    # 没有这把锁，双击提交的两个进程会同时查到「无重复」、各建一个作业各开一台 GPU。
    # 用独立的 .submit.lock，不与执行用的 .job.lock 互锁（执行可以跑几十分钟，
    # 提交不该被它堵住；两个提交之间才需要互斥）。
    try:
        with store.JobLock(path=store.SUBMIT_LOCK_FILE, wait=SUBMIT_LOCK_WAIT):
            same = store.find_same_task(model, params)
            active = [x for x in same if x[1] in DEDUP_ACTIVE_STATES]
            finished = [x for x in same if x[1] == state.DONE]
            dead = [x for x in same if x[1] not in DEDUP_ACTIVE_STATES
                    and x[1] != state.DONE]

            if not a.force:
                if active:
                    # 进行中：幂等复用，返回已有 job_id，绝不新建第二份
                    jid, st = active[0]
                    print(f"{store.DEDUP_REUSE_MARK} {jid}")
                    _ok(f"同参数作业已在 {_fmt_state(st)}，复用：{jid}（未新建、未重复建机）")
                    return 0
                if finished:
                    _die(f"同参数作业已完成：{finished[0][0]}（state={finished[0][1]}）。"
                         f"重复跑会白烧钱；确实要重跑加 --force。")
                if dead:
                    # 失败/中止的旧作业不复用、也不拦：用户重提就是要再跑一次
                    print(f"  同参数旧作业已结束（{dead[0][1]}），按新提交处理：{dead[0][0]}")

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
                "task_dedup_key": dedup,
                "expect": expect,
                "freeze_policy": "strict" if kind == "video" else None,
                "budget": spec.get("budget") or SPEC_DEFAULTS["budget"],
                "instance": spec.get("instance") or SPEC_DEFAULTS["instance"],
                "state": state.CREATED,
                "state_seq": 1,
            }

            # ---- 生命周期（2026-11-10 定版）----
            # 三条事实落进 job.json，reaper 只读这里，不再猜：
            #   task_class         短任务 / 长任务（用户在 UI 勾的，或默认短）
            #   task_class_source  user / estimated / default（以后校准估算用）
            #   idle_tier          用户选的三档（60/90/120），短任务为 None
            #   idle_after_done_s  实算出来的空转秒数（审计看这个，不用再算一遍）
            job.update(_lifecycle_fields(spec))
            # auto_run：只有"勾了立即执行但被并发挡住"的作业才带。
            # 没勾的作业不带这个字段 —— 它们该等人点「执行」，不该被自动拉起。
            if spec.get("auto_run"):
                job["auto_run"] = True

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
    except store.JobLocked as e:
        _die(f"提交排队超时：{e}")

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
    esc = ((j.get("instance") or {}).get("oom_escalation")) or {}
    if esc.get("floor_vram"):
        atts = esc.get("attempts") or []
        trail = "：".join(f"{a.get('gpu')}({a.get('vram')}G)¥{a.get('cost')}" for a in atts[-3:])
        print(f"  ⤴ 已升卡 {len(atts)} 次 → 下次要求 ≥{esc['floor_vram']}G（{trail}）")
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

    # registry 作业要看**渲染后**的图（frames/duration 是占位符，模板里还是默认值），
    # 否则会像 run.py 之前那样把改过帧数的作业误判成 params 不匹配。
    reg = (job.get("model") or {}).get("registry") or {}
    rr = store.read_json(jdir / "render.json") or {}
    graph = rr.get("graph") or {}
    if reg.get("workflow_key") and graph:
        from tools.p20 import registry as p20registry
        try:
            wf_cfg = p20registry.get_workflow(reg["workflow_key"],
                                              version=reg.get("workflow_version"))
        except Exception as e:
            print(f"⚠ 取不到工作流配置（{e}），按 legacy 方式校验")
            wf_cfg = None
        if wf_cfg:
            res = verify_mod.verify_job(job, jdir / "artifacts", None,
                                        rendered_graph=graph, wf_config=wf_cfg)
        else:
            res = verify_mod.verify_job(job, jdir / "artifacts", wf_path)
    else:
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


# --------------------------------------------------------------------- rm
def cmd_rm(a) -> int:
    """删除作业目录：默认移到 jobs/.trash/（可恢复），--purge 彻底删除。"""
    import shutil

    ids_ = [x for x in a.job_id if x]
    if not ids_:
        _die("至少给一个 job_id")
    trash = store.JOBS_DIR / ".trash"
    rows = []
    for jid in ids_:
        d = store.JOBS_DIR / jid
        if not d.is_dir():
            rows.append((jid, "NOT_FOUND", ""))
            continue
        state_ = ""
        try:
            state_ = json.loads((d / "job.json").read_text(encoding="utf-8")).get("state") or ""
        except (OSError, json.JSONDecodeError):
            pass
        # 可删 = 终态 ∪ 失败分支。FAILED_PROVISION/FAILED_RUN/FAILED_FETCH/FAILED_VERIFY
        # 是「中间失败态」，正常流程会折成 FAILED 再进 TERMINAL；若流程本身崩在
        # 折叠之前，作业就卡在这个状态且永远不会前进 —— 它不是「在跑」，是可以删的。
        # 真正要保护的只有主链上还可能继续的状态（CREATED/RUNNING/PROVISIONING…）。
        deletable = state_ in state.TERMINAL or state_ in getattr(state, "FAIL_BRANCH", ())
        if deletable or not state_:
            pass
        elif not a.force:
            rows.append((jid, "REFUSED", f"状态 {state_} 未结束（--force 可强删）"))
            continue
        # 发布副本
        pub = store.ROOT / "reports" / "p1" / "jobs" / jid  # 仅用于路径基准
        pub2 = Path("/srv/dsh-pub/jobs") / jid
        if a.purge:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(pub2, ignore_errors=True)
            rows.append((jid, "PURGED", ""))
        else:
            trash.mkdir(parents=True, exist_ok=True)
            dst = trash / jid
            if dst.exists():
                dst = trash / f"{jid}-{dt.datetime.now():%Y%m%d%H%M%S}"
            shutil.move(str(d), str(dst))
            shutil.rmtree(pub2, ignore_errors=True)
            rows.append((jid, "TRASHED", str(dst.relative_to(store.ROOT))))
    for jid, what, extra in rows:
        print(f"{'✓' if what in ('TRASHED', 'PURGED') else '✗'} {jid} {what} {extra}")
    bad = sum(1 for _, w, _ in rows if w not in ("TRASHED", "PURGED"))
    print(f"共 {len(rows)} 个：成功 {len(rows) - bad}，失败 {bad}")
    return 1 if bad else 0


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

    sp = sub.add_parser("rm", help="删除作业（默认进 .trash，可恢复）")
    sp.add_argument("job_id", nargs="+")
    sp.add_argument("--purge", action="store_true", help="彻底删除，不进回收站")
    sp.add_argument("--force", action="store_true", help="未结束的作业也删")
    sp.set_defaults(fn=cmd_rm)

    sp = sub.add_parser("validate", help="校验 tasks / job / manifest")
    sp.add_argument("--deep", action="store_true", help="核对产物大小与 sha256")
    sp.add_argument("--job", help="只校验指定作业")
    sp.set_defaults(fn=cmd_validate)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
