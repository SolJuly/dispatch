#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 校验层：把「产物好不好」从肉眼判断变成可复现的 PASS/FAIL。

6 条复验项（与 status.json / 架构图 H 区同名）：
  decode    ffprobe 能解析出有效视频流（图：PIL 能打开）
  geometry  宽高命中 expect
  frames    帧数/时长落在 expect 区间
  size      体积 ≥ expect.min_bytes
  black     黑帧占比 ≤ expect.black_ratio_max（YAVG<16 视为黑）
  params    从 workflow 回读的参数与 job.params 一致

规则：
  - 不适用项记 None（例如图片没有 frames/black），不参与 verdict
  - 占位符（{{SEED}} 等）跳过比对——那是运行时注入的
  - verdict = 所有适用项全 True 才 PASS
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

CHECK_ORDER = ("decode", "geometry", "frames", "size", "black", "params")

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


def content_warnings(ys: list[float], edge: float | None) -> list[str]:
    """三条启发式：帧差过低 / 亮度σ过低 / 边缘能量过低。

    只提示"这视频可能没用"，不改变 PASS/FAIL——最终仍需人眼判断。
    """
    w: list[str] = []
    if len(ys) > 1:
        diffs = [abs(a - b) for a, b in zip(ys, ys[1:])]
        win = WARN_WINDOW
        windows = [sum(diffs[i:i + win]) / len(diffs[i:i + win])
                   for i in range(0, len(diffs), win) if diffs[i:i + win]]
        win_max = max(windows) if windows else 0.0
        if win_max < WARN_FRAME_DIFF:
            w.append(f"最大窗口帧差 {win_max:.2f} < {WARN_FRAME_DIFF}：整段画面几乎静止"
                     f"（{len(windows)} 个窗口中最活跃的一段也只有 {win_max:.2f}）")
    if len(ys) > 1:
        mu = sum(ys) / len(ys)
        sigma = (sum((y - mu) ** 2 for y in ys) / len(ys)) ** 0.5
        if sigma < WARN_YAVG_STD:
            w.append(f"亮度σ {sigma:.2f} < {WARN_YAVG_STD}：曝光或内容异常")
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


def check_params(job: dict, workflow: dict | None) -> tuple[bool | None, dict]:
    """回读比对：workflow 里写死的参数，必须与 job.params 一致。"""
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

# 内容启发式阈值（只告警，不参与 PASS/FAIL）
# 帧差告警：用「滑动窗口内的最大帧差」而不是全局平均。
# 全局平均会把「前半段静止、后半段在动」的视频误判为静止
# （实测：H3 闭环成功样本全局 0.092，比 LTXV 噪点的 0.263 还低）。
# 实测分档（窗口=12 帧）：真静止 0.0025 / 微动作成功 0.19 / 噪点 0.38 / 正常运动 1.8+
WARN_FRAME_DIFF = 0.10   # 窗口最大帧差下限，低于此判为「整段几乎静止」
WARN_WINDOW = 12         # 窗口帧数（24fps 下约 0.5 秒）

# TODO(帧差指标的根本局限)：YAVG 差值只反映「平均亮度」的变化，不反映画面内容。
# 画面里有人走动但整体亮度稳定时，帧差会接近 0（H3 闭环样本就是 0.092）。
# 真正的静止检测应该用内容差（SSIM / freezedetect / 场景变化），而不是亮度差。
# 当前用窗口最大值 + 低阈值只能挡住「完全不动」，挡不住「亮度恒定但内容在动」。
# 待有预算时改用 ffmpeg freezedetect 计算冻结时长占比，见 reports/p1/P1-总结.md 技术债 #3。
WARN_YAVG_STD = 1.0      # 亮度标准差下限
                         # 原 1.5 会把「场景稳定、光线均匀」的正常视频误判为曝光异常
                         # （实测 H3 闭环成功样本 σ=1.36）。真静止视频 σ≈0，仍会被拦
WARN_EDGE = 1.0          # 拉普拉斯边缘能量下限（低于此=缺细节/抽象）


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
                kind: str = "video") -> dict:
    checks: dict = {}
    details: dict = {}

    pr = probe(path)
    checks["decode"] = bool(pr.get("codec"))
    details["decode"] = pr or {"error": "ffprobe 无法解析"}

    if kind == "image":
        # 图片：geometry / size / params 三项，frames/black 不适用
        checks["geometry"], details["geometry"] = _check_geometry(pr, expect)
        checks["size"], details["size"] = _check_size(path, expect)
        checks["params"], details["params"] = check_params(job, workflow)
        checks["frames"], checks["black"] = None, None
    else:
        checks["geometry"], details["geometry"] = _check_geometry(pr, expect)
        checks["frames"], details["frames"] = _check_frames(pr, expect)
        checks["size"], details["size"] = _check_size(path, expect)
        ys = yavg_series(path)
        checks["black"], details["black"] = _check_black(ys, expect)
        checks["params"], details["params"] = check_params(job, workflow)

    applicable = {k: v for k, v in checks.items() if v is not None}
    verdict = "PASS" if applicable and all(applicable.values()) else "FAIL"
    failed = [k for k, v in applicable.items() if not v]

    warnings: list[str] = []
    if kind != "image":
        ys = details.get("black", {}).get("_ys") or yavg_series(path)
        edge = edge_energy(path)
        warnings = content_warnings(ys, edge)
        details["content"] = {"edge_energy": edge, "samples": len(ys)}

    return {"file": path.name, "checks": checks, "verdict": verdict,
            "failed": failed, "warnings": warnings, "details": details}


def verify_job(job: dict, artifacts_dir: Path, workflow_path: Path | None = None) -> dict:
    """对一个 job 的 artifacts 目录做全量校验。"""
    expect = job.get("expect") or {}
    workflow = None
    if workflow_path and workflow_path.exists():
        try:
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            workflow = None

    files = sorted(p for p in artifacts_dir.glob("*")
                   if p.is_file() and p.suffix.lower() in (".mp4", ".png", ".jpg", ".jpeg", ".webp"))
    if not files:
        return {"verdict": "FAIL", "reason": "artifacts 目录为空", "files": []}

    kind = "image" if files[0].suffix.lower() != ".mp4" else "video"
    results = [verify_file(f, expect, job, workflow, kind) for f in files]
    all_pass = all(r["verdict"] == "PASS" for r in results)
    return {"verdict": "PASS" if all_pass else "FAIL", "kind": kind,
            "artifacts": results,
            "checks": results[0]["checks"] if len(results) == 1 else None}


TASK_CHECKS = {
    "decode": "ffprobe 解码 / 时长 / 帧数",
    "geometry": "尺寸匹配",
    "frames": "帧数 / 时长区间",
    "size": "产物体积下限",
    "black": "黑帧占比",
    "params": "params_hash 回读匹配",
}
