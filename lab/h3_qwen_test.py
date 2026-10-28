#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MiniMax H3（本地）+ Qwen-Image（本地）连通性测试。

三组，同一段提示词：
  1. Qwen-Image 文生图（1024×1024）
  2. MiniMax H3 文生视频（3s = 72 帧 @24fps）
  3. MiniMax H3 图生视频（同 3s，首帧 start_shot1.png）

不拼接、不做长视频——拼接问题按用户要求留到后面。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
COMFY = "/root/ComfyUI"
OUT = "/root/h3qwen_out"
IMAGE = "start_shot1.png"

PROMPT = ("电影感写实风格，中景镜头。现代简约客厅，午后自然光从落地窗斜射入室。"
          "一位30岁左右女性穿米色针织家居服，坐在浅灰布艺沙发上低头看手机。"
          "她放下手机，双手撑膝站起，身体转向画面右侧厨房方向。"
          "背景虚化，浅景深，暖色调，35mm镜头质感，动作流畅自然。")

QWEN_UNET = "qwen/qwen_image_fp8_e4m3fn_scaled.safetensors"
QWEN_CLIP = "qwen_2.5_vl_fp16.safetensors"
QWEN_VAE = "qwen_image_vae.safetensors"

H3_UNET = "minimax/minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors"
H3_CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
H3_VAE = "minimax_h3_video_vae_int8_convrot-v2.safetensors"

W, H, FPS, SECONDS = 768, 512, 24, 3
FRAMES = FPS * SECONDS


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
            jget("/system_stats", 5)
            return True
        except Exception:
            time.sleep(3)
    return False


def run(g, timeout=1800, tag=""):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": g, "client_id": "h3qwen"})["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}")
        if pid in h:
            ent = h[pid]
            st = ent.get("status", {})
            files = []
            for o in (ent.get("outputs") or {}).values():
                for k in ("images", "videos", "gifs"):
                    for f in (o.get(k) or []):
                        files.append((f.get("subfolder", ""), f.get("filename")))
            ok = st.get("status_str") == "success"
            err = None
            if not ok:
                msgs = st.get("messages") or []
                for m in msgs:
                    if isinstance(m, list) and m and m[0] == "execution_error":
                        info = m[1]
                        err = f"{info.get('node_type')}: {info.get('exception_message')}"[:400]
            return {"ok": ok, "seconds": round(time.time() - t0, 1),
                    "files": sorted(files), "err": err}
        time.sleep(2)
    raise TimeoutError("timeout")


def qwen_graph(tag):
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": QWEN_UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": QWEN_CLIP, "type": "qwen_image"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": "低质量, 模糊, 变形", "clip": ["clip", 0]}},
        "lat": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": 20261027, "steps": 20, "cfg": 2.5, "sampler_name": "euler",
                          "scheduler": "simple", "denoise": 1.0, "model": ["unet", 0],
                          "positive": ["pos", 0], "negative": ["neg", 0],
                          "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": tag, "images": ["dec", 0]}},
    }


def h3_graph(tag, first_frame=None):
    g = {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": H3_UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "shift": {"class_type": "MiniMaxH3SigmaShift",
                  "inputs": {"model": ["unet", 0], "shift_video": 5.0, "shift_audio": 1.0}},
        "cond": {"class_type": "MiniMaxH3ImageToVideo",
                 "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": PROMPT,
                            "width": W, "height": H, "length": FRAMES}},
        "neg": {"class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"clip": ["clip", 0], "vae": ["vae", 0],
                           "prompt": "低质量, 模糊, 抖动", "width": W, "height": H, "length": FRAMES}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": 20261027, "steps": 4, "cfg": 1.0, "sampler_name": "euler",
                          "scheduler": "simple", "denoise": 1.0, "model": ["shift", 0],
                          "positive": ["cond", 0], "negative": ["neg", 0],
                          "latent_image": ["cond", 1]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": float(FPS)}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": tag,
                            "format": "mp4", "codec": "h264"}},
    }
    if first_frame:
        g["img"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        g["cond"]["inputs"]["first_frame"] = ["img", 0]
        g["neg"]["inputs"]["first_frame"] = ["img", 0]
    return g


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": [], "status": "running"}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")

        cases = [
            ("qwen_t2i", "Qwen-Image 文生图 1024x1024", lambda: qwen_graph("qwen_t2i")),
            ("h3_t2v", "H3 文生视频 3s", lambda: h3_graph("h3_t2v", None)),
            ("h3_i2v", "H3 图生视频 3s", lambda: h3_graph("h3_i2v", IMAGE)),
        ]
        for cid, desc, mk in cases:
            rec = {"id": cid, "desc": desc}
            try:
                rr = run(mk(), tag=cid)
                rec.update({"ok": rr["ok"], "seconds": rr["seconds"], "err": rr["err"]})
                if rr["ok"] and rr["files"]:
                    sub, fn = rr["files"][0]
                    src = os.path.join(COMFY, "output", sub, fn)
                    ext = os.path.splitext(fn)[1]
                    dst = os.path.join(OUT, f"{cid}{ext}")
                    shutil.copy(src, dst)
                    rec["file"] = dst
                    rec["bytes"] = os.path.getsize(dst)
            except Exception as e:
                rec.update({"ok": False, "err": f"{type(e).__name__}: {e}"[:300]})
            res["runs"].append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)

        res["status"] = "ok" if all(r.get("ok") for r in res["runs"]) else "partial"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "runs"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
