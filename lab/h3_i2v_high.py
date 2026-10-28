#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H3 后段崩坏归因：隔离「步数」与「帧数」两个变量。

3 组（其余参数全部固定）：
  A  DasiwaHybridV2_int8  73 帧  20 步   → 只换模型，看崩坏是否消失
  B  DasiwaHybridV2_int8 121 帧  20 步   → 再看帧数是否放大问题
  C  fastvideo 4step     121 帧   4 步   → 保持原模型只加帧数

判定看片子中后段是否畸变，并记录每段耗时。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request

API = "http://127.0.0.1:8188"
COMFY = "/root/ComfyUI"
OUT = "/root/i2vtest"

H3_CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
H3_VAE = "minimax_h3_video_vae_int8_convrot-v2.safetensors"
UNET_BASE = "minimax/DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors"
UNET_FAST = "minimax/minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors"

PROMPT = ("电影感写实风格，中景镜头。现代简约客厅，午后自然光从落地窗斜射入室。"
          "一位30岁左右女性穿米色针织家居服，坐在浅灰布艺沙发上低头看手机。"
          "她放下手机，双手撑膝站起，身体转向画面右侧厨房方向。"
          "背景虚化，浅景深，暖色调，35mm镜头质感，动作流畅自然。")
NEG = "低质量, 模糊, 抖动, 变形"

W, H, FPS = 768, 512, 24

GROUPS = [
    {"id": "I2V", "desc": "Dasiwa 20步 121帧 i2v(5s)", "unet": UNET_BASE, "steps": 20,
     "frames": 121, "first_frame": "start_shot1.png"},
    {"id": "T2V", "desc": "Dasiwa 20步 121帧 t2v(5s)", "unet": UNET_BASE, "steps": 20,
     "frames": 121, "first_frame": None},
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
            jget("/system_stats", 5)
            return True
        except Exception:
            time.sleep(3)
    return False


def graph(unet, steps, frames, tag, first_frame=None):
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": H3_CLIP, "type": "minimax"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE}},
        "shift": {"class_type": "MiniMaxH3SigmaShift",
                  "inputs": {"model": ["unet", 0], "shift_video": 5.0, "shift_audio": 1.0}},
        "cond": {"class_type": "MiniMaxH3ImageToVideo",
                 "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": PROMPT,
                            "width": W, "height": H, "length": frames}},
        "neg": {"class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": NEG,
                           "width": W, "height": H, "length": frames}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": 20261027, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
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


def run(g, timeout=2400):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": g, "client_id": "h3m"})["prompt_id"]
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
                        err = f"{m[1].get('node_type')}: {m[1].get('exception_message')}"[:300]
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 1),
                    "files": sorted(files), "err": err}
        time.sleep(3)
    raise TimeoutError("timeout")


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": [], "status": "running"}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        for g in GROUPS:
            rec = {"id": g["id"], "desc": g["desc"], "steps": g["steps"], "frames": g["frames"]}
            t0 = time.time()
            try:
                rr = run(graph(g["unet"], g["steps"], g["frames"], f"i2v_{g['id']}", g.get("first_frame")))
                rec.update({"ok": rr["ok"], "infer_s": rr["seconds"], "err": rr["err"]})
                if rr["ok"] and rr["files"]:
                    sub, fn = rr["files"][0]
                    dst = os.path.join(OUT, f"{g['id']}.mp4")
                    shutil.copy(os.path.join(COMFY, "output", sub, fn), dst)
                    rec["bytes"] = os.path.getsize(dst)
            except Exception as e:
                rec.update({"ok": False, "err": f"{type(e).__name__}: {e}"[:300]})
            rec["wall_s"] = round(time.time() - t0, 1)
            res["runs"].append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
        res["status"] = "ok"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print("done", res.get("status"))


if __name__ == "__main__":
    main()
