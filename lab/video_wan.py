#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一实例 / 同一模型 / 同一参数，连出两段 5s 视频（Wan2.1 i2v fp8, 81 帧 @16fps）。"""
import json, os, shutil, subprocess, sys, time, urllib.request

API = "http://127.0.0.1:8188"
OUT = "/root/vid_out3"
UNET = "Wan/Wan2.1_i2v_720p_scaled_fp8_e4m3_lightx2v.safetensors"
CLIP = "umt5_xxl_int8_convrot.safetensors"
VAE = "wan_2.1_vae.safetensors"
W, H, FRAMES, FPS, STEPS, CFG, SEED = 832, 480, 81, 16, 4, 1.0, 20261026

SHOTS = {
    "shot1": {
        "still": ("cinematic photo, modern minimalist living room, afternoon sunlight through "
                  "floor-to-ceiling window, a 30-year-old woman in beige knit loungewear sitting on "
                  "light grey fabric sofa looking at her phone, mid shot, shallow depth of field, "
                  "warm tone, 35mm lens"),
        "video": ("she puts down the phone, stands up slowly with hands on knees, turns toward the "
                  "kitchen on the right, camera tilts up slightly and pans right following her, "
                  "cinematic realism, warm afternoon light, shallow depth of field, 35mm"),
    },
    "shot2": {
        "still": ("cinematic photo, modern kitchen, a 30-year-old woman in beige knit loungewear "
                  "reaching the handle of a stainless steel double-door fridge, cold white light "
                  "spilling out, warm kitchen light, shallow depth of field, 50mm lens"),
        "video": ("she opens the fridge door, cold white light spills onto her face, bends down to "
                  "take a bottle of mineral water, stands up and closes the door, camera slowly pushes "
                  "in to a close-up of her hands and face, warm and cold light contrast, cinematic"),
    },
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


def restart_comfy(wait=300):
    subprocess.run("pkill -f 'main.py --listen' || true", shell=True)
    time.sleep(2)
    subprocess.Popen("cd /root/ComfyUI && setsid nohup bash /start.d/comfyui.sh "
                     "> /root/ComfyUI/comfylog.txt 2>&1 < /dev/null &", shell=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            jget("/system_stats", 4); return round(time.time() - t0, 1)
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI 重启失败")


def still_graph(prompt, seed):
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": "juggernautXL_juggernautX.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": "blurry, low quality, deformed", "clip": ["ckpt", 1]}},
        "lat": {"class_type": "EmptyLatentImage", "inputs": {"width": W, "height": H, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": 24, "cfg": 5.5, "sampler_name": "dpmpp_2m",
                          "scheduler": "karras", "denoise": 1.0, "model": ["ckpt", 0],
                          "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": f"still_{seed}", "images": ["dec", 0]}},
    }


VARIANTS = {
    "A": dict(steps=4, cfg=1.0, sampler="euler", scheduler="simple", shift=8.0),
    "B": dict(steps=20, cfg=5.0, sampler="uni_pc", scheduler="simple", shift=8.0),
}
VARIANT = "A"


def video_graph(prompt, start_image, seed):
    v = VARIANTS[VARIANT]
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "wan"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "img": {"class_type": "LoadImage", "inputs": {"image": start_image}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": "静态, 模糊, 低质量, 变形, 水印", "clip": ["clip", 0]}},
        "wv": {"class_type": "WanImageToVideo",
               "inputs": {"width": W, "height": H, "length": FRAMES, "batch_size": 1,
                          "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
                          "start_image": ["img", 0]}},
        "ms": {"class_type": "ModelSamplingSD3",
               "inputs": {"model": ["unet", 0], "shift": v["shift"]}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": v["steps"], "cfg": v["cfg"],
                          "sampler_name": v["sampler"], "scheduler": v["scheduler"], "denoise": 1.0,
                          "model": ["ms", 0],
                          "positive": ["wv", 0], "negative": ["wv", 1], "latent_image": ["wv", 2]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "vid": {"class_type": "CreateVideo", "inputs": {"images": ["dec", 0], "fps": float(FPS)}},
        "save": {"class_type": "SaveVideo",
                 "inputs": {"video": ["vid", 0], "filename_prefix": "wan_seg",
                            "format": "mp4", "codec": "h264"}},
    }


def run(g, timeout=2400):
    t0 = time.time()
    r = jpost("/prompt", {"prompt": g, "client_id": "wan"})
    if r.get("node_errors"):
        return {"ok": False, "error": json.dumps(r["node_errors"], ensure_ascii=False)[:800], "seconds": 0}
    pid = r["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}", 20)
        if pid in h:
            st = h[pid].get("status", {})
            files = []
            for o in h[pid].get("outputs", {}).values():
                for k in ("images", "gifs", "video", "videos"):
                    for f in o.get(k, []) or []:
                        files.append((f.get("subfolder", ""), f["filename"]))
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 2),
                    "files": files}
        time.sleep(2)
    raise RuntimeError("超时")


def main():
    os.makedirs(OUT, exist_ok=True)
    global VARIANT
    if "--variant" in sys.argv:
        VARIANT = sys.argv[sys.argv.index("--variant") + 1]
    res = {"status": "unknown", "variant": VARIANT, "runs": []}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        res["comfy_boot_s"] = restart_comfy()      # 让新 VAE 被扫描到
        st = jget("/system_stats")
        res.update({"device": st["devices"][0]["name"],
                    "params": {"w": W, "h": H, "frames": FRAMES, "fps": FPS, "steps": STEPS,
                               "cfg": CFG, "unet": UNET, "vae": VAE, "clip": CLIP, "seed": SEED}})
        only = None
        if "--shot" in sys.argv:
            only = sys.argv[sys.argv.index("--shot") + 1]
        for name, sp in SHOTS.items():
            if only and name != only:
                continue
            t_still = time.time()
            r1 = run(still_graph(sp["still"], SEED))
            r1["step"] = "still"
            r1["wall_s"] = round(time.time() - t_still, 2)
            if not r1["ok"] or not r1.get("files"):
                res["runs"].append(r1); raise RuntimeError(f"{name} 首帧失败: {r1.get('error','')[:200]}")
            sub, fn = r1["files"][0]
            start = f"start_{name}.png"
            shutil.copy(os.path.join("/root/ComfyUI/output", sub, fn),
                        os.path.join("/root/ComfyUI/input", start))
            res["runs"].append({"step": "still", "shot": name, "seconds": r1["seconds"],
                                "saved": start})
            t_v = time.time()
            r2 = run(video_graph(sp["video"], start, SEED))
            r2["step"], r2["shot"] = "video", name
            r2["wall_s"] = round(time.time() - t_v, 2)
            if r2["ok"] and r2.get("files"):
                sub2, fn2 = r2["files"][0]
                dst = os.path.join(OUT, f"{name}.mp4")
                shutil.copy(os.path.join("/root/ComfyUI/output", sub2, fn2), dst)
                r2["saved"] = dst
                r2["size_mb"] = round(os.path.getsize(dst) / 1048576, 2)
            res["runs"].append(r2)
            if not r2["ok"]:
                break
        res["status"] = "ok" if sum(1 for x in res["runs"] if x.get("step") == "video" and x.get("ok")) == 2 else "failed"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
