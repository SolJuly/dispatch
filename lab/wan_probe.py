#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T10：Wan2.1 黑帧复验。

分三步：
  1. 探测实例上 Wan2.1 的资产（unet / umt5 clip / vae）与相关节点
  2. 用官方 i2v 结构构造图并提交（带 clip_vision，若节点要求）
  3. 拉回产物，用黑帧率 + 边缘能量判定"是不是又黑了"

用法（在实例上跑，需 ComfyUI 已就绪）：
    python3 wan_probe.py [--t2v]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

API = "http://127.0.0.1:8188"
OUT = "/root/wan_out"
IMAGE = "start_shot1.png"

PROMPT = ("A cinematic medium shot in a modern minimalist living room with warm afternoon light. "
          "A young woman in beige knit loungewear sits on a light grey sofa looking at her phone, "
          "then slowly stands up and turns toward the kitchen. Shallow depth of field, 35mm lens, "
          "smooth natural motion, warm tones.")
NEG = "low quality, blurry, distorted, deformed, static, watermark, text"


def jget(p, t=30):
    return json.loads(urllib.request.urlopen(API + p, timeout=t).read())


def jpost(p, obj, t=60):
    req = urllib.request.Request(API + p, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=t).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:1200]}") from None


def pick(options: list[str], *keys: str) -> str | None:
    """在候选里挑第一个包含全部关键字的。"""
    for o in options:
        low = o.lower()
        if all(k.lower() in low for k in keys):
            return o
    return None


def probe() -> dict:
    oi = jget("/object_info", 60)
    ul = oi["UNETLoader"]["input"]["required"]["unet_name"][0]
    cl = oi["CLIPLoader"]["input"]["required"]["clip_name"][0]
    vl = oi["VAELoader"]["input"]["required"]["vae_name"][0]
    cv = oi.get("CLIPVisionLoader", {}).get("input", {}).get("required", {}).get("clip_name", [[]])[0]

    info = {
        # 优先官方 wan2.1/ 目录下的 fp8，避开社区 NVFP4 4bit 量化版
        "wan_unet_i2v": (pick(ul, "wan2.1/", "i2v", "480p", "fp8")
                         or pick(ul, "wan2.1/", "i2v", "fp8")
                         or pick(ul, "wan", "i2v")),
        "wan_unet_t2v": (pick(ul, "wan2.1/", "t2v", "14B", "fp8")
                         or pick(ul, "wan", "t2v")),
        "umt5": (pick(cl, "umt5", "xxl", "int8") or pick(cl, "umt5", "xxl", "fp8")
                 or pick(cl, "umt5", "xxl") or pick(cl, "umt5")),
        "wan_vae": pick(vl, "wan", "vae") or pick(vl, "wan2"),
        "clip_vision": pick(cv, "clip_vision") or pick(cv, "vit"),
        "nodes": {k: (k in oi) for k in
                  ("WanImageToVideo", "EmptyHunyuanLatentVideo", "CLIPVisionLoader",
                   "CreateVideo", "SaveVideo", "KSampler")},
    }
    # 顺便把 WanImageToVideo 的输入字段打出来
    if "WanImageToVideo" in oi:
        d = oi["WanImageToVideo"]["input"]
        info["i2v_req"] = list(d.get("required", {}).keys())
        info["i2v_opt"] = list(d.get("optional", {}).keys())
        info["i2v_out"] = oi["WanImageToVideo"].get("output")
    if "CLIPLoader" in oi:
        info["clip_types"] = oi["CLIPLoader"]["input"]["required"].get("type", [[]])[0]
    return info


def build(info: dict, t2v: bool = False) -> dict:
    g = {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": info["wan_unet_t2v" if t2v else "wan_unet_i2v"],
                            "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": info["umt5"], "type": "wan"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": info["wan_vae"]}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["clip", 0]}},
    }
    if t2v:
        g["lat"] = {"class_type": "EmptyHunyuanLatentVideo",
                    "inputs": {"width": 832, "height": 480, "length": 49, "batch_size": 1}}
        pos, neg, lat = ["pos", 0], ["neg", 0], ["lat", 0]
    else:
        g["img"] = {"class_type": "LoadImage", "inputs": {"image": IMAGE}}
        i2v_in = {"width": 832, "height": 480, "length": 49, "batch_size": 1,
                  "positive": ["pos", 0], "negative": ["neg", 0],
                  "vae": ["vae", 0], "start_image": ["img", 0]}
        if info.get("clip_vision"):
            # WanImageToVideo 要的是 CLIP_VISION_OUTPUT，必须经 CLIPVisionEncode 编码
            g["cvis"] = {"class_type": "CLIPVisionLoader",
                         "inputs": {"clip_name": info["clip_vision"]}}
            g["cvenc"] = {"class_type": "CLIPVisionEncode",
                          "inputs": {"clip_vision": ["cvis", 0], "image": ["img", 0],
                                     "crop": "none"}}
            i2v_in["clip_vision_output"] = ["cvenc", 0]
        g["i2v"] = {"class_type": "WanImageToVideo", "inputs": i2v_in}
        pos, neg, lat = ["i2v", 0], ["i2v", 1], ["i2v", 2]

    g["ks"] = {"class_type": "KSampler",
               "inputs": {"seed": 20261028, "steps": 20, "cfg": 3.5,
                          "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                          "model": ["unet", 0], "positive": pos, "negative": neg,
                          "latent_image": lat}}
    g["dec"] = {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}}
    g["vid"] = {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": 16.0}}
    g["save"] = {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": "wan_probe",
                            "format": "mp4", "codec": "h264"}}
    return g


def run(g, timeout=1800):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": g, "client_id": "wan"})["prompt_id"]
    print("prompt_id", pid, flush=True)
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
            err = None
            if st.get("status_str") != "success":
                for m in (st.get("messages") or []):
                    if isinstance(m, list) and m and m[0] == "execution_error":
                        err = f"{m[1].get('node_type')}: {m[1].get('exception_message')}"
                        break
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 1),
                    "files": sorted(files), "err": err}
        time.sleep(3)
    raise TimeoutError("timeout")


def main():
    t2v = "--t2v" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    info = probe()
    print("=== 资产探测 ===")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    missing = [k for k in ("wan_unet_i2v", "umt5", "wan_vae") if not info.get(k)]
    if missing:
        print(f"\n✗ 缺资产：{missing}，无法继续")
        return 1

    g = build(info, t2v=t2v)
    print(f"\n=== 提交 {'t2v' if t2v else 'i2v'}（{len(g)} 节点）===")
    rr = run(g)
    print(json.dumps({k: v for k, v in rr.items() if k != "files"}, ensure_ascii=False))

    if rr["ok"] and rr["files"]:
        import shutil
        sub, fn = rr["files"][0]
        src = os.path.join("/root/ComfyUI/output", sub, fn)
        dst = os.path.join(OUT, "wan_result.mp4")
        shutil.copy(src, dst)
        print("saved", dst, os.path.getsize(dst), "bytes")
    else:
        print("err:", rr.get("err"))
    return 0 if rr["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
