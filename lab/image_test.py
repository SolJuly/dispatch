#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一提示词、同一 seed，在同一实例上连续出图两次，记录每次耗时。"""
import json, os, subprocess, sys, time, urllib.request

API = "http://127.0.0.1:8188"
OUT = "/root/img_out"
PROMPT = ("电影感写实摄影，一只金毛寻回犬在开阔草地上全力奔跑，四肢腾空、耳朵向后飞扬，"
          "毛发在风中飘动。低角度侧面跟拍视角，背景草地与树林虚化拉出动态模糊，"
          "午后金色阳光逆光勾勒出狗狗轮廓光。浅景深，高速快门定格动作，35mm镜头质感，"
          "暖色调，画面充满速度感与生命力，8K高清细节。")
NEG = "blurry, low quality, deformed, watermark, text, extra limbs"
SEED = 20261026


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
            jget("/system_stats", 5); return True
        except Exception:
            time.sleep(3)
    return False


def graph_flux(seed, w, h, steps, tag=""):
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.sft"}},
        "clip": {"class_type": "DualCLIPLoader",
                 "inputs": {"clip_name1": "clip_l.safetensors",
                            "clip_name2": "t5xxl_fp16.safetensors", "type": "flux"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.sft"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["clip", 0]}},
        "gd": {"class_type": "FluxGuidance", "inputs": {"guidance": 3.5, "conditioning": ["pos", 0]}},
        "lat": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                          "scheduler": "simple", "denoise": 1.0, "model": ["unet", 0],
                          "positive": ["gd", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"dog_flux_{tag or seed}", "images": ["dec", 0]}},
    }


def graph_sdxl(seed, w, h, steps, tag=""):
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple",
                 "inputs": {"ckpt_name": "juggernautXL_juggernautX.safetensors"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["ckpt", 1]}},
        "lat": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": 5.0, "sampler_name": "dpmpp_2m",
                          "scheduler": "karras", "denoise": 1.0, "model": ["ckpt", 0],
                          "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": f"dog_sdxl_{tag or seed}", "images": ["dec", 0]}},
    }


def restart_comfy(wait=240):
    """镜像无 systemd：杀掉 main.py 后用 /start.d 脚本重新拉起，清掉 ComfyUI 的节点缓存。"""
    subprocess.run("pkill -f 'main.py --listen' || true", shell=True)
    time.sleep(2)
    subprocess.Popen("cd /root/ComfyUI && setsid nohup bash /start.d/comfyui.sh "
                     "> /root/ComfyUI/comfylog.txt 2>&1 < /dev/null &", shell=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            jget("/system_stats", 4)
            return time.time() - t0
        except Exception:
            time.sleep(2)
    raise RuntimeError("ComfyUI 重启失败")


def run(graph, timeout=600):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": graph, "client_id": "img"})["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}", 20)
        if pid in h:
            st = h[pid].get("status", {})
            files = [(o.get("subfolder", ""), f["filename"])
                     for o in h[pid].get("outputs", {}).values() for f in o.get("images", []) or []]
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 2),
                    "files": files}
        time.sleep(1)
    raise RuntimeError("超时")


def main():
    force = None
    if "--model" in sys.argv:
        force = sys.argv[sys.argv.index("--model") + 1]
    os.makedirs(OUT, exist_ok=True)
    res = {"status": "unknown", "runs": []}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        info = jget("/object_info", 30)
        res["comfyui"] = jget("/system_stats")["system"]["comfyui_version"]
        res["device"] = jget("/system_stats")["devices"][0]["name"]
        has_flux = "flux1-dev.sft" in (info["UNETLoader"]["input"]["required"]["unet_name"][0]
                                       if "UNETLoader" in info else [])
        use_flux = has_flux if force in (None, "auto") else (force == "flux")
        planner = graph_flux if use_flux else graph_sdxl
        res["model"] = "flux1-dev" if use_flux else "sdxl-juggernaut"
        w, h, steps = (1280, 720, 20) if has_flux else (1216, 704, 30)
        res["params"] = {"width": w, "height": h, "steps": steps, "seed": SEED}
        for i in (1, 2):
            fresh = "--fresh" in sys.argv
            if fresh:
                boot = restart_comfy()
                res.setdefault("comfy_boot_s", []).append(round(boot, 1))
            t_total = time.time()
            r = run(planner(SEED, w, h, steps, tag=f"r{i}_{SEED}"))
            r["run"] = i
            r["wall_s"] = round(time.time() - t_total, 2)
            if r["ok"] and r["files"]:
                sub, fn = r["files"][0]
                src = os.path.join("/root/ComfyUI/output", sub, fn)
                dst = os.path.join(OUT, f"dog_run{i}_{fn}")
                open(dst, "wb").write(open(src, "rb").read())
                r["saved"] = dst
            res["runs"].append(r)
        ok_all = all(r["ok"] for r in res["runs"]) and len(res["runs"]) == 2
        res["status"] = "ok" if ok_all else "partial"
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
