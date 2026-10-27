#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 GPU 实例内跑一次基准：给定 spec（模型/分辨率/步数…），调 ComfyUI API 出图/出视频。

被 lab_runner.py 上传到实例再执行：
    /root/miniconda3/envs/comfyui/bin/python bench_remote.py --spec spec.json --outdir /root/lab_out
输出：stdout 一段 JSON（结果），产物落在 outdir。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8188"


# ------------------------------------------------------------------ HTTP
def jget(path: str, timeout: float = 10):
    return json.loads(urllib.request.urlopen(API + path, timeout=timeout).read())


def jpost(path: str, obj, timeout: float = 120):
    req = urllib.request.Request(API + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def wait_ready(limit: float = 420.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            return time.time() - t0, jget("/system_stats", 5)
        except Exception:
            time.sleep(3)
    raise RuntimeError("ComfyUI 在 %ds 内未就绪" % limit)


# ------------------------------------------------------------------ 采样
class VramSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._halt = False
        self.peak = 0
        self.util_peak = 0
        self.n = 0

    def run(self):
        while not self._halt:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5).stdout.strip()
                m, u = [int(x) for x in out.split(",")[:2]]
                self.peak = max(self.peak, m)
                self.util_peak = max(self.util_peak, u)
                self.n += 1
            except Exception:
                pass
            time.sleep(1)

    def finish(self):
        self._halt = True
        self.join(timeout=5)
        return {"vram_peak_mib": self.peak, "util_peak_pct": self.util_peak, "samples": self.n}


# ------------------------------------------------------------------ 图构建
class Skip(Exception):
    pass


def build_sd(spec: dict, seed: int) -> dict:
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": spec["ckpt"]}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"text": spec["prompt"], "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": spec["negative"], "clip": ["ckpt", 1]}},
        "lat": {"class_type": spec.get("latent_node", "EmptyLatentImage"),
                "inputs": {"width": spec["width"], "height": spec["height"], "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": spec["steps"], "cfg": spec["cfg"],
                          "sampler_name": spec["sampler"], "scheduler": spec["scheduler"],
                          "denoise": 1.0, "model": ["ckpt", 0], "positive": ["pos", 0],
                          "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "save": {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": spec["prefix"], "images": ["dec", 0]}},
    }


def build_flux(spec: dict, seed: int) -> dict:
    clip_node = spec.get("clip_node", "DualCLIPLoader")
    if clip_node == "DualCLIPLoader":
        clip = {"class_type": "DualCLIPLoader",
                "inputs": {"clip_name1": spec["clip"], "clip_name2": spec["clip2"],
                           "type": "flux"}}
    else:
        clip = {"class_type": "CLIPLoader",
                "inputs": {"clip_name": spec["clip"], "type": "flux"}}
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": spec["unet"], "weight_dtype": "default"}},
        "clip": clip,
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": spec["vae"]}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"text": spec["prompt"], "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": "", "clip": ["clip", 0]}},
        "gd": {"class_type": "FluxGuidance",
               "inputs": {"guidance": spec.get("guidance", 3.5), "conditioning": ["pos", 0]}},
        "lat": {"class_type": "EmptySD3LatentImage",
                "inputs": {"width": spec["width"], "height": spec["height"], "batch_size": 1}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": spec["steps"], "cfg": spec.get("cfg", 1.0),
                          "sampler_name": spec["sampler"], "scheduler": spec["scheduler"],
                          "denoise": 1.0, "model": ["unet", 0], "positive": ["gd", 0],
                          "negative": ["neg", 0], "latent_image": ["lat", 0]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": spec["prefix"], "images": ["dec", 0]}},
    }


def build_wan_i2v(spec: dict, seed: int) -> dict:
    """Wan 图生视频：需要 WanImageToVideo 节点 + wan unet/clip/vae + 起始图。"""
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": spec["unet"], "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": spec["clip"], "type": spec.get("clip_type", "wan")}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": spec["vae"]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": spec["start_image"]}},
        "pos": {"class_type": "CLIPTextEncode",
                "inputs": {"text": spec["prompt"], "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode",
                "inputs": {"text": spec["negative"], "clip": ["clip", 0]}},
        "v": {"class_type": "WanImageToVideo",
              "inputs": {"width": spec["width"], "height": spec["height"],
                         "length": spec.get("frames", 33), "batch_size": 1,
                         "positive": ["pos", 0], "negative": ["neg", 0], "vae": ["vae", 0],
                         "start_image": ["img", 0]}},
        "ks": {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": spec["steps"], "cfg": spec["cfg"],
                          "sampler_name": spec["sampler"], "scheduler": spec["scheduler"],
                          "denoise": 1.0, "model": ["unet", 0],
                          "positive": ["v", 0], "negative": ["v", 1], "latent_image": ["v", 2]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveAnimatedWEBP",
                 "inputs": {"filename_prefix": spec["prefix"], "images": ["dec", 0],
                            "fps": spec.get("fps", 8), "lossless": False, "quality": 85,
                            "method": "default"}},
    }


BUILDERS = {"sd15": build_sd, "sdxl": build_sd, "flux": build_flux, "wan_i2v": build_wan_i2v}

REQUIRED_NODES = {
    "sd15": ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler",
             "VAEDecode", "SaveImage"],
    "sdxl": ["CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage", "KSampler",
             "VAEDecode", "SaveImage"],
    "flux": ["UNETLoader", "VAELoader", "EmptySD3LatentImage", "FluxGuidance", "KSampler",
             "VAEDecode", "SaveImage"],
    "wan_i2v": ["UNETLoader", "CLIPLoader", "VAELoader", "LoadImage", "WanImageToVideo",
                "KSampler", "VAEDecode"],
}


# ------------------------------------------------------------------ 产物
def png_size(path: str):
    try:
        with open(path, "rb") as f:
            head = f.read(33)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            w, h = struct.unpack(">II", head[16:24])
            return [w, h]
    except Exception:
        pass
    return None


def describe_artifact(path: str) -> dict:
    st = os.stat(path)
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:16]
    d = {"file": os.path.basename(path), "size": st.st_size, "sha256_16": sha}
    sz = png_size(path)
    if sz:
        d["width"], d["height"] = sz
    return d


def run_prompt(graph: dict, output_dir: str) -> dict:
    t0 = time.time()
    r = jpost("/prompt", {"prompt": graph, "client_id": "lab"})
    pid = r.get("prompt_id")
    if not pid:
        raise RuntimeError("提交失败: %s" % json.dumps(r)[:400])
    while True:
        h = jget("/history/%s" % pid, 10)
        if pid in h:
            item = h[pid]
            status = item.get("status", {})
            el = time.time() - t0
            outs = item.get("outputs", {})
            files = []
            for node_out in outs.values():
                for key in ("images", "gifs", "videos"):
                    for f in node_out.get(key, []) or []:
                        files.append(os.path.join(output_dir, f.get("subfolder", ""),
                                                  f["filename"]))
            return {"seconds": round(el, 2), "status": status.get("status_str"),
                    "completed": status.get("completed"), "files": files,
                    "error": status.get("messages") if status.get("status_str") != "success" else None}
        if time.time() - t0 > 900:
            raise RuntimeError("推理超时 >900s")
        time.sleep(1.5)


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--outdir", default="/root/lab_out")
    args = ap.parse_args()
    spec = json.load(open(args.spec, encoding="utf-8"))
    os.makedirs(args.outdir, exist_ok=True)

    res = {"cell": spec.get("cell_id"), "model": spec.get("model"),
           "config": spec.get("config_name"), "status": "unknown"}
    try:
        t_ready, stats = wait_ready()
        info = jget("/object_info", 30)
        res["comfyui_version"] = stats.get("system", {}).get("comfyui_version")
        res["torch"] = stats.get("system", {}).get("pytorch_version")
        dev = (stats.get("devices") or [{}])[0]
        res["device"] = dev.get("name")
        res["vram_total_mib"] = round((dev.get("vram_total") or 0) / 1048576)
        res["nodes"] = len(info)
        res["t_ready_s"] = round(t_ready, 1)

        need = REQUIRED_NODES.get(spec["model"], [])
        missing = [n for n in need if n not in info]
        if missing:
            raise Skip("缺少节点: %s" % ",".join(missing))
        if spec["model"] == "flux" and "DualCLIPLoader" not in info and "CLIPLoader" not in info:
            raise Skip("缺少 CLIP loader 节点")
        if spec["model"] == "flux":
            spec["clip_node"] = "DualCLIPLoader" if "DualCLIPLoader" in info else "CLIPLoader"

        # 模型可用性
        def choices(node, field):
            try:
                c = info[node]["input"]["required"][field][0]
                return c if isinstance(c, list) else []
            except Exception:
                return []

        if spec["model"] in ("sd15", "sdxl"):
            avail = choices("CheckpointLoaderSimple", "ckpt_name")
            if spec["ckpt"] not in avail:
                pref = spec.get("ckpt_fallbacks") or (
                    ["juggernautXL_juggernautX.safetensors", "animagine-xl-3.0.safetensors",
                     "ghostmix_v12.safetensors", "darkSushiMixMix_225D.safetensors"])
                pick = next((x for x in pref if x in avail), None)
                if not pick:
                    # 再退一步：用名字里带 sdxl/juggernaut 的，或第一个
                    pick = next((a for a in avail if "xl" in a.lower()), avail[0] if avail else None)
                if not pick:
                    raise Skip("checkpoint 一个都看不到")
                res["ckpt_substituted"] = {"asked": spec["ckpt"], "used": pick}
                spec["ckpt"] = pick
        if spec["model"] == "flux":
            if spec["unet"] not in choices("UNETLoader", "unet_name"):
                raise Skip("unet 不可见: %s" % spec["unet"])
        if spec["model"] == "wan_i2v":
            if spec["unet"] not in choices("UNETLoader", "unet_name"):
                raise Skip("wan unet 不可见: %s" % spec["unet"])
            if spec.get("start_image") and not os.path.exists(os.path.join("/root/ComfyUI/input", spec["start_image"])):
                raise Skip("缺少起始图 input/%s" % spec["start_image"])

        graph = BUILDERS[spec["model"]](spec, spec.get("seed", 20261026))
        sampler = VramSampler()
        sampler.start()
        runs = []
        for i in range(int(spec.get("repeats", 2))):
            g = BUILDERS[spec["model"]](spec, spec.get("seed", 20261026) + i)
            out = run_prompt(g, spec.get("output_dir") or (os.path.join(spec.get("comfy_dir", "/root/ComfyUI"), "output")))
            out["run"] = i
            runs.append(out)
            if out["status"] != "success":
                break
        vram = sampler.finish()

        arts = []
        for run in runs:
            for f in run.get("files") or []:
                if os.path.exists(f):
                    dst = os.path.join(args.outdir, os.path.basename(f))
                    if os.path.abspath(f) != os.path.abspath(dst):
                        shutil.copy2(f, dst)
                    arts.append(describe_artifact(dst))
        res.update({"status": "ok", "runs": runs, "vram": vram, "artifacts": arts,
                    "t_cold_s": runs[0]["seconds"] if runs else None,
                    "t_warm_s": runs[1]["seconds"] if len(runs) > 1 else None,
                    "spec": {k: spec[k] for k in ("model", "config_name", "width", "height",
                                                  "steps", "cfg", "sampler", "scheduler") if k in spec}})
    except Skip as e:
        res.update({"status": "skipped", "reason": str(e)})
    except Exception as e:
        res.update({"status": "failed", "reason": "%s: %s" % (type(e).__name__, e)})
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
