#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频质量矩阵实验：验证「时间轴被压缩」是不是画质差的根因。

结论口径（每组都记录）：
  - 段数 / 帧数 / 编码 fps / 时长 / 文件大小
  - 推理耗时、端到端耗时

组别：
  A  SVD 4 段链式，97 帧，编码 8fps   → 12.1s（时间轴"正确"）
  B  SVD 2 段链式，48 帧，编码 8fps   → 6.0s
  C  SVD 单段 25 帧，编码 8fps        → 3.1s（无拼接基准）
  C2 SVD 单段，ComfyUI 直出 mp4 8fps  → 验证「能不能不本地合成」
  D  LTXV 2B，97 帧一次生成，直出 24fps → 4.04s（无拼接对照）

产物落 /root/matrix_out/<组>/，指标落 result.json。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from fractions import Fraction

API = "http://127.0.0.1:8188"
COMFY = "/root/ComfyUI"
OUT = "/root/matrix_out"
IMAGE = "start_shot1.png"
CKPT_SVD = "svd_xt.safetensors"
CKPT_LTXV = "ltx-2/ltx-video-2b-v0.9.5.safetensors"

SEG_FRAMES = 25
STEPS_SVD = 20
MOTION = 127

LTXV_W, LTXV_H, LTXV_FRAMES, LTXV_STEPS, LTXV_CFG = 768, 512, 97, 30, 3.0
PROMPT = ("电影感写实风格，中景镜头。现代简约客厅，午后自然光从落地窗斜射入室。"
          "一位30岁左右女性穿米色针织家居服，坐在浅灰布艺沙发上低头看手机。"
          "她放下手机，双手撑膝站起，身体转向画面右侧厨房方向。"
          "背景虚化，浅景深，暖色调，35mm镜头质感，动作流畅自然。")
NEG = "模糊, 变形, 低质量, 闪烁, 抖动, 文字, 水印"

GROUPS = [
    {"id": "A", "desc": "SVD 4段链式 97帧 @8fps", "segs": 4, "mode": "frames", "fps": 8},
    {"id": "B", "desc": "SVD 2段链式 48帧 @8fps", "segs": 2, "mode": "frames", "fps": 8},
    {"id": "C", "desc": "SVD 单段 25帧 @8fps", "segs": 1, "mode": "frames", "fps": 8},
    {"id": "C2", "desc": "SVD 单段 直出mp4 @8fps", "segs": 1, "mode": "video", "fps": 8},
    {"id": "D", "desc": "LTXV2B 97帧 直出 @24fps", "segs": 0, "mode": "ltxv", "fps": 24},
]


def jget(p, t=30):
    return json.loads(urllib.request.urlopen(API + p, timeout=t).read())


def jpost(p, obj, t=60):
    req = urllib.request.Request(API + p, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=t).read())


def wait_ready(limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            st = jget("/system_stats", 5)
            if st.get("system"):
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def svd_graph(image, seed, tag, mode="frames"):
    g = {
        "ckpt": {"class_type": "ImageOnlyCheckpointLoader",
                 "inputs": {"ckpt_name": CKPT_SVD}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "cond": {"class_type": "SVD_img2vid_Conditioning",
                 "inputs": {"width": 832, "height": 480, "video_frames": SEG_FRAMES,
                            "motion_bucket_id": MOTION, "fps": 8, "augmentation_level": 0.0,
                            "clip_vision": ["ckpt", 1], "init_image": ["img", 0], "vae": ["ckpt", 2]}},
        "guide": {"class_type": "VideoLinearCFGGuidance",
                  "inputs": {"model": ["ckpt", 0], "min_cfg": 1.0}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": STEPS_SVD, "cfg": 2.5, "sampler_name": "euler",
                          "scheduler": "karras", "denoise": 1.0, "model": ["guide", 0],
                          "positive": ["cond", 0], "negative": ["cond", 1],
                          "latent_image": ["cond", 2]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
    }
    if mode == "video":
        g["vid"] = {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": 8.0}}
        g["save"] = {"class_type": "SaveVideo",
                     "inputs": {"video": ["vid", 0], "filename_prefix": tag,
                                "format": "mp4", "codec": "h264"}}
    else:
        g["save"] = {"class_type": "SaveImage",
                     "inputs": {"filename_prefix": tag, "images": ["dec", 0]}}
    return g


def ltxv_graph(seed, tag):
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": CKPT_LTXV}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["ckpt", 1]}},
        "lat": {"class_type": "EmptyLTXVLatentVideo",
                "inputs": {"width": LTXV_W, "height": LTXV_H, "length": LTXV_FRAMES,
                           "batch_size": 1}},
        "cond": {"class_type": "LTXVConditioning",
                 "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": 24.0}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": LTXV_STEPS, "cfg": LTXV_CFG,
                          "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                          "model": ["ckpt", 0], "positive": ["cond", 0],
                          "negative": ["cond", 1], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": 24.0}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": tag,
                            "format": "mp4", "codec": "h264"}},
    }


def run(g, timeout=1800):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": g, "client_id": "matrix"})["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}")
        if pid in h:
            ent = h[pid]
            st = ent.get("status", {})
            outs = ent.get("outputs", {}) or {}
            files = []
            for o in outs.values():
                for k in ("images", "gifs", "videos"):
                    for f in (o.get(k) or []):
                        files.append((f.get("subfolder", ""), f.get("filename")))
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 1),
                    "files": sorted(files), "status": st.get("status_str"),
                    "err": (st.get("messages") or [])[-1:] if st.get("status_str") != "success" else None}
        time.sleep(2)
    raise TimeoutError(f"prompt {pid} 超时")


def encode(frames, out_path, fps):
    import av
    from PIL import Image
    first = Image.open(frames[0])
    w, h = first.size
    container = av.open(out_path, mode="w")
    st = container.add_stream("libx264", rate=Fraction(round(fps * 100), 100))
    st.width, st.height = w, h
    st.pix_fmt = "yuv420p"
    st.options = {"crf": "20", "preset": "medium"}
    for p in frames:
        for pkt in st.encode(av.VideoFrame.from_image(Image.open(p).convert("RGB"))):
            container.mux(pkt)
    for pkt in st.encode():
        container.mux(pkt)
    container.close()


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "groups": [], "status": "running"}
    t_all = time.time()
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")

        for grp in GROUPS:
            gid = grp["id"]
            gdir = os.path.join(OUT, gid)
            os.makedirs(gdir, exist_ok=True)
            rec = {"id": gid, "desc": grp["desc"], "fps": grp["fps"], "mode": grp["mode"]}
            t0 = time.time()
            try:
                if grp["mode"] == "ltxv":
                    rr = run(ltxv_graph(7000, f"ltxv_{gid}"))
                    rec["infer_s"] = rr["seconds"]
                    rec["ok"] = rr["ok"]
                    if rr["ok"] and rr["files"]:
                        src = os.path.join(COMFY, "output", rr["files"][0][0], rr["files"][0][1])
                        dst = os.path.join(gdir, f"{gid}.mp4")
                        shutil.copy(src, dst)
                        rec["mp4"] = dst
                        rec["native"] = "comfyui-SaveVideo"
                    else:
                        rec["err"] = str(rr.get("err"))[:300]
                elif grp["mode"] == "video":
                    # C2：ComfyUI 自己出 mp4（验证「能不能不本地合成」）
                    rr = run(svd_graph(IMAGE, 3000, f"svd_{gid}", "video"))
                    rec["infer_s"] = rr["seconds"]
                    rec["ok"] = bool(rr["ok"] and rr["files"])
                    rec["native"] = "comfyui-SaveVideo"
                    if rec["ok"]:
                        src = os.path.join(COMFY, "output", rr["files"][0][0], rr["files"][0][1])
                        dst = os.path.join(gdir, f"{gid}.mp4")
                        shutil.copy(src, dst)
                        rec["mp4"] = dst
                        rec["duration_expected_s"] = round(SEG_FRAMES / grp["fps"], 3)
                    else:
                        rec["err"] = str(rr.get("err"))[:300]
                else:
                    frames, cur, segs = [], IMAGE, []
                    for i in range(grp["segs"]):
                        rr = run(svd_graph(cur, 3000 + i, f"svd_{gid}_{i}", "frames"))
                        segs.append(rr["seconds"])
                        if not rr["ok"] or not rr["files"]:
                            raise RuntimeError(f"第 {i+1} 段失败: {rr.get('err')}")
                        keep = rr["files"] if i == 0 else rr["files"][1:]
                        frames += [os.path.join(COMFY, "output", s, f) for s, f in keep]
                        last_s, last_f = rr["files"][-1]
                        if i < grp["segs"] - 1:
                            nxt = f"chain_{gid}_{i}.png"
                            shutil.copy(os.path.join(COMFY, "output", last_s, last_f),
                                        os.path.join(COMFY, "input", nxt))
                            cur = nxt
                    rec["seg_s"] = segs
                    rec["infer_s"] = round(sum(segs), 1)
                    rec["frames"] = len(frames)
                    rec["ok"] = True
                    dst = os.path.join(gdir, f"{gid}.mp4")
                    encode(frames, dst, grp["fps"])
                    rec["mp4"] = dst
                    rec["native"] = "local-PyAV"
                    rec["duration_expected_s"] = round(len(frames) / grp["fps"], 3)
                if rec.get("mp4"):
                    rec["size_mb"] = round(os.path.getsize(rec["mp4"]) / 1048576, 3)
            except Exception as e:
                rec.update({"ok": False, "err": f"{type(e).__name__}: {e}"[:300]})
            rec["wall_s"] = round(time.time() - t0, 1)
            res["groups"].append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)

        res["status"] = "ok"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    res["total_s"] = round(time.time() - t_all, 1)
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False)[:800])


if __name__ == "__main__":
    main()
