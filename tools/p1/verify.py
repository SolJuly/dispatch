#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1/P2 校验层：把「产物好不好」从肉眼判断变成可复现的 PASS/FAIL。

7 条复验项（与 status.json / 架构图 H 区同名）：
  decode    ffprobe 能解析出有效视频流（图：PIL 能打开）
  geometry  宽高命中 expect
  frames    帧数/时长落在 expect 区间
  size      体积 ≥ expect.min_bytes
  black     黑帧占比 ≤ expect.black_ratio_max（YAVG<16 视为黑）
  freeze    冻结时长占比门禁（freezedetect，D3 严格管控；expect.freeze 出现即参与 verdict）
  params    从 workflow 回读的参数与 job.params 一致

规则：
  - 不适用项记 None（例如图片没有 frames/black/freeze），不参与 verdict
  - 占位符（{{SEED}} 等）跳过比对——那是运行时注入的
  - verdict = 所有适用项全 True 才 PASS
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# freeze.py 在 tools/p2，与本包平级；保证任何入口都能 import 到
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from tools.p2 import freeze as freeze_mod  # noqa: E402

CHECK_ORDER = ("decode", "geometry", "frames", "size", "black", "freeze", "params")

# workflow 节点 → 参数字段
_PARAM_NODES = {
    "KSampler": ("seed", "steps", "cfg"),
    "MiniMaxH3ImageToVideo": ("width", "height", "length"),
    "MiniMaxH3ReferenceToVideo": ("width", "height", "length"),
    "EmptyLatentImage": ("width", "height"),
    "EmptySD3LatentImage": ("width", "height"),
    "EmptyLTXVLatentVideo": ("width", "height", "length"),
}


def _run(cmd, timeout=180):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _is_placeholder(v) -> bool:
    return isinstance(v, str) and "{{" in v


def probe(path: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=codec_name,width,height,nb_frames,duration,r_frame_rate",
              "-of", "json", str(path)])
    if not r or r.returncode != 0:
        return {}
    try:
        st = json.loads(r.stdout)["streams"][0]
    except Exception:
        return {}
    fps = 0.0
    fr = st.get("r_frame_rate", "0/1")
    try:
        n, d = fr.split("/")
        fps = round(float(n) / float(d), 3) if float(d) else 0.0
    except Exception:
        pass
    try:
        frames = int(st.get("nb_frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    return {"codec": st.get("codec_name"), "w": st.get("width"), "h": st.get("height"),
            "frames": frames, "fps": fps,
            "duration": round(float(st.get("duration") or 0), 3)}


def yavg_series(path: Path, limit: int = 400) -> list[float]:
    """逐帧亮度均值。limit 防止超大文件拖死。"""
    r = _run(["ffprobe", "-v", "error", "-f", "lavfi",
              "-i", f"movie={path},signalstats",
              "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
              "-of", "csv=p=0"], timeout=240)
    out = []
    if not r:
        return out
    for line in r.stdout.splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        try:
            out.append(float(line.split(",")[-1]))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def edge_energy(path: Path, frames: int = 3) -> float | None:
    """拉普拉斯后的平均亮度，反映画面边缘/细节强度。

    实测参照：正常视频（H3）≈2.5；抽象色块/糊片（LTXV 失败样本）≈0.5。
    """
    r = _run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", str(frames),
              "-vf", "convolution=0 1 0 1 -4 1 0 1 0,signalstats,metadata=print:file=-",
              "-f", "null", "-"], timeout=120)
    if not r:
        return None
    vals = [float(m.group(1)) for m in re.finditer(r"lavfi\.signalstats\.YAVG=([\d.]+)", r.stdout or "")]
    return round(sum(vals) / len(vals), 3) if vals else None


def content_warnings(edge: float | None, fz: dict | None, freeze_gated: bool = False) -> list[str]:
    """内容告警（只提示，不改变 PASS/FAIL——除非 freeze 已升级为门禁）。

    P2 起：不再用「亮度均值差 / 亮度σ」判静止（动作集中会误报，见 P2 计划 §3）。
    静止判定交给 freezedetect；这里只在「未走门禁」时补一句冻结提示，
    保留 edge_energy 抓噪点/抽象/糊片（LTXV 失败样本只有它抓得住）。
    """
    w: list[str] = []
    # 素材驱动类走 full_freeze_only：中度静止不判 FAIL，但仍要提示换素材
    stats = (fz or {}).get("stats") or fz or {}
    downgraded = bool((fz or {}).get("downgraded"))
    if (not freeze_gated or downgraded) and stats.get("ok"):
        ratio = stats.get("ratio")
        longest = stats.get("longest_s")
        if ratio is not None and ratio > WARN_FREEZE_RATIO:
            w.append(f"冻结时长占比 {ratio:.1%} > {WARN_FREEZE_RATIO:.0%}"
                     f"（最长 {longest}s），画面可能静止/卡帧")
    if edge is not None and edge < WARN_EDGE:
        w.append(f"边缘能量 {edge:.2f} < {WARN_EDGE}：缺少细节（画面模糊或抽象）")
    return w


def read_workflow_params(workflow: dict) -> dict:
    """从 workflow 图里回读关键参数（只取一次，先到先得）。"""
    out: dict = {}
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        ins = node.get("inputs") or {}
        for k in _PARAM_NODES.get(ct, ()):
            if k in ins and k not in out:
                out[k] = ins[k]
    return out


def check_params(job: dict, workflow: dict | None, wf_config: dict | None = None) -> tuple[bool | None, dict]:
    """回读比对：workflow 里写死的参数，必须与 job.params 一致。

    registry 作业传 wf_config（工作流配置）→ 用配置里的 bindings 回读，支持任意节点类；
    legacy 作业不传 → 走下面的 _PARAM_NODES 白名单，行为不变。
    """
    if wf_config and workflow:
        from tools.p20 import render as p20render
        return p20render.check_params(job, workflow, wf_config)
    if not workflow:
        return None, {"skipped": "workflow 缺失"}
    got = read_workflow_params(workflow)
    params = job.get("params") or {}
    diffs, compared, skipped = [], [], []
    for k, wf_val in got.items():
        if _is_placeholder(wf_val):
            skipped.append(k)
            continue
        # frames ← length
        job_val = params.get("frames") if k == "length" else params.get(k)
        if job_val is None:
            skipped.append(k)
            continue
        compared.append(k)
        try:
            same = float(wf_val) == float(job_val)
        except (TypeError, ValueError):
            same = wf_val == job_val
        if not same:
            diffs.append(f"{k}: workflow={wf_val} / job={job_val}")
    ok = None if not compared else (not diffs)
    return ok, {"compared": compared, "skipped": skipped, "diffs": diffs}


def _check_size(path: Path, expect: dict) -> tuple[bool | None, dict]:
    need = expect.get("min_bytes")
    if not isinstance(need, int) or need <= 0:
        return None, {"skipped": "expect.min_bytes 未设置"}
    size = path.stat().st_size
    return size >= need, {"bytes": size, "min_bytes": need}


def _check_geometry(pr: dict, expect: dict) -> tuple[bool | None, dict]:
    ws, hs = expect.get("width"), expect.get("height")
    if not ws and not hs:
        return None, {"skipped": "expect 未限定尺寸"}
    ok_w = (pr.get("w") in ws) if ws else True
    ok_h = (pr.get("h") in hs) if hs else True
    return bool(ok_w and ok_h), {"actual": [pr.get("w"), pr.get("h")],
                                 "expect": [ws, hs]}


def _check_frames(pr: dict, expect: dict) -> tuple[bool | None, dict]:
    rng = expect.get("duration_s")
    if not rng:
        return None, {"skipped": "expect.duration_s 未设置"}
    dur = pr.get("duration") or 0
    lo, hi = float(rng[0]), float(rng[1])
    return lo <= dur <= hi, {"duration": dur, "expect": [lo, hi], "frames": pr.get("frames")}


BLACK_YAVG = 24   # YUV limited range 黑电平是 16，留 8 级编码噪声余量

# 内容启发式阈值
# 冻结告警：只在「未走门禁」的 legacy 作业上提示（门禁阈值在 expect.freeze，见 freeze.py）。
# freezedetect 是内容级判据，替代 P1 的「亮度均值差 / 亮度σ」——那两条在动作集中的
# 视频上会误报（实测 testsrc2 持续运动：窗口帧差 0.067、σ 0.19，双双触发误报）。
WARN_FREEZE_RATIO = 0.20  # 冻结时长占比告警线（legacy 作业）
WARN_EDGE = 1.0           # 拉普拉斯边缘能量下限（低于此=缺细节/抽象；抓 LTXV 噪点样本）


def _check_black(ys: list[float], expect: dict) -> tuple[bool | None, dict]:
    cap = expect.get("black_ratio_max")
    if cap is None:
        return None, {"skipped": "expect.black_ratio_max 未设置"}
    if not ys:
        return False, {"error": "拿不到亮度序列，无法判定黑帧"}
    ratio = sum(1 for y in ys if y < BLACK_YAVG) / len(ys)
    return ratio <= float(cap), {"black_ratio": round(ratio, 4), "max": cap,
                                 "black_yavg_cut": BLACK_YAVG, "samples": len(ys),
                                 "yavg_mean": round(sum(ys) / len(ys), 2), "_ys": ys}


def verify_file(path: Path, expect: dict, job: dict, workflow: dict | None,
                kind: str = "video", wf_config: dict | None = None) -> dict:
    checks: dict = {}
    details: dict = {}

    pr = probe(path)
    checks["decode"] = bool(pr.get("codec"))
    details["decode"] = pr or {"error": "ffprobe 无法解析"}

    if kind == "image":
        # 图片：geometry / size / params 三项，frames/black/freeze 不适用
        checks["geometry"], details["geometry"] = _check_geometry(pr, expect)
        checks["size"], details["size"] = _check_size(path, expect)
        checks["params"], details["params"] = check_params(job, workflow, wf_config)
        checks["frames"], checks["black"], checks["freeze"] = None, None, None
    else:
        checks["geometry"], details["geometry"] = _check_geometry(pr, expect)
        checks["frames"], details["frames"] = _check_frames(pr, expect)
        checks["size"], details["size"] = _check_size(path, expect)
        ys = yavg_series(path)
        checks["black"], details["black"] = _check_black(ys, expect)
        checks["params"], details["params"] = check_params(job, workflow, wf_config)
        # 冻结门禁（D3 严格管控）：expect.freeze 出现即参与 PASS/FAIL
        fz = freeze_mod.freeze_stats(path)
        fz_expect = expect.get("freeze")
        if fz_expect:
            ok_fz, d_fz = freeze_mod.verdict(
                fz, fz_expect.get("ratio_max"), fz_expect.get("longest_max_s"),
                mode=fz_expect.get("mode"))
            checks["freeze"] = ok_fz
            d_fz["stats"] = fz
            details["freeze"] = d_fz
        else:
            checks["freeze"] = None
            details["freeze"] = fz

    applicable = {k: v for k, v in checks.items() if v is not None}
    verdict = "PASS" if applicable and all(applicable.values()) else "FAIL"
    failed = [k for k, v in applicable.items() if not v]

    warnings: list[str] = []
    if kind != "image":
        edge = edge_energy(path)
        fz = details.get("freeze")
        warnings = content_warnings(edge, fz, freeze_gated=bool(expect.get("freeze")))
        details["content"] = {"edge_energy": edge, "freeze": fz}

    return {"file": path.name, "checks": checks, "verdict": verdict,
            "failed": failed, "warnings": warnings, "details": details}


def verify_job(job: dict, artifacts_dir: Path, workflow_path: Path | None = None, *,
               rendered_graph: dict | None = None, wf_config: dict | None = None) -> dict:
    """对一个 job 的 artifacts 目录做全量校验。

    registry 作业：传 rendered_graph（实际提交的图）+ wf_config（绑定配置），
    这样参数复验不依赖 workflow 模板里的静态值；legacy 作业保持原路径。
    """
    expect = job.get("expect") or {}
    workflow = None
    if rendered_graph is not None:
        workflow = rendered_graph
    elif workflow_path and workflow_path.exists():
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            workflow = None

    files = sorted(p for p in artifacts_dir.glob("*")
                   if p.is_file() and p.suffix.lower() in (".mp4", ".png", ".jpg", ".jpeg", ".webp"))
    if not files:
        return {"verdict": "FAIL", "reason": "artifacts 目录为空", "files": []}

    # 多图作业（batch>1）：张数必须与 expect.images 一致，否则是 fetch 漏拉
    want = expect.get("images")
    if isinstance(want, int) and not isinstance(want, bool) and want != len(files):
        return {"verdict": "FAIL",
                "reason": f"产物张数不符：expect.images={want}，实际 {len(files)}",
                "files": [p.name for p in files], "expect_images": want}

    # 产物类型看工作流声明，不看「文件名第一个」：多路输出时封面 png 可能排前面，
    # 一旦按 png 判，冻结/黑帧/时长三项全部 skip，冻结视频也会 PASS。
    declared = str(((wf_config or {}).get("output") or {}).get("kind") or "")
    primary_idx = next((i for i, f in enumerate(files)
                        if f.name.startswith("00-primary-")), -1)
    if primary_idx < 0:
        # legacy / 老作业没有 00-primary- 前缀：至少让主产物命中期望扩展名，
        # 别被排在前面的封面 png 抢走（会在下面把 kind 判成 image）。
        want = str(expect.get("ext") or "").lower()
        primary_idx = next((i for i, f in enumerate(files)
                            if want and f.suffix.lower() == f".{want}"), 0)
    if declared in ("image", "video"):
        kind = declared
    else:
        kind = "image" if files[primary_idx].suffix.lower() != ".mp4" else "video"
    results = [verify_file(f, expect, job, workflow, kind, wf_config) for f in files]

    # 一份作业里可能有多路输出（动作迁移会额外存 pose 预览、拼接对比片，甚至单帧封面）。
    # 判定以**主产物**（fetch 时打了 00-primary- 前缀的 output 节点产物）为准；
    # 附加产物的 size/black/freeze 偏差只记告警，否则一张 1 帧封面就能把好作业判死。
    # 唯一例外：任何一路 decode 失败（文件坏了）仍然整体 FAIL。
    primary = results[primary_idx]
    broken = [r for r in results if r["checks"].get("decode") is False]
    secondary = [r for i, r in enumerate(results)
                 if i != primary_idx and r["verdict"] != "PASS"]
    verdict = "PASS" if primary["verdict"] == "PASS" and not broken else "FAIL"
    out = {"verdict": verdict, "kind": kind, "artifacts": results,
           "checks": primary["checks"] if len(results) == 1 else None,
           "primary": primary["file"]}
    if secondary:
        out["secondary_failures"] = [
            f"{r['file']}: {','.join(r['failed'])}" for r in secondary]
    return out


TASK_CHECKS = {
    "decode": "ffprobe 解码 / 时长 / 帧数",
    "geometry": "尺寸匹配",
    "frames": "帧数 / 时长区间",
    "size": "产物体积下限",
    "black": "黑帧占比",
    "freeze": "冻结时长占比门禁",
    "params": "params_hash 回读匹配",
}
