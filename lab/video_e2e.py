#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端视频测试：在实例内用 ComfyUI 生成约 10 秒视频（SVD 链式 i2v），并用 PyAV 编码 MP4。

步骤：
 1. SDXL 出首帧（1024×576）
 2. SVD 图生视频 3 段 × 25 帧，上一段末帧作为下一段首帧
 3. 合成 73 帧 → H.264 MP4（时长≈10s）
输出：/root/vid_out/video.mp4 + result.json
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
COMFY = "/root/ComfyUI"
OUT = "/root/vid_out"
PROMPT = ("a cinematic aerial shot over a misty pine forest at sunrise, camera slowly "
          "moving forward, warm light, volumetric fog, photorealistic")
STILL_PROMPT = ("a lonely wooden cabin beside a calm alpine lake at sunrise, pine trees, "
                "mist over water, cinematic, ultra detailed")
SEG_FRAMES = 25          # SVD 单段帧数
SEGS = 3                 # 段数 → 73 帧（首段 25，后续各 24 去重）
TARGET_SECONDS = 10.0


def jget(p, t=20):
    return json.loads(urllib.request.urlopen(API + p, timeout=t).read())


def jpost(p, obj, t=60):
    req = urllib.request.Request(API + p, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=t).read())


def wait_ready(limit=300):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            jget("/system_stats", 5)
            return True
        except Exception:
            time.sleep(3)
    return False


def run_prompt(graph, timeout=900):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": graph, "client_id": "vid"})["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}", 20)
        if pid in h:
            st = h[pid].get("status", {})
            files = []
            for o in h[pid].get("outputs", {}).values():
                for f in o.get("images", []) or []:
                    files.append((f.get("subfolder", ""), f["filename"]))
                for f in o.get("gifs", []) or []:
                    files.append((f.get("subfolder", ""), f["filename"]))
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 1),
                    "files": files, "status": st}
        time.sleep(1.5)
    raise RuntimeError("prompt 超时")


def still_graph():
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": "juggernautXL_juggernautX.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": STILL_PROMPT, "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality", "clip": ["ckpt", 1]}},
        "lat": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 576, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": 20261026, "steps": 20, "cfg": 5.0, "sampler_name": "dpmpp_2m",
                          "scheduler": "karras", "denoise": 1.0, "model": ["ckpt", 0],
                          "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": "vid_still", "images": ["dec", 0]}},
    }


def svd_graph(image_name: str, seed: int):
    return {
        "ckpt": {"class_type": "ImageOnlyCheckpointLoader",
                 "inputs": {"ckpt_name": "svd_xt.safetensors"}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "cond": {"class_type": "SVD_img2vid_Conditioning",
                 "inputs": {"width": 1024, "height": 576, "video_frames": SEG_FRAMES,
                            "motion_bucket_id": 127, "fps": 7, "augmentation_level": 0.0,
                            "clip_vision": ["ckpt", 1], "init_image": ["img", 0], "vae": ["ckpt", 2]}},
        "guide": {"class_type": "VideoLinearCFGGuidance",
                  "inputs": {"model": ["ckpt", 0], "min_cfg": 1.0}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": 20, "cfg": 2.5, "sampler_name": "euler",
                          "scheduler": "karras", "denoise": 1.0, "model": ["guide", 0],
                          "positive": ["cond", 0], "negative": ["cond", 1],
                          "latent_image": ["cond", 2]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"svd_seg_{seed}",
                                                       "images": ["dec", 0]}},
    }


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    res = {"status": "unknown", "steps": []}
    t0 = time.time()
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        res["comfy_ready_s"] = round(time.time() - t0, 1)
        stats = jget("/system_stats")
        res["comfyui"] = stats["system"]["comfyui_version"]
        res["device"] = stats["devices"][0]["name"]

        # 1) 首帧
        r = run_prompt(still_graph())
        res["steps"].append({"step": "still", **r, "files": r["files"][:1]})
        if not r["ok"] or not r["files"]:
            raise RuntimeError("首帧生成失败")
        sub, fn = r["files"][0]
        still_path = os.path.join(COMFY, "output", sub, fn)
        start_name = "vid_start.png"
        shutil.copy(still_path, os.path.join(COMFY, "input", start_name))
        res["start_image"] = start_name

        # 2) SVD 多段，末帧接下一段
        frames = []
        cur = start_name
        for i in range(SEGS):
            rr = run_prompt(svd_graph(cur, 1000 + i))
            res["steps"].append({"step": f"svd_{i}", "seconds": rr["seconds"],
                                 "ok": rr["ok"], "n": len(rr["files"])})
            if not rr["ok"] or not rr["files"]:
                raise RuntimeError(f"第 {i+1} 段失败")
            files = sorted(rr["files"])
            keep = files if i == 0 else files[1:]          # 后续段去掉与上一段重复的首帧
            for sub, fn in keep:
                frames.append(os.path.join(COMFY, "output", sub, fn))
            last_sub, last_fn = files[-1]
            cur = f"vid_chain_{i}.png"
            shutil.copy(os.path.join(COMFY, "output", last_sub, last_fn),
                        os.path.join(COMFY, "input", cur))
        res["frames"] = len(frames)

        # 3) PyAV 编码 H.264 MP4
        import av
        from PIL import Image
        fps = round(len(frames) / TARGET_SECONDS, 2)
        out_path = os.path.join(OUT, "video.mp4")
        first = Image.open(frames[0])
        W, H = first.size
        container = av.open(out_path, mode="w")
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = W, H
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "20", "preset": "medium"}
        for p in frames:
            for packet in stream.encode(av.VideoFrame.from_image(Image.open(p))):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        dur = len(frames) / fps
        res.update({"status": "ok", "video": out_path, "fps": fps,
                    "duration_s": round(dur, 2),
                    "size_mb": round(os.path.getsize(out_path) / 1048576, 2),
                    "width": W, "height": H})
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    res["total_s"] = round(time.time() - t0, 1)
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
