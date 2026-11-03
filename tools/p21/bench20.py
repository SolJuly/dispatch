#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.1 5.1 连测：一次起机串行跑 N 条，采集成功率/耗时/显存/成本 + seed 复现。

为什么不是跑 N 次作业：每次作业都要重新建机（~2 分钟 + ¥0.08 开机费），
20 次就是 40 分钟、¥1.6，且大部分时间花在装载模型上。这里复用一台实例、
模型只装一次，每条只付推理时间。

用法：
    python3 tools/p21/bench20.py --workflow qwen_image_t2i --model qwen_image --n 20
    python3 tools/p21/bench20.py --workflow sdxl_t2i --model sdxl_juggernaut --n 20 --no-seed-check
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from tools.p1 import run as p1run  # noqa: E402
from tools.p1 import verify as p1verify  # noqa: E402
from tools.p20 import comfy as p20comfy  # noqa: E402
from tools.p20 import registry as p20registry  # noqa: E402
from tools.p20 import render as p20render  # noqa: E402

BASE_IMAGE = "compshareImage-1x65ieja8ncj"
OUT_DIR = ROOT / "reports/p2/p21/evidence"
ART_ROOT = ROOT / "reports/p2/p21/bench"

CASES = [
    ("人像", "portrait of an old fisherman, weathered face, dramatic side light, 85mm", 768, 1024),
    ("人像", "young woman in hanfu, bamboo forest, soft rim light, cinematic", 768, 1024),
    ("人像", "street portrait, neon rain, shallow depth of field, 50mm", 768, 1024),
    ("人像", "ballet dancer mid-air, studio strobe, high detail fabric", 768, 1024),
    ("人像", "elderly craftsman hands, macro detail, warm tungsten light", 1024, 1024),
    ("场景", "misty mountain lake at dawn, pine forest, long exposure", 1024, 768),
    ("场景", "tokyo alley at night, wet asphalt reflections, film grain", 1024, 768),
    ("场景", "desert dunes, low sun, long shadows, minimal composition", 1024, 768),
    ("场景", "abandoned factory, god rays through broken roof, dust", 1024, 768),
    ("场景", "snow cabin window, warm interior, cold blue exterior", 1024, 768),
    ("文字排版", 'poster with the words "SILENT SPRING", bold grotesque type, off-white paper', 768, 1024),
    ("文字排版", 'vintage book cover, title "NORTH", serif type, muted palette', 768, 1024),
    ("文字排版", 'neon sign reading "OPEN 24H", rain, bokeh, night street', 1024, 768),
    ("风格化", "ukiyo-e style great wave, indigo and cream, woodblock texture", 1024, 768),
    ("风格化", "impasto oil painting, sunflower field, thick brush strokes", 1024, 1024),
    ("风格化", "isometric miniature city block, pastel, soft shadows", 1024, 1024),
    ("风格化", "pixel art forest shrine, 32 colors, dithering", 1024, 1024),
    ("风格化", "claymation astronaut, felt texture, studio lighting", 1024, 1024),
    ("边界", "very long prompt: " + "lush botanical garden with ferns, orchids, mossy stones, "
     "dappled sunlight, shallow depth of field, " * 18, 768, 1024),
    ("边界", "extreme aspect ratio test, lighthouse on cliff, wide panorama", 2048, 512),
]


def pixel_sha256(p: Path) -> str:
    """解码后的像素哈希：排除 PNG 内嵌 prompt/workflow 元数据带来的差异。"""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout:
            return hashlib.sha256(r.stdout).hexdigest()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Bench:
    def __init__(self, wf_key: str, model_key: str, n: int, gpu: str, image: str = ""):
        self.wf = p20registry.get_workflow(wf_key)
        self.model = p20registry.get_model(model_key)
        self.wf_key, self.model_key = wf_key, model_key
        self.n, self.gpu = n, gpu
        self.image = image
        self.img_name = ""
        self.ctx: dict = {}
        self.hourly = 0.0
        self.uid = ""
        self.vram_peak = 0
        self._stop = threading.Event()
        self.results: list[dict] = []
        self.started = time.time()

    # ---------- 一次性：起机 + setup + 显存采样
    def provision(self) -> None:
        sys.path.insert(0, str(ROOT))
        from compshare.cs import CS  # noqa: PLC0415

        cs = CS()
        price = cs.price(self.gpu, 1, 16, 64, "Postpay", 100, 0, image_id=BASE_IMAGE)
        self.hourly = float(price.get("total") or 0)
        print(f"[0] {self.gpu} ¥{self.hourly:.2f}/h · 余额 ¥{float(cs.balance()['AmountAvailable']):.2f}")
        if cs.stock(self.gpu) <= 0:
            raise SystemExit(f"{self.gpu} 无现货")
        self.uid = cs.create(image_id=BASE_IMAGE, gpu_type=self.gpu, gpu=1, cpu=16,
                             memory_gb=64, name="p21-bench20", charge_type="Postpay",
                             disk_gb=100, data_disk_gb=0, remark="p21 bench20")[0]
        print(f"[1] 实例 {self.uid} 创建，等待 running…")
        inst = cs.wait_state(self.uid, {"running"}, timeout=900)
        cs.stop_at(self.uid, int((dt.datetime.now().astimezone()
                                  + dt.timedelta(hours=1)).timestamp()))
        host, port, pwd = cs.public_ip(inst), "23", cs.password_of(inst)
        self.ctx = {"host": host, "port": port, "password": pwd, "uid": self.uid}
        for _ in range(30):
            rc, out = p1run._ssh_run(host, port, pwd, "echo ok", timeout=40)
            if rc == 0 and "ok" in out:
                break
            time.sleep(6)
        print(f"[2] ssh 就绪 {host}")
        p1run.run_setup(self.uid, "all", self.ctx)
        if self.image:
            from tools.p20 import p20ctl as _p20ctl
            self.img_name = _p20ctl._normalize_image({"image": self.image})["image"]
            local = ROOT / "reports/p2/p21/inputs" / self.img_name
            p1run._ssh_run(host, port, pwd, "mkdir -p /root/ComfyUI/input", timeout=60)
            if not p1run._scp_put(host, port, pwd, local, f"/root/ComfyUI/input/{self.img_name}"):
                raise SystemExit(f"输入图上传失败：{self.img_name}")
            print(f"[2.5] 输入图已上传 {self.img_name}")
        threading.Thread(target=self._vram_loop, daemon=True).start()

    def _vram_loop(self) -> None:
        while not self._stop.is_set():
            rc, out = p1run._ssh_run(self.ctx["host"], self.ctx["port"], self.ctx["password"],
                                     "nvidia-smi --query-gpu=memory.used --format=csv,noheader",
                                     timeout=30)
            if rc == 0:
                try:
                    mb = int(out.strip().splitlines()[0].split()[0])
                    self.vram_peak = max(self.vram_peak, mb)
                except (ValueError, IndexError):
                    pass
            self._stop.wait(2)

    # ---------- 单条
    def _job(self, params: dict, prefix: str) -> dict:
        return {
            "job_id": prefix, "kind": "image", "params": params,
            "model": {"key": self.model_key,
                      "workflow": self.wf["graph"],
                      "registry": {"workflow_key": self.wf_key, "workflow_version": self.wf["version"],
                                   "model_key": self.model_key,
                                   "model_version": self.model["version"]}},
            "expect": p20render.expect_for(self.wf, params),
        }

    def _submit(self, graph: dict) -> str:
        payload = json.dumps({"prompt": graph, "client_id": "p21bench"}, ensure_ascii=False)
        cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
               "import json,sys,urllib.request;"
               "p=json.loads(sys.argv[1]);"
               "r=urllib.request.Request('http://127.0.0.1:8188/prompt',data=json.dumps(p).encode(),"
               "headers={'Content-Type':'application/json'});"
               "print(urllib.request.urlopen(r,timeout=120).read().decode())\" '"
               + payload.replace("'", "'\"'\"'") + "'")
        rc, out = p1run._ssh_run(self.ctx["host"], self.ctx["port"], self.ctx["password"],
                                 cmd, timeout=180)
        pid = p20comfy.parse_prompt_id(out)
        if not pid:
            raise RuntimeError(f"提交失败：{out[-300:]}")
        return pid

    def _wait(self, pid: str, limit_s: int = 300) -> tuple[list[tuple[str, str]], float]:
        t0 = time.time()
        cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
               "import json,sys,urllib.request;"
               "h=json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/'+sys.argv[1],timeout=60));"
               "pid=sys.argv[1];"
               "print(json.dumps(h.get(pid) or {}))\" " + pid)
        while time.time() - t0 < limit_s:
            rc, out = p1run._ssh_run(self.ctx["host"], self.ctx["port"], self.ctx["password"],
                                     cmd, timeout=120)
            if rc == 0:
                ent = p20comfy.parse_history(out)
                if ent:
                    st = p20comfy.status_of(ent)
                    if st == "success":
                        return sorted(p20comfy.extract_files(ent)), time.time() - t0
                    if st == "error":
                        raise RuntimeError(f"推理失败：{p20comfy.extract_error(ent)}")
            time.sleep(2)
        raise RuntimeError(f"超时 {limit_s}s")

    def run_case(self, idx: int, cat: str, prompt: str, w: int, h: int,
                 seed: int | None = None, tag: str = "", prefix: str = "",
                 batch: int = 1, denoise: float | None = None) -> dict:
        seed = seed if seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
        params = {"prompt": prompt, "neg_prompt": "blurry, lowres, watermark, text artifacts",
                  "seed": seed, "width": w, "height": h, "steps": 20, "cfg": 2.5,
                  "sampler": "euler", "scheduler": "simple"}
        if "batch_size" in (self.wf.get("params") or {}):
            params["batch_size"] = batch
        if self.img_name:
            params["image"] = self.img_name
            params["denoise"] = denoise if denoise is not None else [0.35, 0.55, 0.75][(idx - 1) % 3]
        if self.wf_key == "sdxl_t2i":
            params.update({"steps": 25, "cfg": 6.0, "sampler": "dpmpp_2m", "scheduler": "karras"})
        # 按模型约束夹紧尺寸（如 SDXL 上限 1536），避免个别 case 把整轮打断
        cons = (self.model or {}).get("constraints") or {}
        for k, v in (("width", w), ("height", h)):
            c = cons.get(k) or {}
            mult = int(c.get("multiple") or 8)
            v = max(int(c.get("min") or 256), min(int(c.get("max") or 2048), v))
            if k == "width":
                w = v - v % mult
            else:
                h = v - v % mult
        params.update({"width": w, "height": h})

        prefix = prefix or f"bench{tag}-{idx:02d}"
        job = self._job(params, prefix)
        dest = ART_ROOT / self.wf_key / prefix
        if dest.exists():                 # 重跑同一条时先清空，避免旧文件干扰张数校验
            import shutil
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            graph, _meta = p20render.render_job(job)
        except Exception as e:  # noqa: BLE001
            rec = {"i": idx, "category": cat, "seed": seed, "size": [w, h],
                   "prompt": prompt[:80], "inference_s": 0, "total_s": 0, "files": 0,
                   "verdict": "FAIL", "error": f"{type(e).__name__}: {e}",
                   "sha256": [], "pixel_sha256": []}
            self.results.append(rec)
            print(f"  ✗ {idx:02d} {cat:<5}  参数错误 {e}", flush=True)
            return rec

        t0 = time.time()
        err = ""
        nfiles, inf_s, shas, pshas = 0, 0.0, [], []
        try:
            pid = self._submit(graph)
            files, inf_s = self._wait(pid)
            for _sub, name in files:
                remote = f"/root/ComfyUI/output/{name}"
                if not p1run._scp_get(self.ctx["host"], self.ctx["port"],
                                      self.ctx["password"], remote, dest / name):
                    raise RuntimeError(f"scp 失败：{name}")
            pngs = sorted(dest.glob("*.png"))
            nfiles = len(pngs)
            shas = [sha256(p) for p in pngs]
            pshas = [pixel_sha256(p) for p in pngs]
            verdict = p1verify.verify_job(job, dest, rendered_graph=graph,
                                          wf_config=self.wf)["verdict"]
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            verdict = "FAIL"
        total = time.time() - t0
        rec = {"i": idx, "category": cat, "seed": seed, "size": [w, h], "prompt": prompt[:80],
               "inference_s": round(inf_s, 1), "total_s": round(total, 1), "files": nfiles,
               "verdict": verdict, "error": err, "sha256": shas, "pixel_sha256": pshas}
        self.results.append(rec)
        flag = "✓" if verdict == "PASS" else "✗"
        print(f"  {flag} {idx:02d} {cat:<5} {inf_s:5.1f}s {verdict:<4} {err[:80]}", flush=True)
        return rec

    # ---------- 收尾
    def teardown(self) -> dict:
        self._stop.set()
        sys.path.insert(0, str(ROOT))
        from compshare.cs import CS  # noqa: PLC0415
        cs = CS()
        try:
            cs.terminate(self.uid)
        except Exception as e:  # noqa: BLE001
            print(f"[!] 删除实例失败：{e}（手动 rm {self.uid}）")
        elapsed = time.time() - self.started
        return {"elapsed_min": round(elapsed / 60, 1),
                "est_cost": round(self.hourly * elapsed / 3600, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default="qwen_image_t2i")
    ap.add_argument("--model", default="qwen_image")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--gpu", default="4090")
    ap.add_argument("--image", default="", help="img2img 的输入图（本地路径）")
    ap.add_argument("--no-seed-check", action="store_true")
    ap.add_argument("--seed-only", action="store_true", help="只做 seed 复现验证")
    ap.add_argument("--batch-scan", action="store_true", help="扫描 batch×分辨率 的显存上限")
    ap.add_argument("--combo", default="", help="只测一个组合 WxHxB，如 1024x1024x4")
    a = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    b = Bench(a.workflow, a.model, a.n, a.gpu, a.image)
    ts = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    payload = {"schema": "p21.bench/v1", "workflow": a.workflow, "model": a.model,
               "gpu": a.gpu, "started_at": ts, "cases": []}
    try:
        b.provision()
        if a.batch_scan:
            if a.combo:
                w0, h0, b0 = (int(x) for x in a.combo.lower().split("x"))
                combos = [(w0, h0, b0)]
            else:
                combos = [(1024, 1024, 1), (768, 1024, 2), (768, 1024, 4),
                          (512, 512, 4), (1024, 1024, 2)]
            payload["batch_scan"] = []
            print("[3] batch × 分辨率 扫描 …")
            for w, h, batch in combos:
                b.vram_peak = 0
                rec = b.run_case(1, "batch", CASES[0][1], w, h, seed=777,
                                 prefix=f"batch-{w}x{h}-b{batch}", batch=batch)
                rec["batch"] = batch
                rec["vram_peak_mb"] = b.vram_peak
                payload["batch_scan"].append(rec)
                print(f"    {w}x{h} batch={batch}: {rec['verdict']} "
                      f"显存 {b.vram_peak}MB {rec['error'][:60]}", flush=True)
        if not a.seed_only and not a.batch_scan:
            print(f"[3] 连测 {a.n} 条 …")
            for i, (cat, prompt, w, h) in enumerate(CASES[:a.n], 1):
                b.run_case(i, cat, prompt, w, h)
        if not a.no_seed_check:
            print("[4] seed 复现（同 seed 跑两次，逐张 sha256 比对）…")
            r1 = b.run_case(90, "seed", CASES[0][1], CASES[0][2], CASES[0][3],
                            seed=12345, tag="a", prefix="benchseed", denoise=0.55)
            r2 = b.run_case(91, "seed", CASES[0][1], CASES[0][2], CASES[0][3],
                            seed=12345, tag="b", prefix="benchseed", denoise=0.55)
            same_file = bool(r1["sha256"]) and r1["sha256"] == r2["sha256"]
            same_pix = bool(r1.get("pixel_sha256")) and r1.get("pixel_sha256") == r2.get("pixel_sha256")
            payload["seed_check"] = {"seed": 12345, "same_file": same_file,
                                     "same_pixels": same_pix,
                                     "sha_a": r1.get("sha256"), "sha_b": r2.get("sha256"),
                                     "pixel_sha_a": r1.get("pixel_sha256"),
                                     "pixel_sha_b": r2.get("pixel_sha256")}
            print(f"    文件哈希{'一致' if same_file else '不一致'} · "
                  f"像素哈希{'一致' if same_pix else '不一致'}")
        payload["vram_peak_mb"] = b.vram_peak
    finally:
        end = b.teardown()
        cases = b.results
        ok = sum(1 for r in cases if r["verdict"] == "PASS")
        payload.update({
            "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "cases": cases, "total": len(cases), "pass": ok,
            "success_rate": round(ok / len(cases), 3) if cases else 0,
            "inference_p50_s": _pct([r["inference_s"] for r in cases], 0.5),
            "inference_p95_s": _pct([r["inference_s"] for r in cases], 0.95),
            "vram_peak_mb": b.vram_peak,
            **end,
        })
        path = OUT_DIR / f"bench-{a.workflow}-{ts}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[5] 结果 {path.relative_to(ROOT)}")
        print(f"    成功 {ok}/{len(cases)} · P50 {payload['inference_p50_s']}s · "
              f"P95 {payload['inference_p95_s']}s · 显存峰值 {b.vram_peak}MB · "
              f"{end['elapsed_min']}min ≈ ¥{end['est_cost']}")
    return 0 if payload.get("pass", 0) == payload.get("total", 1) else 1


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[i], 1)


if __name__ == "__main__":
    raise SystemExit(main())
