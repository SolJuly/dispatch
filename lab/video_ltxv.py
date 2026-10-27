#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一实例、同一模型、同一参数，连续生成两段 5s 视频（LTXV 2.3 t2v），记录耗时。"""
import json, os, shutil, sys, time, urllib.request

API = "http://127.0.0.1:8188"
OUT = "/root/vid_out2"
UNET = "Ltx/ltx-2.3-22b-distilled-1.1_transformer_only-int4_convrot.safetensors"
CLIP = "t5xxl_fp16.safetensors"
VAE = "ltx-2.3-22b-distilled_video_vae.safetensors"
W, H, FRAMES, FPS, STEPS, CFG = 768, 512, 121, 24, 8, 3.0

PROMPTS = {
    "shot1": ("电影感写实风格，中景镜头。现代简约客厅，午后自然光从落地窗斜射入室。"
              "一位30岁左右女性穿米色针织家居服，坐在浅灰布艺沙发上低头看手机。"
              "她放下手机，双手撑膝站起，身体转向画面右侧厨房方向，镜头轻微上摇并向右平移跟拍，"
              "背景虚化，浅景深，暖色调，35mm镜头质感，动作流畅自然。"),
    "shot2": ("紧接上一镜头，中景转近景。女性从画面左侧走入厨房，右手伸向双开门不锈钢冰箱把手，"
              "拉开冰箱门，冷白光从冰箱内溢出照在她脸上。她俯身取出一瓶透明矿泉水，直起身关上门。"
              "镜头先固定拍摄开冰箱动作，取水时缓慢推近至手部与面部近景，最后定格在拧瓶盖的瞬间。"
              "厨房暖光与冰箱冷光对比，浅景深，50mm镜头质感，动作连贯真实。"),
}


def jget(p, t=20):
    return json.loads(urllib.request.urlopen(API + p, timeout=t).read())


def jpost(p, obj, t=60):
    req = urllib.request.Request(API + p, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=t).read())


def wait_ready(limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            jget("/system_stats", 5); return True
        except Exception:
            time.sleep(2)
    return False


def graph(prompt, tag):
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "ltxv"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": "blurry, low quality, static, deformed, watermark", "clip": ["clip", 0]}},
        "cond": {"class_type": "LTXVConditioning",
                 "inputs": {"positive": ["pos", 0], "negative": ["neg", 0], "frame_rate": float(FPS)}},
        "lat": {"class_type": "EmptyLTXVLatentVideo",
                "inputs": {"width": W, "height": H, "length": FRAMES, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": 20261026, "steps": STEPS, "cfg": CFG,
                          "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                          "model": ["unet", 0], "positive": ["cond", 0],
                          "negative": ["cond", 1], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": float(FPS)}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": tag,
                            "format": "mp4", "codec": "h264"}},
    }


def run(g, timeout=2400):
    t0 = time.time()
    r = jpost("/prompt", {"prompt": g, "client_id": "vid"})
    if r.get("node_errors"):
        return {"ok": False, "error": r.get("node_errors"), "seconds": 0}
    pid = r["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}", 20)
        if pid in h:
            st = h[pid].get("status", {})
            outs = h[pid].get("outputs", {})
            files = []
            for o in outs.values():
                for k in ("images", "gifs", "video", "videos"):
                    for f in o.get(k, []) or []:
                        files.append((f.get("subfolder", ""), f["filename"]))
            return {"ok": st.get("status_str") == "success",
                    "seconds": round(time.time() - t0, 2), "files": files,
                    "status": st.get("status_str"), "messages": st.get("messages")[-1:] if st.get("messages") else []}
        time.sleep(2)
    raise RuntimeError("超时")


def main():
    os.makedirs(OUT, exist_ok=True)
    res = {"status": "unknown", "runs": []}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        st = jget("/system_stats")
        res.update({"device": st["devices"][0]["name"], "comfyui": st["system"]["comfyui_version"],
                    "params": {"w": W, "h": H, "frames": FRAMES, "fps": FPS, "steps": STEPS,
                               "cfg": CFG, "unet": UNET}})
        for name, prompt in PROMPTS.items():
            r = run(graph(prompt, f"ltxv_{name}"))
            r["shot"] = name
            if r["ok"] and r.get("files"):
                f = r["files"][0]
                src = os.path.join("/root/ComfyUI/output", f[0], f[1])
                dst = os.path.join(OUT, f"{name}.mp4")
                shutil.copy(src, dst)
                r["saved"] = dst
                r["size_mb"] = round(os.path.getsize(dst) / 1048576, 2)
            res["runs"].append(r)
            if not r["ok"]:
                break
        res["status"] = "ok" if all(x["ok"] for x in res["runs"]) and len(res["runs"]) == 2 else "failed"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
