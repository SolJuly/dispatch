#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 拼接工具：视频顺序拼接 + 图片网格拼版（ffmpeg，零第三方依赖）。

    python3 tools/p2/concat.py probe --inputs a.mp4 b.mp4
    python3 tools/p2/concat.py video --inputs a.mp4 b.mp4 c.mp4 --out long.mp4 [--mode auto]
    python3 tools/p2/concat.py image --inputs 1.png 2.png 3.png 4.png --out sheet.png --cols 2 --gap 8

视频策略（auto）：
  - 同 codec/宽高/fps/pix_fmt/时基且音轨一致 → concat demuxer + `-c copy`（无损、秒级）
  - 否则 → 一次重编码：scale+pad+fps 归一后 concat 滤镜（libx264 crf18）
  - 统一 `-an`（现有产物无音轨）；有音轨时显式降级重编码并在告警里说明
拼完必校验：总时长 ≈ Σ输入 ±1 帧（1/fps），不合格直接报错。

集成点（D5）：job.json 里的 `post` 数组，在 PERSISTED 之后、RELEASING 之前本地执行。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _run(cmd, timeout=600):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None if isinstance(e, (OSError,)) else subprocess.CompletedProcess(cmd, 124, "", str(e))


def _ffprobe(path: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,time_base",
              "-show_entries", "format=duration",
              "-of", "json", str(path)], timeout=60)
    if not r or r.returncode != 0:
        return {}
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}
    streams = d.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not v:
        return {"has_audio": has_audio}
    fps = 0.0
    try:
        n, den = (v.get("r_frame_rate") or "0/1").split("/")
        fps = round(float(n) / float(den), 3) if float(den) else 0.0
    except Exception:
        pass
    try:
        dur = round(float((d.get("format") or {}).get("duration") or 0), 3)
    except (TypeError, ValueError):
        dur = 0.0
    return {"codec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
            "fps": fps, "pix_fmt": v.get("pix_fmt"), "time_base": v.get("time_base"),
            "duration": dur, "has_audio": has_audio}


def probe(paths: list[Path]) -> dict:
    """逐个探测 + 兼容性判定。"""
    rows = []
    for p in paths:
        p = Path(p)
        rows.append({"file": p.name, "exists": p.exists(), "info": _ffprobe(p) if p.exists() else {}})
    bad = [r["file"] for r in rows if not r["exists"] or not r["info"].get("codec")]
    if bad:
        return {"compatible": False, "reason": f"无法解码或不存在：{bad}", "files": rows}
    keys = {(r["info"]["codec"], r["info"]["w"], r["info"]["h"], r["info"]["fps"],
             r["info"]["pix_fmt"], r["info"]["time_base"], r["info"]["has_audio"]) for r in rows}
    compatible = len(keys) == 1
    reason = "" if compatible else "参数不一致（codec/宽高/fps/pix_fmt/时基/音轨不同）"
    return {"compatible": compatible, "reason": reason, "files": rows,
            "total_duration": round(sum(r["info"]["duration"] for r in rows), 3)}


def _verify_output(out: Path, expect_duration: float, fps: float) -> tuple[bool, str]:
    info = _ffprobe(out)
    if not info.get("codec"):
        return False, "输出无法解码"
    tol = max(1.0 / max(fps, 1.0), 0.05) + 0.05
    if abs(info["duration"] - expect_duration) > tol:
        return False, f"时长不符：输出 {info['duration']}s / 期望 {expect_duration}s（容差 {tol:.3f}）"
    return True, ""


def concat_videos(paths: list[Path], out: Path, mode: str = "auto",
                  fps: float | None = None, size: str | None = None,
                  with_audio: bool = False) -> dict:
    """顺序拼接。返回 {ok, mode, reason, duration, warnings}。"""
    paths = [Path(p) for p in paths]
    pr = probe(paths)
    if not pr["compatible"] and any(not r["exists"] or not r["info"].get("codec") for r in pr["files"]):
        return {"ok": False, "reason": pr["reason"]}

    infos = [r["info"] for r in pr["files"]]
    target_fps = fps or (infos[0]["fps"] if infos else 24.0)
    if size:
        W, H = (int(x) for x in size.lower().split("x"))
    else:
        W, H = max(i.get("w") or 0 for i in infos), max(i.get("h") or 0 for i in infos)
    has_audio = any(i.get("has_audio") for i in infos)
    warnings: list[str] = []
    if has_audio and not with_audio:
        warnings.append("输入含音轨，按视频-only 拼接（音轨丢弃）")

    use_copy = mode == "copy" or (mode == "auto" and pr["compatible"] and not has_audio)
    out.parent.mkdir(parents=True, exist_ok=True)

    if use_copy:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{p.resolve()}'\n")
            lst = Path(f.name)
        try:
            r = _run(["ffmpeg", "-v", "error", "-fflags", "+genpts", "-f", "concat", "-safe", "0",
                      "-i", str(lst), "-c", "copy", "-an", "-movflags", "+faststart", str(out)], timeout=600)
        finally:
            lst.unlink(missing_ok=True)
        real_mode = "copy"
    else:
        if pr["compatible"] and mode == "auto" and has_audio:
            warnings.append("参数一致但含音轨 → 走重编码以统一处理")
        parts = []
        for i in range(len(paths)):
            parts.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                         f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                         f"fps={target_fps}[v{i}]")
        chain = "".join(f"[v{i}]" for i in range(len(paths)))
        fc = ";".join(parts) + f";{chain}concat=n={len(paths)}:v=1:a=0[outv]"
        cmd = ["ffmpeg", "-v", "error"]
        for p in paths:
            cmd += ["-i", str(p)]
        cmd += ["-filter_complex", fc, "-map", "[outv]", "-c:v", "libx264", "-crf", "18",
                "-preset", "medium", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(out)]
        r = _run(cmd, timeout=1200)
        real_mode = "encode"

    if r is None or r.returncode != 0:
        tail = (r.stderr or "").strip()[-400:] if r else "ffmpeg 不可用/超时"
        return {"ok": False, "reason": f"ffmpeg 失败：{tail}", "mode": real_mode}

    expect = pr["total_duration"]
    ok, why = _verify_output(out, expect, target_fps)
    if not ok:
        return {"ok": False, "reason": why, "mode": real_mode, "warnings": warnings}
    info = _ffprobe(out)
    return {"ok": True, "mode": real_mode, "duration": info.get("duration"),
            "expect_duration": expect, "size": [info.get("w"), info.get("h")],
            "fps": info.get("fps"), "warnings": warnings}


def concat_images(paths: list[Path], out: Path, cols: int = 2, gap: int = 8,
                  cell: str | None = None) -> dict:
    """图片网格拼版。输出尺寸 = cols*cellW+(cols-1)*gap × rows*cellH+(rows-1)*gap。"""
    paths = [Path(p) for p in paths]
    if not paths:
        return {"ok": False, "reason": "没有输入"}
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        return {"ok": False, "reason": f"不存在：{missing}"}

    if cell:
        cw, ch = (int(x) for x in cell.lower().split("x"))
    else:  # 用最大宽高作为单元格基准
        dims = []
        for p in paths:
            r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                      "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(p)], timeout=30)
            try:
                w, h = (int(x) for x in (r.stdout or "").strip().splitlines()[0].split("x")[:2])
                dims.append((w, h))
            except Exception:
                return {"ok": False, "reason": f"读不到尺寸：{p.name}"}
        cw = max(d[0] for d in dims)
        ch = max(d[1] for d in dims)

    cols = max(1, int(cols))
    rows = (len(paths) + cols - 1) // cols
    n_cells = rows * cols
    pad_cell = f"{cw + gap}x{ch + gap}"

    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    for _ in range(n_cells - len(paths)):
        inputs += ["-f", "lavfi", "-i", f"color=c=black:s={cw}x{ch}:d=1"]

    filt, labels = [], []
    for i in range(n_cells):
        filt.append(f"[{i}:v]scale={cw}:{ch}:force_original_aspect_ratio=decrease,"
                    f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
                    f"pad={cw+gap}:{ch+gap}:0:0:color=black[c{i}]")
        labels.append(f"[c{i}]")

    row_labels = []
    for r in range(rows):
        chunk = labels[r * cols:(r + 1) * cols]
        if cols == 1:
            row_labels.append(chunk[0])
            continue
        row_labels.append(f"[r{r}]")
        filt.append("".join(chunk) + f"hstack=inputs={cols}[r{r}]")

    if rows == 1:
        out_label = row_labels[0]
    else:
        filt.append("".join(row_labels) + f"vstack=inputs={rows}[stack]")
        out_label = "[stack]"

    # 裁掉最右/最下的多余 gap，使尺寸 = cols*cw+(cols-1)*gap
    final_w = cols * cw + (cols - 1) * gap
    final_h = rows * ch + (rows - 1) * gap
    filt.append(f"{out_label}crop={final_w}:{final_h}:0:0[final]")

    cmd = ["ffmpeg", "-v", "error", *inputs, "-filter_complex", ";".join(filt),
           "-map", "[final]", "-frames:v", "1", str(out)]
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _run(cmd, timeout=180)
    if r is None or r.returncode != 0:
        tail = (r.stderr or "").strip()[-400:] if r else "ffmpeg 不可用/超时"
        return {"ok": False, "reason": f"ffmpeg 失败：{tail}"}
    info = _ffprobe(out)
    return {"ok": True, "size": [info.get("w"), info.get("h")],
            "grid": [cols, rows], "expect_size": [final_w, final_h]}


# ------------------------------------------------------------------ post 集成
def run_posts(job: dict, artifacts_dir: Path) -> list[dict]:
    """执行 job.post 里的拼接步骤（本地、零 GPU），产物写入 artifacts/。"""
    results = []
    for op in (job.get("post") or []):
        kind = op.get("op")
        if kind != "concat":
            results.append({"op": kind, "ok": False, "reason": f"未知 post op：{kind}"})
            continue
        inputs = [artifacts_dir / Path(x).name for x in (op.get("inputs") or [])]
        out = artifacts_dir / (op.get("out") or "concat.mp4")
        is_img = all(p.suffix.lower() in IMAGE_EXT for p in inputs)
        res = (concat_images(inputs, out, cols=int(op.get("cols") or 2), gap=int(op.get("gap") or 8))
               if is_img else concat_videos(inputs, out, mode=op.get("mode") or "auto"))
        res["op"] = kind
        res["out"] = out.name
        res["inputs"] = [p.name for p in inputs]
        results.append(res)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="concat", description="P2 视频/图片拼接")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="兼容性探测")
    sp.add_argument("--inputs", nargs="+", required=True)
    sp.set_defaults(fn=lambda a: _print({"probe": probe([Path(x) for x in a.inputs])}))

    sp = sub.add_parser("video", help="视频拼接")
    sp.add_argument("--inputs", nargs="+", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--mode", default="auto", choices=["auto", "copy", "encode"])
    sp.add_argument("--fps", type=float)
    sp.add_argument("--size")
    sp.set_defaults(fn=lambda a: _print(concat_videos([Path(x) for x in a.inputs], Path(a.out),
                                                      mode=a.mode, fps=a.fps, size=a.size)))

    sp = sub.add_parser("image", help="图片网格")
    sp.add_argument("--inputs", nargs="+", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--cols", type=int, default=2)
    sp.add_argument("--gap", type=int, default=8)
    sp.add_argument("--cell")
    sp.set_defaults(fn=lambda a: _print(concat_images([Path(x) for x in a.inputs], Path(a.out),
                                                      cols=a.cols, gap=a.gap, cell=a.cell)))

    a = ap.parse_args(argv)
    return a.fn(a)


def _print(d: dict) -> int:
    print(json.dumps(d, ensure_ascii=False, indent=2))
    ok = d.get("probe", {}).get("compatible") if "probe" in d else d.get("ok")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
