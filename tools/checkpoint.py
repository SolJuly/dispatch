#!/usr/bin/env python3
"""任务3：明确的检查点（CP0–CP7）。

把"模型 / 输入 / 输出 / 日志路径"落成可判定的检查点，每个检查点给出
判据、期望值、实测值、结论。跑完直接知道这台机器能不能开工、跑完的产物能不能用。

用法:
    python3 tools/checkpoint.py --instance audit-store
    python3 tools/checkpoint.py --instance audit-store --json
    python3 tools/checkpoint.py --instance audit-store --video   # 额外要求 SVD 模型
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COLLECT = ROOT / "lab" / "checkpoint.sh"


def collect(inst, cs) -> dict:
    """把 lab/checkpoint.sh 通过 stdin 送到远端执行，解析 KEY=VALUE。"""
    import csctl
    user, ip, pw, port = csctl._ssh_cmd(inst, cs)
    body = COLLECT.read_text(encoding="utf-8")
    r = subprocess.run(
        ["sshpass", "-p", pw, "ssh", "-p", str(port),
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=10", f"{user}@{ip}", "bash -s"],
        input=body, capture_output=True, text=True, timeout=120)
    out = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    out["_rc"] = r.returncode
    if r.returncode != 0:
        out["_err"] = (r.stderr or "")[:300]
    return out


def isver(v) -> bool:
    """版本号形如 580.159.04 / 13.0 / 2.4.0+cu124。"""
    return bool(re.match(r"^\d+(\.\d+)+", str(v or "").strip()))


def num(v, default=None):
    try:
        return float(str(v).strip())
    except Exception:
        return default


# ------------------------------------------------------------ 检查点定义
def build_checks(k: dict, want_video: bool) -> list[dict]:
    C: list[dict] = []

    def add(cp: str, name: str, ok: bool, expect: str, actual: str, note: str = "",
            soft: bool = False):
        # 每条判据都挂上该检查点依赖的路径（CP_PATHS 在此被显式引用）
        C.append({"cp": cp, "name": name, "ok": bool(ok), "expect": expect,
                  "actual": actual, "note": note, "soft": soft,
                  "paths": paths_of(cp)})

    # ---- CP0 开机就绪：能不能干活
    add("CP0", "GPU 可见", bool(k.get("GPU_NAME")), "nvidia-smi 返回卡名",
        k.get("GPU_NAME") or "(空)")
    add("CP0", "驱动版本", isver(k.get("GPU_DRIVER")), "有版本号", k.get("GPU_DRIVER", "-"))
    add("CP0", "CUDA 版本", (k.get("CUDA") or "-") not in ("-", ""), "有版本号", k.get("CUDA", "-"))
    add("CP0", "NVML 可初始化", k.get("NVML_OK") == "yes", "yes（save-image 后可能失效）",
        k.get("NVML_OK", "-"), "实测：制作镜像后新进程 NVML 失败，需 stop/start 恢复")
    booting = num(k.get("UPTIME_S"), 9999) < 300
    add("CP0", "ComfyUI 服务端能看到 GPU", "cuda" in (k.get("COMFY_DEVICE") or ""),
        "含 cuda:<n> 卡名", (k.get("COMFY_DEVICE") or "-")[:40],
        "容器刚起，ComfyUI 仍在加载" if booting else "", soft=booting)
    add("CP0", "ComfyUI 端口 8188", k.get("PORT8188") == "up", "up", k.get("PORT8188", "-"),
        "容器刚起，ComfyUI 仍在加载" if booting else "", soft=booting)
    add("CP0", "共享库 /model 已挂载", k.get("SHARED_MNT") == "yes", "yes", k.get("SHARED_MNT", "-"))

    # ---- CP1 环境：跑得起来
    add("CP1", "torch 可用", (k.get("TORCH") or "-") != "-", "有版本", k.get("TORCH", "-"))
    add("CP1", "torch 里 CUDA 可用（SSH 会话）", k.get("TORCH_CUDA") == "True", "True",
        k.get("TORCH_CUDA", "-"), "实测 SSH 会话常为 False（NVML 不可见），推理走 ComfyUI HTTP",
        soft=True)
    add("CP1", "ComfyUI 版本", bool(k.get("COMFY_VER")), "有版本", k.get("COMFY_VER", "-"))
    add("CP1", "PyAV（视频编码）", (k.get("AV_VER") or "-") != "-", "有版本", k.get("AV_VER", "-"))
    add("CP1", "PIL（图像）", (k.get("PIL_VER") or "-") != "-", "有版本", k.get("PIL_VER", "-"))
    # 已知约束：镜像里没有 ffmpeg / rsync，属于"已知缺失"，不判失败
    add("CP1", "ffmpeg", k.get("FFMPEG") == "yes", "yes（缺失则用 PyAV 编码）",
        k.get("FFMPEG", "-"), "镜像已知不带", soft=True)
    add("CP1", "rsync", k.get("RSYNC") == "yes", "yes（缺失则用 tar 流）",
        k.get("RSYNC", "-"), "镜像已知不带", soft=True)

    # ---- CP2 模型检查点：模型在不在、是不是完整文件
    add("CP2", "共享库分类可见", num(k.get("SHARED_CAT"), 0) > 0, ">0 个分类",
        k.get("SHARED_CAT", "0"))
    add("CP2", "SDXL checkpoint 存在", bool(k.get("SDXL_FILE")), "juggernaut* 命中一个",
        (k.get("SDXL_FILE") or "(未命中)")[-40:])
    if want_video:
        add("CP2", "SVD-XT 模型存在", bool(k.get("SVD_FILE")),
            "checkpoints/svd_xt.safetensors", (k.get("SVD_FILE") or "(未命中)")[-28:])
        add("CP2", "diffusion_models 非空", num(k.get("SHARED_UNET"), 0) > 0, ">0",
            k.get("SHARED_UNET", "0"))

    # ---- CP3 输入检查点：输入区就位
    add("CP3", "输入目录存在", k.get("INPUT_DIR") == "yes", "yes", k.get("INPUT_DIR", "-"))
    add("CP3", "工作流目录存在", k.get("WF_DIR") == "yes", "yes", k.get("WF_DIR", "-"))

    # ---- CP4 运行检查点：服务活着且脱离 SSH
    add("CP4", "ComfyUI 进程存在", bool(k.get("COMFY_PROC")), "有 PID", k.get("COMFY_PROC") or "(无)")
    add("CP4", "启动链根已脱离 SSH（PPID=1）", k.get("COMFY_ROOT_PPID") == "1",
        "1", k.get("COMFY_ROOT_PPID", "-"),
        "从 ComfyUI 主进程向上追启动链根；链上出现 sshd 即判定挂 SSH")
    add("CP4", "ComfyUI 启动完成", k.get("LOG_STARTED") == "yes", "日志出现 Starting server",
        k.get("LOG_STARTED", "-"),
        "以启动完成标志为准，比'零异常栈'更能代表服务可用")
    add("CP4", "日志无异常栈", num(k.get("LOG_ERROR"), 0) == 0, "0 次致命异常",
        k.get("LOG_ERROR", "-"),
        "已排除 [WARNING] 与 ComfyUI-Manager 取网失败（受限网络必然发生）",
        soft=True)
    add("CP4", "日志有执行记录", num(k.get("LOG_PROMPT"), 0) >= 1, "≥1 次 Prompt executed",
        k.get("LOG_PROMPT", "0"), soft=True)
    util = (k.get("GPU_UTIL") or "").split(",")
    add("CP4", "GPU 显存占用", True, "记录值", util[1].strip() if len(util) > 1 else "-",
        soft=True)

    # ---- CP5 输出检查点：产物存在且像样
    add("CP5", "输出目录存在", k.get("OUTPUT_DIR") == "yes", "yes", k.get("OUTPUT_DIR", "-"))
    add("CP5", "输出目录非空", num(k.get("OUTPUTS"), 0) > 0, ">0 个文件", k.get("OUTPUTS", "0"))

    # ---- CP6 归档/资源检查点：盘不满、数据盘在
    dp = num(k.get("DISK_PCT"))
    add("CP6", "系统盘余量", dp is not None and dp < 85, "<85%",
        f"{k.get('DISK_PCT','?')}%（剩 {k.get('DISK_FREE','?')}）")
    add("CP6", "日志文件存在", k.get("LOG_FILE") == "yes", "yes", k.get("LOG_FILE", "-"))
    add("CP6", "数据盘挂载", (k.get("DATA_MNT") or "none") != "none", "有挂载点",
        k.get("DATA_MNT", "none"), "20G 数据盘，随实例删除", soft=True)

    # ---- CP7 收尾：时间与版本可追溯
    add("CP7", "采集时间", bool(k.get("NOW")), "有时间戳", k.get("NOW", "-"))
    add("CP7", "系统已运行秒数", num(k.get("UPTIME_S"), -1) >= 0, "≥0", k.get("UPTIME_S", "-"))
    return C


def summarize(checks: list[dict]) -> dict:
    cps = {}
    for c in checks:
        d = cps.setdefault(c["cp"], {"total": 0, "pass": 0, "fail": 0, "soft_fail": 0})
        d["total"] += 1
        if c["ok"]:
            d["pass"] += 1
        elif c["soft"]:
            d["soft_fail"] += 1
        else:
            d["fail"] += 1
    hard = sum(1 for c in checks if not c["ok"] and not c["soft"])
    return {"by_cp": cps, "hard_fail": hard,
            "total": len(checks), "pass": sum(1 for c in checks if c["ok"])}


CP_TITLES = {
    "CP0": "开机就绪（能不能干活）",
    "CP1": "环境（跑得起来）",
    "CP2": "模型检查点（模型在不在）",
    "CP3": "输入检查点（输入区就位）",
    "CP4": "运行检查点（服务活着且脱离 SSH）",
    "CP5": "输出检查点（产物存在）",
    "CP6": "归档/资源（盘不满、数据盘在）",
    "CP7": "收尾（可追溯）",
}


# ------------------------------------------------------------ 路径生命周期配置
# 13 个探针路径：路径定义来自 lab/probe_storage.sh 的 ITEMS 数组，
# 生命周期行为来自 reports/audit-storage-2026-10-26.md 的实测矩阵。
# 键名同时提供英文（便于程序读取）和中文（便于报告引用）两套。
_PATH_ROWS = [
    # key, path, media, stop_start, reboot, save_image_new, terminate, desc
    ("root",         "/root",                               "overlay",
     "保留", "保留", "保留（镜像内）", "丢失", "脚本、配置"),
    ("comfy_output", "/root/ComfyUI/output",                 "overlay",
     "保留", "保留", "保留", "丢失", "出图/出片产物"),
    ("comfy_models", "/root/ComfyUI/models",                 "overlay",
     "保留", "保留", "保留", "丢失", "本地权重"),
    ("custom_nodes", "/root/ComfyUI/custom_nodes",           "overlay",
     "保留", "保留", "保留", "丢失", "自定义节点"),
    ("workflows",    "/root/ComfyUI/user/default/workflows", "overlay",
     "保留", "保留", "保留", "丢失", "工作流 JSON"),
    ("conda_env",    "/root/miniconda3",                     "overlay",
     "保留", "保留", "保留", "丢失", "Python 环境"),
    ("tmp",          "/tmp",                                 "overlay",
     "保留", "保留", "保留", "丢失", "临时文件"),
    ("varlog",       "/var/log",                             "overlay",
     "保留", "保留", "保留", "丢失", "系统日志"),
    ("etc",          "/etc",                                 "overlay",
     "保留", "保留", "保留", "丢失", "系统配置"),
    ("shared_model", "/model",                               "网络存储（UPFS 40T，只读）",
     "保留", "保留", "保留", "保留", "共享模型库，唯一跨实例存活"),
    ("data_disk",    "/cloud/cloud-ssd1",                    "独立数据盘（20G）",
     "保留", "保留", "丢失", "丢失", "只跟本实例，存镜像带不走"),
    ("comfy_log",    "/root/ComfyUI/comfyui_8188.log",       "overlay",
     "保留", "保留", "保留", "丢失", "ComfyUI 运行日志"),
    ("output_png",   "/root/ComfyUI/output",                 "overlay",
     "保留", "保留", "保留", "丢失", "产物文件（与 comfy_output 同目录，探针标签不同）"),
]

PATHS: list[dict] = [
    {
        "key": k,
        "path": p,
        "media": m,
        "desc": d,
        # 英文键（程序读取）
        "stop_start": ss,
        "reboot": rb,
        "save_image_new": si,
        "terminate": tm,
        # 中文键（验收/报告引用）
        "stop保留": ss,
        "reboot保留": rb,
        "saveimage保留": si,
        "terminate保留": tm,
    }
    for (k, p, m, ss, rb, si, tm, d) in _PATH_ROWS
]

PATH_INDEX: dict[str, dict] = {p["key"]: p for p in PATHS}

# 每个检查点依赖哪些路径（执行逻辑里通过 CP_PATHS 显式引用）
CP_PATHS: dict[str, list[str]] = {
    "CP0": ["root", "shared_model"],                  # 开机就绪：本地脚本 + 共享库挂载
    "CP1": ["conda_env", "tmp", "etc"],               # 环境：Python 环境 / 临时区 / 系统配置
    "CP2": ["shared_model", "comfy_models"],          # 模型：共享库 + 本地权重
    "CP3": ["root", "workflows"],                     # 输入：输入区 + 工作流目录
    "CP4": ["comfy_log", "root"],                     # 运行：日志 + 启动脚本
    "CP5": ["comfy_output", "output_png"],            # 输出：产物目录 + 产物文件
    "CP6": ["varlog", "data_disk"],                   # 资源：日志区 + 数据盘
    "CP7": ["root"],                                  # 收尾：配置与时间戳落盘位置
}


def paths_of(cp: str) -> list[str]:
    """该检查点涉及的路径 key 列表。"""
    return CP_PATHS.get(cp, [])


def path_brief(key: str, override_path: str | None = None) -> str:
    """单条路径的一行摘要，用于文本输出。

    形如：/root(overlay,stop保留/reboot保留/saveimage保留/terminate丢失)
    /model 是唯一跨实例存活的路径，单独表述。
    override_path：探针实际探测到的路径（镜像差异时覆盖名义路径）。
    """
    p = PATH_INDEX.get(key)
    if not p:
        return key
    path = override_path or p["path"]
    if p["terminate保留"] == "保留":
        return f'{path}({p["media"]},始终保留)'
    return (f'{path}({p["media"]},'
            f'stop{p["stop保留"]}/reboot{p["reboot保留"]}/'
            f'saveimage{p["saveimage保留"]}/terminate{p["terminate保留"]})')


def paths_line(cp: str, collected: dict | None = None) -> str:
    """检查点 → 路径摘要行。

    同一条路径被多个探针标签覆盖时（例如 /root/ComfyUI/output 同时是
    comfy_output 和 output_png），只显示一次并标注标签，避免文本重复。
    """
    keys = paths_of(cp)
    if not keys:
        return "-"
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for k in keys:
        p = PATH_INDEX.get(k)
        if not p:
            continue
        path = p["path"]
        if path not in groups:
            groups[path] = []
            order.append(path)
        groups[path].append(k)

    parts = []
    for path in order:
        ks = groups[path]
        override = None
        if collected and "comfy_log" in ks:
            override = collected.get("LOG_PATH") or None
        brief = path_brief(ks[0], override)
        if len(ks) > 1:
            brief += f"[探针:{'+'.join(ks)}]"
        parts.append(brief)
    return ", ".join(parts)


def instantiate_paths(collected: dict) -> list[dict]:
    """用本次采集的实际探测值回填名义路径。

    镜像版本不同时（例如日志从 comfyui_8188.log 改成 comfy.log），
    仅在 JSON 里追加 actual_path 字段，原有键保持不动。
    """
    actual = {"comfy_log": (collected or {}).get("LOG_PATH")}
    out: list[dict] = []
    for p in PATHS:
        q = dict(p)
        a = actual.get(p["key"])
        if a and a != p["path"]:
            q["actual_path"] = a
        out.append(q)
    return out


def demo_collect() -> dict:
    """--dry-run 用的样例数据：不连实例、不产生任何费用、不含凭据。"""
    return {
        "NOW": "2026-10-27 00:00:00", "UPTIME_S": "120",
        "GPU_NAME": "Tesla V100S-PCIE-32GB", "GPU_DRIVER": "580.159.04",
        "CUDA": "12.4", "NVML_OK": "yes",
        "COMFY_DEVICE": "cuda:0 Tesla V100S-PCIE-32GB", "PORT8188": "up",
        "SHARED_MNT": "yes", "SHARED_CAT": "57",
        "SDXL_FILE": "juggernautXL_v9.safetensors", "SVD_FILE": "svd_xt.safetensors",
        "SHARED_UNET": "12",
        "TORCH": "2.4.0+cu124", "TORCH_CUDA": "False", "COMFY_VER": "0.3.40",
        "AV_VER": "14.4.0", "PIL_VER": "10.2.0", "FFMPEG": "no", "RSYNC": "no",
        "INPUT_DIR": "yes", "WF_DIR": "yes",
        "COMFY_PROC": "41", "COMFY_ROOT_PPID": "1",
        "LOG_ERROR": "0", "LOG_PROMPT": "3", "LOG_STARTED": "yes", "GPU_UTIL": "0, 0 MiB",
        "OUTPUT_DIR": "yes", "OUTPUTS": "4",
        "DISK_PCT": "31", "DISK_FREE": "54G", "LOG_FILE": "yes",
        "DATA_MNT": "/cloud/cloud-ssd1", "_rc": 0,
    }


def main() -> int:
    args = sys.argv[1:]
    ident = "audit-store"
    if "--instance" in args:
        ident = args[args.index("--instance") + 1]
    want_video = "--video" in args
    dry_run = "--dry-run" in args

    if dry_run:
        # 离线自检路径：只读，不连实例、不产生 GPU 费用、不写报告文件
        k = demo_collect()
        name, uid, state = "dry-run-sample", "dry-run", "Offline"
    else:
        from compshare.cs import CS
        cs = CS()
        inst = cs.get(ident)
        k = collect(inst, cs)
        if k.get("_rc") != 0:
            print(f"采集失败 rc={k.get('_rc')}: {k.get('_err','')[:200]}")
            return 2
        name, uid, state = inst.get("Name"), inst.get("UHostId"), inst.get("State")

    checks = build_checks(k, want_video)
    s = summarize(checks)
    result = {"instance": uid, "name": name, "state": state,
              "collected": k, "paths": instantiate_paths(k),
              "checks": checks, "summary": s}

    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if s["hard_fail"] == 0 else 1

    print(f"检查点报告  {name} ({uid})  {k.get('NOW','')}")
    verdict = "✅ 全部通过" if s["hard_fail"] == 0 else f"❌ {s['hard_fail']} 项硬性未通过"
    print(f"结论: {verdict}   {s['pass']}/{s['total']} 通过")
    cur = None
    for c in checks:
        if c["cp"] != cur:
            cur = c["cp"]
            d = s["by_cp"][cur]
            status = "PASS" if d["fail"] == 0 and d["soft_fail"] == 0 else (
                "WARN" if d["fail"] == 0 else "FAIL")
            print(f"\n{cur} {CP_TITLES.get(cur,'')}   [{status}]  {d['pass']}/{d['total']}")
            print(f"  检查路径: {paths_line(cur, k)}")
            print(f"  {'判据':<28}{'期望':<26}{'实测':<28}{'':<4}备注")
            print("  " + "-" * 100)
        mark = "✅" if c["ok"] else ("⚠️" if c["soft"] else "❌")
        note = c["note"]
        print(f"  {c['name']:<28}{c['expect']:<26}{str(c['actual'])[:26]:<28}{mark}   {note}")

    if dry_run:
        print("\n（--dry-run 离线自检，未连接实例、未写报告文件）")
        return 0 if s["hard_fail"] == 0 else 1

    out = ROOT / "reports" / "checkpoint" / f"{name or uid}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0 if s["hard_fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
