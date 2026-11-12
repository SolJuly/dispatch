#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p20ctl —— P2.0 Workflow/Model Registry 统一入口。

  python3 tools/p20/p20ctl.py init                       # 建库/重建索引
  python3 tools/p20/p20ctl.py import [--check]           # 从 registry/*.json 导入
  python3 tools/p20/p20ctl.py validate                   # 全量体检（配置+图绑定+兼容性）
  python3 tools/p20/p20ctl.py list workflows|models      # 列表
  python3 tools/p20/p20ctl.py show workflow|model <key>  # 详情
  python3 tools/p20/p20ctl.py resolve --workflow K --model M --set prompt=... --set seed=1
  python3 tools/p20/p20ctl.py new --workflow K --model M --params p.json [--run --gpu 4090]
  python3 tools/p20/p20ctl.py compat list
  python3 tools/p20/p20ctl.py compat verify --workflow K --model M --evidence <job_id>

设计约束：
  - 不修改 P1 的 job 状态机；new 只是生成标准化 spec 后调用 jobctl new。
  - 兼容性未验证默认拒绝；--allow-experimental 会写进 job 的 registry 元数据，便于审计。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p20 import config_schema as cs  # noqa: E402
from tools.p20 import registry, render  # noqa: E402

JOBCTL = ROOT / "tools" / "p1" / "jobctl.py"
RUNNER = ROOT / "tools" / "p1" / "run.py"
SPEC_DIR = ROOT / "reports" / "p2" / "p20" / "specs"
INPUTS_DIR = ROOT / "reports" / "p2" / "p21" / "inputs"
JOB_ID_RE = re.compile(r"p1-\d{8}T\d{4}-[0-9a-f]{6}")


def _die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


def _ok(msg: str):
    print(f"✓ {msg}")


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _parse_set(pairs: list[str]) -> dict:
    out = {}
    for kv in pairs or []:
        if "=" not in kv:
            _die(f"--set 需要 k=v：{kv}")
        k, v = kv.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _load_params(path: str | None, sets: list[str]) -> dict:
    out = {}
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        out.update(cs.load_json(p))
    out.update(_parse_set(sets))
    return out


def _atomic_write_json(path: Path, obj) -> None:
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ------------------------------------------------------------------ 命令
def cmd_init(a) -> int:
    if a.force and registry.DB_PATH.exists():
        registry.DB_PATH.unlink()
    r = registry.import_all(registry.DB_PATH, quiet=False)
    _ok(f"registry 就绪：{r['db']}")
    return 0


def cmd_import(a) -> int:
    if a.check:
        fp = registry.source_fingerprint()
        if not registry.DB_PATH.exists():
            print(f"✗ 库不存在，需要导入（fingerprint {fp[:12]}）")
            return 1
        c = registry.connect()
        try:
            cur = c.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
        finally:
            c.close()
        fresh = bool(cur and cur["v"] == fp)
        print(("✓ 已是最新" if fresh else "✗ 源文件已变化，需要 import") +
              f"（src {fp[:12]} / db {(cur['v'][:12] if cur else '—')}）")
        return 0 if fresh else 1
    r = registry.import_all(registry.DB_PATH, quiet=False)
    _ok(f"导入完成：{r['workflows']} 工作流 / {r['models']} 模型 / 显式兼容 {r['compat_explicit']}")
    return 0


def cmd_validate(a) -> int:
    r = registry.validate_all(registry.DB_PATH)
    if a.json:
        _dump(r)
        return 0 if r["ok"] else 1
    for e in r["errors"]:
        print(f"✗ {e}")
    for w in r["warnings"]:
        print(f"⚠ {w}")
    print(f"\n工作流 {r['counts'].get('workflows', 0)} · 模型 {r['counts'].get('models', 0)} · "
          f"verified {r['counts'].get('verified', 0)} · equivalent {r['counts'].get('equivalent', 0)} · "
          f"experimental {r['counts'].get('experimental', 0)} · "
          f"incompatible {r['counts'].get('incompatible', 0)}")
    print("✓ 全部通过" if r["ok"] else f"\n✗ {len(r['errors'])} 个错误")
    return 0 if r["ok"] else 1


def cmd_list(a) -> int:
    if a.what == "workflows":
        rows = registry.list_workflows(registry.DB_PATH)
        if a.json:
            _dump(rows)
            return 0
        print(f"{'key':<28} {'v':<3} {'kind':<6} {'model':<14} {'compat':<12} {'nodes':<5} name")
        print("-" * 110)
        for w in rows:
            print(f"{w['key']:<28} {w['version']:<3} {w['kind']:<6} {w.get('model_key', '-'):<14} "
                  f"{w['compat']['status']:<12} {len(w.get('graph_obj') or {}):<5} {w.get('name', '')}")
        return 0
    rows = registry.list_models(registry.DB_PATH)
    if a.json:
        _dump(rows)
        return 0
    print(f"{'key':<16} {'v':<3} {'kind':<6} {'family':<10} {'files':<6} name")
    print("-" * 86)
    for m in rows:
        print(f"{m['key']:<16} {m['version']:<3} {m['kind']:<6} {m.get('family') or '-':<10} "
              f"{len(m.get('files') or {}):<6} {m.get('name', '')}")
    return 0


def cmd_show(a) -> int:
    if a.what == "workflow":
        w = registry.get_workflow(a.key, version=a.version, db_path=registry.DB_PATH)
        compat = registry.compat_for(w["key"], (w.get("model") or {}).get("key"), registry.DB_PATH)
        if a.json:
            _dump({"workflow": w, "compat": compat})
            return 0
        print(f"工作流 {w['key']} · v{w['version']} · {w['kind']} · {w.get('name')}")
        print(f"  图       {w.get('graph') or w.get('source_path') or '—'}")
        print(f"  图 sha   {w.get('graph_sha256', '—')}")
        print(f"  模型     {(w.get('model') or {}).get('key')} · 槽位 "
              f"{list(((w.get('model') or {}).get('slots') or {}))}")
        print(f"  兼容性   {compat['status']}（{compat['basis']}，证据 {compat['evidence']}）")
        print("  参数：")
        for name, spec in (w.get("params") or {}).items():
            bs = ", ".join(f"{b['node']}.{b['input']}" for b in (spec.get("bindings") or []))
            print(f"    {name:<12} {spec.get('type'):<6} 默认 {str(spec.get('default', '—')):<12} "
                  f"→ {bs}")
        out = w.get("output") or {}
        print(f"  输出     {out.get('node')}.{out.get('input')} · {out.get('kind')}/{out.get('ext')}")
        return 0
    m = registry.get_model(a.key, version=a.version, db_path=registry.DB_PATH)
    if a.json:
        _dump(m)
        return 0
    print(f"模型 {m['key']} · v{m['version']} · {m['kind']} · {m.get('name')}")
    print(f"  主文件   {m.get('primary')}")
    for role, f in (m.get("files") or {}).items():
        print(f"    {role:<12} {f.get('name')}  ({f.get('loader')}.{f.get('input')})")
    print(f"  默认值   {m.get('defaults')}")
    print(f"  约束     {m.get('constraints')}")
    print(f"  setup    {m.get('setup')}")
    return 0


def cmd_resolve(a) -> int:
    params = _load_params(a.params, a.set)
    try:
        r = registry.resolve(a.workflow, model_key=a.model, params=params,
                             allow_experimental=a.allow_experimental, db_path=registry.DB_PATH)
    except (registry.RegistryError, cs.ConfigError) as e:
        _die(str(e))
    wf, model = r["workflow"], r["model"]
    exp = render.expect_for(wf, r["params"])
    graph, meta = render.render(wf, model, r["params"], a.prefix or "preview",
                                graph=wf.get("graph_obj"))
    if a.json:
        _dump({"workflow": {"key": wf["key"], "version": wf["version"]},
               "model": {"key": model["key"], "version": model["version"]},
               "compat": r["compat"], "params": r["params"],
               "defaults_applied": r["defaults_applied"], "expect": exp,
               "render": {k: v for k, v in meta.items() if k != "params"},
               "graph": graph if a.show_graph else None})
        return 0
    print(f"工作流 {wf['key']} v{wf['version']} × 模型 {model['key']} v{model['version']}")
    print(f"兼容性 {r['compat']['status']}（{r['compat']['basis']}）")
    print(f"参数   {json.dumps(r['params'], ensure_ascii=False)[:300]}")
    if r["defaults_applied"]:
        print(f"补默认 {r['defaults_applied']}")
    print(f"期望   {json.dumps(exp, ensure_ascii=False)}")
    print(f"渲染   {len(graph)} 节点 · graph_sha256 {meta['graph_sha256'][:16]}… · "
          f"无残留占位符")
    return 0


def _video_keys(wf: dict) -> set[str]:
    """注册表里声明为 type=video 的参数名。

    参数名不等于语义：animate2_distilled 的驱动视频叫 image_2，vace/scail 叫 image，
    靠正则匹配 video 会漏掉 7/9 个视频输入工作流（trim 与真实帧数估价同时失效）。
    """
    return {k for k, v in (wf.get("params") or {}).items()
            if isinstance(v, dict) and v.get("type") == "video"}


def _normalize_image(params: dict, video_keys: set[str] | None = None) -> dict:
    """把本地图片归一成「实例 input/ 下的文件名」。

    实现：按内容哈希命名，复制到 reports/p2/p21/inputs/<name>；
    params.image 只留文件名（参与渲染与 params_hash），不额外塞参数键
    （registry 的参数契约是严格校验的，多一个键就会被拒）。
    """
    import hashlib
    import shutil
    # 兼容 image / image_2 / mask / mask_2：官方模板里首尾帧、多参考图都用 image_2
    for key in [k for k in params if isinstance(k, str) and re.fullmatch(r"(?:image|mask|audio|video)(?:_\d+)?", k)]:
        raw = params.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / raw
        if not p.is_file():
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
        name = f"in-{h}{p.suffix.lower() or '.png'}"
        INPUTS_DIR.mkdir(parents=True, exist_ok=True)
        dst = INPUTS_DIR / name
        # 传进来的已经是归一化后的 inputs/in-<hash>.png（例如拿上次作业的输入复用）：
        # copy2 到自身会抛 SameFileError，这里直接跳过。
        if p.resolve() != dst.resolve():
            shutil.copy2(p, dst)
        params[key] = name
    _trim_video(params, video_keys=video_keys)
    return params


def _trim_video(params: dict, video_keys: set[str] | None = None,
                fps: int = 16, timeout: int = 300) -> None:
    """按 trim_seconds 截取驱动视频（可选重采样到 16fps）。

    为什么需要：动作迁移模板会**按驱动视频的帧数循环**（81 帧一轮），
    一条 10.8s / 60fps 的手机视频 = 563 帧 = 7 轮，就是 20+ 分钟、¥0.9。
    截到 4 秒并重采样 16fps ≈ 64 帧，一轮就完，约 3 分钟 ¥0.2。
    """
    import subprocess

    try:
        secs = float(params.get("trim_seconds") or 0)
    except (TypeError, ValueError):
        return
    if secs <= 0:
        return
    keys = video_keys if video_keys is not None else {
        k for k in params if isinstance(k, str) and re.fullmatch(r"video(?:_\d+)?", k)}
    for key in [k for k in params if k in keys]:
        name = params.get(key)
        if not isinstance(name, str) or not name:
            continue
        src = INPUTS_DIR / name
        if not src.is_file():
            continue
        out_name = f"{src.stem}-t{secs:g}-{fps}fps.mp4"
        out = INPUTS_DIR / out_name
        # 只认「非空且完整」的产物：ffmpeg 被 kill / 超时会留下 0 字节残片，
        # 旧实现用 out.is_file() 判定已生成 → 下次直接跳过重编码，把坏文件当成品
        # 上传进实例（2026-11-08 审计复现）。现在：残片先清掉，写临时名再原子改名。
        if out.is_file() and out.stat().st_size > 0:
            params[key] = out_name
            continue
        if out.is_file():
            out.unlink(missing_ok=True)
        tmp = out.with_suffix(".part.mp4")
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src),
               "-t", f"{secs:g}", "-r", str(fps),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-an", str(tmp)]
        try:
            subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)
            if not tmp.is_file() or tmp.stat().st_size <= 0:
                raise OSError("ffmpeg 产出为空")
            tmp.replace(out)
        except (OSError, subprocess.SubprocessError) as e:
            tmp.unlink(missing_ok=True)
            # 截取失败就保留原视频（不写 trim 后的名字），行为可预期：
            # 宁可多花时间跑完整素材，也不静默上传一个坏文件。
            print(f"  ⚠ 截取失败（{type(e).__name__}），改用原始素材 {name}")
            continue
        params[key] = out_name


def _make_spec(r: dict, *, job_key: str, task_ref: str | None) -> dict:
    wf, model = r["workflow"], r["model"]
    r["params"] = _normalize_image(dict(r["params"]), video_keys=_video_keys(wf))
    slots = (wf.get("model") or {}).get("slots") or {}
    files = model.get("files") or {}
    primary = model.get("primary") or next(iter(slots))
    checkpoint = (files.get(primary) or {}).get("name")
    registry_meta = {
        "workflow_key": wf["key"],
        "workflow_version": wf["version"],
        "model_key": model["key"],
        "model_version": model["version"],
        "compat": r["compat"]["status"],
        "compat_basis": r["compat"]["basis"],
        "setup_need": ((model.get("setup") or {}).get("profile") or "all"),
        "graph_sha256": wf.get("graph_sha256"),
    }
    limits = wf.get("limits") or {}
    from tools.p1 import oom_escalate as _oom
    oom_policy = _oom.normalize_policy((wf.get("oom_policy")
                                        or (model.get("oom_policy"))))
    return {
        "kind": wf["kind"],
        "task_ref": task_ref or f"P2.0-{wf['key']}",
        "model": {
            "key": model["key"],
            "workflow": wf.get("graph") or wf.get("source_path"),
            "checkpoint": checkpoint,
            "workflow_sha256": wf.get("graph_sha256"),
            "registry": registry_meta,
        },
        "params": r["params"],
        "expect": render.expect_for(wf, r["params"]),
        "budget": {"max_minutes": limits.get("max_minutes", 30),
                   "max_cost": limits.get("max_cost", 1.0)},
        "instance": {"required_gpu": wf.get("required_gpu") or ["4090"]},
        # OOM 策略：工作流卡可声明默认档，提交时 UI/CLI 可覆盖
        "oom_policy": oom_policy,
    }


def _pick_job_id(out: str) -> str:
    """jobctl 会先打印"同参数作业"的历史 id，最后一个才是本次新建的。"""
    ids = JOB_ID_RE.findall(out or "")
    return ids[-1] if ids else ""


def cmd_new(a) -> int:
    params = _load_params(a.params, a.set)
    schema = {}
    try:
        wf0 = registry.get_workflow(a.workflow, db_path=registry.DB_PATH)
        schema = wf0.get("params") or {}
    except (registry.RegistryError, cs.ConfigError) as e:
        _die(str(e))
    if "seed" in schema and "seed" not in params:
        import random
        params["seed"] = random.SystemRandom().randint(1, 2**31 - 1)
        print(f"  seed 未指定，随机生成 {params['seed']}（已写进 spec，可复现）")
    try:
        r = registry.resolve(a.workflow, model_key=a.model, params=params,
                             allow_experimental=a.allow_experimental, db_path=registry.DB_PATH)
    except (registry.RegistryError, cs.ConfigError) as e:
        _die(str(e))
    spec = _make_spec(r, job_key=a.workflow, task_ref=a.task_ref)
    ts = time.strftime("%Y%m%dT%H%M%S")
    spec_path = Path(a.spec_out) if a.spec_out else SPEC_DIR / f"{a.workflow}-{ts}.json"
    if not spec_path.is_absolute():
        spec_path = ROOT / spec_path
    _atomic_write_json(spec_path, spec)
    _ok(f"spec 已生成：{spec_path.relative_to(ROOT)}")
    print(f"  工作流 {r['workflow']['key']} v{r['workflow']['version']} × "
          f"模型 {r['model']['key']} · 兼容性 {r['compat']['status']}")
    print(f"  参数   {json.dumps(r['params'], ensure_ascii=False)[:200]}")
    print(f"  预算   ¥{spec['budget']['max_cost']} / {spec['budget']['max_minutes']}min · "
          f"卡型 {spec['instance']['required_gpu']}")

    cmd = [sys.executable, str(JOBCTL), "new", "--spec", str(spec_path)]
    if getattr(a, "force", False):
        cmd.append("--force")          # 显式重跑：跳过去重
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    out = (p.stdout or "") + (p.stderr or "")
    print(out.strip())
    if p.returncode != 0:
        _die(f"jobctl new 失败（rc={p.returncode}）")
    jid = _pick_job_id(out)
    if not jid:
        _die("未从 jobctl 输出解析到 job_id")
    # 复用进行中的同参数作业：不再 spawn 第二个 run.py
    from tools.p1 import store as p1store
    if p1store.DEDUP_REUSE_MARK in out:
        print(f"  同参数作业已在跑，复用 {jid}（不重复建机、不重复执行）")
        return 0
    if not a.run:
        print(f"\n下一步：python3 tools/p1/run.py {jid} --gpu {spec['instance']['required_gpu'][0]}")
        return 0
    cmd = [sys.executable, str(RUNNER), jid, "--gpu", a.gpu or spec["instance"]["required_gpu"][0],
           "--hours", str(a.hours)]
    if a.dry_run:
        cmd.append("--dry-run")
    print(f"\n▶ 执行：{' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT))


def cmd_compat(a) -> int:
    if a.action == "list":
        rows = registry.compat_matrix(registry.DB_PATH)
        if a.json:
            _dump(rows)
            return 0
        print(f"{'workflow':<28} {'model':<14} {'status':<12} basis / evidence")
        print("-" * 100)
        for r in rows:
            ev = ",".join(r.get("evidence") or []) or "—"
            print(f"{r['workflow']:<28} {r['model']:<14} {r['status']:<12} "
                  f"{r.get('basis') or '—'} / {ev}")
        return 0
    if a.action == "set":
        if a.status not in ("verified", "incompatible"):
            _die("--status 只允许 verified / incompatible")
        if a.status == "verified" and not a.evidence:
            _die("verified 必须给 --evidence（真实成功作业 ID）")
        r = registry.set_compat(a.workflow, a.model, a.status,
                                evidence=a.evidence or [], notes=a.notes or "",
                                db_path=registry.DB_PATH)
        _ok(f"兼容性已写入：{a.workflow} × {a.model} → {r['status']}")
        return 0
    # verify：证据必须是 DONE + manifest PASS + registry 元数据对得上
    jid = (a.evidence or [None])[0]
    if not jid:
        _die("compat verify 需要 --evidence <job_id>")
    jf = ROOT / "reports" / "p1" / "jobs" / jid / "job.json"
    if not jf.exists():
        _die(f"作业不存在：{jid}")
    job = cs.load_json(jf)
    man_path = jf.parent / "manifest.json"
    man = cs.load_json(man_path) if man_path.exists() else {}
    if job.get("state") != "DONE" or man.get("verdict") != "PASS":
        _die(f"证据作业未成功：state={job.get('state')} verdict={man.get('verdict')}")
    reg = ((job.get("model") or {}).get("registry") or {})
    if reg.get("workflow_key") != a.workflow or reg.get("model_key") != a.model:
        _die(f"证据作业的 registry 元数据不匹配：{reg.get('workflow_key')} × {reg.get('model_key')}")
    arts = [x for x in (man.get("artifacts") or []) if (jf.parent / x.get("rel", "")).exists()]
    if not arts:
        _die("证据作业没有可读产物")
    prev = registry.compat_for(a.workflow, a.model, registry.DB_PATH)
    evidence = list(dict.fromkeys(list(prev.get("evidence") or []) + [jid]))
    r = registry.set_compat(a.workflow, a.model, "verified", evidence=evidence,
                            notes=a.notes or f"p20ctl compat verify（{jid}）",
                            db_path=registry.DB_PATH)
    _ok(f"已验证：{a.workflow} × {a.model}（证据 {evidence}）")
    return 0


def cmd_stats(a) -> int:
    _dump(registry.stats(registry.DB_PATH))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p20ctl", description="P2.0 Registry CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="建库/重建索引")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("import", help="从 registry/*.json 导入")
    sp.add_argument("--check", action="store_true", help="只检查是否最新（不写库）")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("validate", help="全量体检")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("list", help="列表")
    sp.add_argument("what", choices=("workflows", "models"))
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="详情")
    sp.add_argument("what", choices=("workflow", "model"))
    sp.add_argument("key")
    sp.add_argument("--version", type=int)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("resolve", help="解析参数 + 预览渲染")
    sp.add_argument("--workflow", required=True)
    sp.add_argument("--model")
    sp.add_argument("--params")
    sp.add_argument("--set", action="append", default=[])
    sp.add_argument("--prefix", default="preview")
    sp.add_argument("--allow-experimental", action="store_true")
    sp.add_argument("--show-graph", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_resolve)

    sp = sub.add_parser("new", help="生成 spec 并建作业（可选直接执行）")
    sp.add_argument("--workflow", required=True)
    sp.add_argument("--model")
    sp.add_argument("--params")
    sp.add_argument("--set", action="append", default=[])
    sp.add_argument("--task-ref")
    sp.add_argument("--spec-out")
    sp.add_argument("--allow-experimental", action="store_true")
    sp.add_argument("--run", action="store_true", help="建完作业立刻调 run.py 真跑")
    sp.add_argument("--dry-run", action="store_true", help="配合 --run：只走状态机不建机")
    sp.add_argument("--gpu")
    sp.add_argument("--hours", type=float, default=1.0)
    sp.add_argument("--force", action="store_true", help="同参数已完成也强制新建（显式重跑）")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("compat", help="兼容性矩阵")
    sp.add_argument("action", choices=("list", "set", "verify"))
    sp.add_argument("--workflow")
    sp.add_argument("--model")
    sp.add_argument("--status")
    sp.add_argument("--evidence", action="append", default=[])
    sp.add_argument("--notes")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_compat)

    sub.add_parser("stats", help="库统计").set_defaults(fn=cmd_stats)

    a = ap.parse_args(argv)
    if getattr(a, "action", None) in ("set", "verify"):
        if not a.workflow or not a.model:
            _die("compat set/verify 需要 --workflow 和 --model")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
