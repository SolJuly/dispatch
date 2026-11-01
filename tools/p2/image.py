#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 镜像烘焙编排（宿主侧）：bake / verify / show。

    python3 tools/p2/image.py bake --layer base  --gpu 4090 --hours 1
    python3 tools/p2/image.py bake --layer media --gpu 4090 --hours 1   # 从 image.json 的 base 起机叠加 ffmpeg
    python3 tools/p2/image.py verify --runs 3 --layer all
    python3 tools/p2/image.py show

    --dry-run  只打印计划不建机（离线测试）
    --keep-on-fail  烘焙失败时保留实例（默认失败也销毁）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from compshare.cs import CS, CompShareError  # noqa: E402
from tools.p2 import config  # noqa: E402

SSH_PORT = "23"
RETRY_8903 = 3


def _ssh_run(host: str, port: str, pwd: str, cmd: str, timeout=300) -> tuple[int, str]:
    base = ["sshpass", "-p", pwd, "ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=8",
            f"root@{host}", cmd]
    try:
        p = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT({timeout}s)"


def _scp_put(host: str, port: str, pwd: str, local: Path, remote: str) -> bool:
    cmd = ["sshpass", "-p", pwd, "scp", "-q", "-P", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15",
           str(local), f"root@{host}:{remote}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return p.returncode == 0


def _wait_sshd(host: str, port: str, pwd: str) -> None:
    for i in range(30):
        rc, out = _ssh_run(host, port, pwd, "echo ssh-ready", timeout=25)
        if rc == 0 and "ssh-ready" in out:
            print(f"  sshd ready（{i * 5}s）", flush=True)
            return
        time.sleep(5)
    raise RuntimeError("sshd 60 次探活仍未就绪")


def create_instance(cs: CS, image: dict, gpu: str, name: str) -> tuple[str, dict, str, str, str]:
    """建机 → Running → sshd 就绪。返回 (uid, inst, host, port, pwd)。"""
    img_id = image["CompShareImageId"]
    price = cs.price(gpu, 1, 16, 64, "Postpay", 100, 0, image_id=img_id)
    print(f"  镜像 {image.get('Name')} · {gpu} · ≈¥{float(price.get('total') or 0):.2f}/h", flush=True)
    ids = cs.create(image_id=img_id, gpu_type=gpu, gpu=1, cpu=16, memory_gb=64,
                    name=name, charge_type="Postpay", disk_gb=100, data_disk_gb=0,
                    remark="p2 image")
    uid = ids[0]
    print(f"  已创建 {uid}，等 Running…", flush=True)
    inst = cs.wait_state(uid, {"running"}, timeout=900)
    host, port = cs.public_ip(inst), SSH_PORT
    pwd = cs.password_of(inst)
    _wait_sshd(host, port, pwd)
    return uid, inst, host, port, pwd


def _terminate(cs: CS, uid: str) -> None:
    for attempt in range(1, RETRY_8903 + 1):
        try:
            cs.terminate(uid)
            print(f"  已销毁 {uid}", flush=True)
            return
        except CompShareError as e:
            if "8903" in str(e) and attempt < RETRY_8903:
                time.sleep(10)
                continue
            raise


def _read_json_remote(host: str, port: str, pwd: str, path: str) -> dict | None:
    rc, out = _ssh_run(host, port, pwd, f"cat {path} 2>/dev/null")
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out[out.index("{"):].strip().splitlines()[0])
    except Exception:
        return None


def _wait_custom_image(cs: CS, name: str, timeout=600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        for i in cs.images(name, kinds=("Custom",)):
            if (i.get("Name") or "") == name:
                return i
        time.sleep(10)
    raise RuntimeError(f"镜像 {name} 在 {timeout}s 内未出现")


def _poll_bake(host: str, port: str, pwd: str, timeout: int = 1500) -> dict | None:
    """轮询远端 /root/.p2_bake_summary.json 直到出现（成功）或日志出现 ✗（失败）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(10)
        rc, out = _ssh_run(host, port, pwd,
                           "if [ -f /root/.p2_bake_summary.json ]; then "
                           "echo RESULT; cat /root/.p2_bake_summary.json; "
                           "else echo RUNNING; tail -n 6 /root/bake.log 2>/dev/null; fi",
                           timeout=30)
        if rc != 0:
            continue
        if out.lstrip().startswith("RESULT"):
            try:
                return json.loads(out[out.index("{"):].strip().splitlines()[0])
            except Exception:
                return None
        if "✗" in out:
            return None
    return None


def _load_images() -> dict:
    if config.IMAGE_JSON.exists():
        return json.loads(config.IMAGE_JSON.read_text(encoding="utf-8"))
    return {"schema": "p2.image/v2", "fallback_keyword": config.BASE_IMAGE_KEYWORD, "layers": {}}


def _save_images(d: dict) -> None:
    config.IMAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.IMAGE_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  image.json 已更新 → {config.IMAGE_JSON}", flush=True)


def cmd_bake(cs: CS, a) -> int:
    layer = a.layer
    if layer not in ("base", "media"):
        print(f"✗ layer 应为 base|media，得到 {layer}")
        return 1
    name = config.LAYER_NAMES[layer]

    # 确定起机镜像
    if layer == "base":
        image = cs.resolve_image(config.BASE_IMAGE_KEYWORD)
        from_id = ""
    else:
        d = _load_images()
        base = (d.get("layers") or {}).get("base") or {}
        base_id = base.get("image_id")
        if not base_id:
            print("✗ media 需要先 bake base（image.json 里没有 base.image_id）")
            return 1
        image = cs.resolve_image(base_id)
        from_id = base_id

    print(f"=== bake {layer} · 镜像 {image.get('Name')} ({image.get('CompShareImageId')}) ===")
    if a.dry_run:
        print(f"  [dry-run] base：建机→instance_setup.sh→写marker→save-image")
        print(f"  [dry-run] media：建机(base镜像)→ffmpeg软链→写marker→save-image")
        return 0

    uid = inst = host = port = pwd = None
    try:
        uid, inst, host, port, pwd = create_instance(cs, image, a.gpu, f"p2-bake-{layer}")
        cs.stop_at(uid, int(time.time() + 3600))
        has_ffmpeg = False

        if layer == "base":
            # 复用 P1 已验证的 instance_setup.sh：config + 重启 ComfyUI + 6 项资产自检。
            # 它内部用 `pkill -f '[m]ain.py'`（方括号技巧）避免误杀执行进程，且重启走 --detach。
            print("  跑 instance_setup.sh（config + 重启 + 自检，最长 ~5 分钟）…", flush=True)
            env = dict(os.environ)
            env.update({"P1_HOST": host, "P1_PORT": port, "P1_PASSWORD": pwd})
            p = subprocess.run(["bash", str(_ROOT / "tools" / "p1" / "instance_setup.sh"),
                                uid, "--need=all"],
                               capture_output=True, text=True, timeout=a.bake_timeout, env=env)
            tail = ((p.stdout or "") + (p.stderr or ""))[-2500:]
            print("  " + tail.replace("\n", "\n  ")[-2200:], flush=True)
            if p.returncode != 0 or "自检通过" not in tail:
                raise RuntimeError("instance_setup 未自检通过")
        else:
            # media：base 层已含全部配置，仅把自带 ffmpeg 软链进 PATH
            rc, out = _ssh_run(host, port, pwd,
                               "ln -sf /usr/local/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg && "
                               "ln -sf /usr/local/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe && "
                               "ffmpeg -version 2>&1 | head -1", timeout=120)
            print("  " + (out or "").strip(), flush=True)
            if rc != 0 or "ffmpeg version" not in out:
                raise RuntimeError("ffmpeg 软链失败")
            has_ffmpeg = True

        # 写 marker（供 instance_setup.sh 快路径识别「已烘焙镜像」）
        marker = {"schema": "p2.image.marker/v1", "layer": layer, "name": name,
                  "base": from_id, "comfy_ver": "0.36.x", "has_ffmpeg": has_ffmpeg,
                  "baked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        mf = Path(tempfile.mktemp(suffix=".json"))
        mf.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        if not _scp_put(host, port, pwd, mf, "/root/.p2_image.json"):
            raise RuntimeError("写 marker 失败")
        mf.unlink(missing_ok=True)
        print(f"  marker 已写（layer={layer} has_ffmpeg={has_ffmpeg}）", flush=True)

        print(f"  save-image → {name} …", flush=True)
        cs.save_image(uid, name, f"P2 {layer} layer, from {image.get('Name')}")
        img = _wait_custom_image(cs, name)
        print(f"  镜像就绪：{img.get('CompShareImageId')} · {name}", flush=True)

        d = _load_images()
        d.setdefault("layers", {})[layer] = {
            "name": name, "image_id": img.get("CompShareImageId"),
            "based_on": image.get("CompShareImageId"), "has_ffmpeg": has_ffmpeg,
            "comfy_ver": "0.36.x", "baked_at": marker["baked_at"],
        }
        _save_images(d)
        _terminate(cs, uid)
        return 0
    except Exception as e:
        print(f"✗ bake 失败：{type(e).__name__}: {e}")
        if uid and not a.keep_on_fail:
            try:
                _terminate(cs, uid)
            except Exception:
                pass
        elif uid:
            print(f"  实例 {uid} 已保留（请手工排查或 --abort）")
        return 1


def _boot_once(cs: CS, image: dict, gpu: str, layer: str) -> dict:
    name = f"p2-bench-{layer}"
    uid = inst = None
    rec = {"layer": layer, "image": image.get("Name")}
    t0 = time.time()
    try:
        ids = cs.create(image_id=image["CompShareImageId"], gpu_type=gpu, gpu=1, cpu=16,
                        memory_gb=64, name=name, charge_type="Postpay", disk_gb=100,
                        data_disk_gb=0, remark="p2 bench")
        uid = ids[0]
        inst = cs.wait_state(uid, {"running"}, timeout=900)
        rec["t_running"] = round(time.time() - t0, 1)
        host, port, pwd = cs.public_ip(inst), SSH_PORT, cs.password_of(inst)
        _wait_sshd(host, port, pwd)
        rec["t_sshd"] = round(time.time() - t0, 1)
        # 等 object_info OK（ComfyUI 就绪）
        for i in range(80):
            rc, out = _ssh_run(host, port, pwd,
                               "/root/miniconda3/bin/python -c \"import urllib.request as u; "
                               "u.urlopen('http://127.0.0.1:8188/object_info', timeout=5); print('UP')\"",
                               timeout=20)
            if rc == 0 and "UP" in out:
                rec["t_ready"] = round(time.time() - t0, 1)
                break
            time.sleep(2)
        rec["t_ready"] = rec.get("t_ready") or round(time.time() - t0, 1)
        return rec
    finally:
        if uid:
            try:
                _terminate(cs, uid)
            except Exception:
                pass


def cmd_verify(cs: CS, a) -> int:
    d = _load_images()
    layers = d.get("layers") or {}
    picks = ["base", "media"] if a.layer == "all" else [a.layer]
    results = []
    for layer in picks:
        entry = layers.get(layer)
        if not entry or not entry.get("image_id"):
            print(f"✗ 缺少 {layer} 层镜像（先 bake）")
            continue
        image = cs.resolve_image(entry["image_id"])
        print(f"=== bench {layer} · {image.get('Name')} ×{a.runs} ===")
        for i in range(a.runs):
            rec = _boot_once(cs, image, a.gpu, layer)
            print(f"  #{i+1} {rec}")
            results.append(rec)
    config.BENCH_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nbench 结果 → {config.BENCH_JSON}")
    return 0


def cmd_show(cs: CS, a) -> int:
    d = _load_images()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="image", description="P2 镜像烘焙/验证")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("bake", help="烘焙一层镜像")
    sp.add_argument("--layer", required=True, choices=["base", "media"])
    sp.add_argument("--gpu", default=config.DEFAULT_GPU)
    sp.add_argument("--hours", type=float, default=1.0)
    sp.add_argument("--bake-timeout", type=int, default=1500, help="烘焙轮询上限秒数")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--keep-on-fail", action="store_true")
    sp.set_defaults(fn=cmd_bake)

    sp = sub.add_parser("verify", help="镜像起机耗时基准")
    sp.add_argument("--runs", type=int, default=3)
    sp.add_argument("--layer", default="all", choices=["all", "base", "media"])
    sp.add_argument("--gpu", default=config.DEFAULT_GPU)
    sp.set_defaults(fn=cmd_verify)

    sub.add_parser("show", help="查看镜像登记").set_defaults(fn=cmd_show)

    a = ap.parse_args(argv)
    cs = CS()
    return a.fn(cs, a)


if __name__ == "__main__":
    raise SystemExit(main())
