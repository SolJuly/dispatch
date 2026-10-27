#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一实例 / 同一模型 / 同一参数，连出两段 5s 视频（SVD-XT 链式 i2v + PyAV 编码）。
每段：4 次 SVD（各 25 帧），后续段去掉重复首帧 → 97 帧，按 19.4fps 编码 ≈ 5.0s。
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from fractions import Fraction

API = "http://127.0.0.1:8188"
OUT = "/root/vid_svd"
SEGS, SEG_FRAMES, SECONDS = 4, 25, 5.0
MOTION, STEPS = 127, 20
SHOTS = {"shot1": "start_shot1.png", "shot2": "start_shot2.png"}


def jget(p, t=20):
    return json.loads(urllib.request.urlopen(API + p, timeout=t).read())


def jpost(p, o, t=60):
    r = urllib.request.Request(API + p, data=json.dumps(o).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=t).read())


def wait_ready(limit=420):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            jget("/system_stats", 5); return True
        except Exception:
            time.sleep(2)
    return False


def svd_graph(image, seed, tag):
    return {
        "ckpt": {"class_type": "ImageOnlyCheckpointLoader",
                 "inputs": {"ckpt_name": "svd_xt.safetensors"}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "cond": {"class_type": "SVD_img2vid_Conditioning",
                 "inputs": {"width": 832, "height": 480, "video_frames": SEG_FRAMES,
                            "motion_bucket_id": MOTION, "fps": 8, "augmentation_level": 0.0,
                            "clip_vision": ["ckpt", 1], "init_image": ["img", 0], "vae": ["ckpt", 2]}},
        "guide": {"class_type": "VideoLinearCFGGuidance",
                  "inputs": {"model": ["ckpt", 0], "min_cfg": 1.0}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": STEPS, "cfg": 2.5, "sampler_name": "euler",
                          "scheduler": "karras", "denoise": 1.0, "model": ["guide", 0],
                          "positive": ["cond", 0], "negative": ["cond", 1],
                          "latent_image": ["cond", 2]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage", "inputs": {"filename_prefix": tag, "images": ["dec", 0]}},
    }


def run(g, timeout=1200):
    t0 = time.time()
    pid = jpost("/prompt", {"prompt": g, "client_id": "svd"})["prompt_id"]
    while time.time() - t0 < timeout:
        h = jget(f"/history/{pid}")
        if pid in h:
            st = h[pid].get("status", {})
            files = sorted((o.get("subfolder", ""), f["filename"])
                           for o in h[pid].get("outputs", {}).values()
                           for f in o.get("images", []) or [])
            return {"ok": st.get("status_str") == "success", "seconds": round(time.time() - t0, 1),
                    "files": files}
        time.sleep(1.5)
    raise RuntimeError("超时")


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
    res = {"status": "unknown", "runs": []}
    try:
        if not wait_ready():
            raise RuntimeError("ComfyUI 未就绪")
        for name, start_img in SHOTS.items():
            t0 = time.time()
            frames, cur, segs = [], start_img, []
            for i in range(SEGS):
                rr = run(svd_graph(cur, 3000 + i, f"svd_{name}_{i}"))
                if not rr["ok"] or not rr["files"]:
                    raise RuntimeError(f"{name} 第 {i+1} 段失败")
                keep = rr["files"] if i == 0 else rr["files"][1:]
                frames += [os.path.join("/root/ComfyUI/output", s, f) for s, f in keep]
                last_s, last_f = rr["files"][-1]
                cur = f"chain_{name}_{i}.png"
                shutil.copy(os.path.join("/root/ComfyUI/output", last_s, last_f),
                            os.path.join("/root/ComfyUI/input", cur))
                segs.append(rr["seconds"])
            fps = len(frames) / SECONDS
            dst = os.path.join(OUT, f"{name}.mp4")
            encode(frames, dst, fps)
            res["runs"].append({"shot": name, "frames": len(frames), "fps": round(fps, 2),
                                "segment_s": segs, "encode_total_s": round(time.time() - t0, 1),
                                "size_mb": round(os.path.getsize(dst) / 1048576, 2),
                                "saved": dst})
        res["status"] = "ok" if len(res["runs"]) == 2 else "failed"
        res["params"] = {"model": "svd_xt", "w": 832, "h": 480, "seg_frames": SEG_FRAMES,
                         "segs": SEGS, "steps": STEPS, "motion": MOTION, "target_seconds": SECONDS}
    except Exception as e:
        res.update({"status": "failed", "reason": f"{type(e).__name__}: {e}"})
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
