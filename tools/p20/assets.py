#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.0 模型资产：把「模型需要哪些权重可见」从 setup 脚本里抽出来。

P1 的 instance_setup.sh 把 H3/Qwen 的 6 个文件名写死在远端 python 里，换模型就得改脚本。
P2.0 的模型配置声明 files（角色→文件+loader/input+匹配规则），这里：

  1. 生成一段远端 Python（base64 传入，避免引号地狱），读 ComfyUI /object_info，
     逐条判断「Loader 的候选列表里有没有这个文件」；
  2. 输出 ASSET_CHECK:{json}，本机解析成 {asset_key: bool}；
  3. 缺失时给出人话原因（loader 不存在 / 文件名不匹配）。

remote check 不依赖 sshpass：调用方传进来一个 ssh_run(cmd) 可调用对象（run.py 已有）。
"""
from __future__ import annotations

import base64
import json
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REMOTE_PY = r'''
import base64, json, sys, urllib.request
spec = json.loads(base64.b64decode(sys.argv[1]).decode())
try:
    oi = json.loads(urllib.request.urlopen('http://127.0.0.1:8188/object_info', timeout=30).read())
except Exception as e:
    print('ASSET_CHECK:' + json.dumps({'__error__': '%s: %s' % (type(e).__name__, e)}))
    raise SystemExit(0)
res = {}
for a in spec:
    key, loader, inp, rule = a['key'], a['loader'], a['input'], a.get('match') or {}
    try:
        vals = oi[loader]['input']['required'][inp][0]
    except Exception:
        res[key] = {'ok': False, 'why': 'loader/input 不存在: %s.%s' % (loader, inp)}
        continue
    # 新版 ComfyUI：combo 输入写成 ["COMBO", {"options": [...], ...}]
    if isinstance(vals, str) and vals == 'COMBO':
        meta = oi[loader]['input']['required'][inp][1] if len(oi[loader]['input']['required'][inp]) > 1 else {}
        vals = (meta or {}).get('options') or []
    if not isinstance(vals, list):
        vals = [vals]
    if 'equals' in rule:
        ok = any(v == rule['equals'] for v in vals)
        why = '未找到 %s' % rule['equals']
    elif 'contains' in rule:
        ok = any(rule['contains'] in v for v in vals if isinstance(v, str))
        why = '没有包含 %s 的文件' % rule['contains']
    elif 'regex' in rule:
        import re
        ok = any(isinstance(v, str) and re.search(rule['regex'], v) for v in vals)
        why = '没有匹配 /%s/ 的文件' % rule['regex']
    else:
        ok = bool(vals)
        why = '候选列表为空'
    res[key] = {'ok': bool(ok), 'why': '' if ok else why}
print('ASSET_CHECK:' + json.dumps(res, ensure_ascii=False))
'''


def asset_rules(model: dict) -> list[dict]:
    """模型需要可见的资产规则。优先 model.assets；否则由 files 自动派生（equals 文件名）。"""
    explicit = model.get("assets") or []
    if explicit:
        return [{"key": a.get("key") or f"{a['loader']}.{a['input']}",
                 "loader": a["loader"], "input": a["input"],
                 "match": a.get("match") or {"equals": a.get("name") or ""}}
                for a in explicit]
    out = []
    for role, f in (model.get("files") or {}).items():
        rule = f.get("match") or {"equals": f.get("name")}
        out.append({"key": role, "loader": f["loader"], "input": f["input"], "match": rule})
    return sorted(out, key=lambda x: x["key"])


def build_check_command(model: dict, *, activate: str = "source /root/miniconda3/bin/activate") -> str:
    payload = base64.b64encode(json.dumps(asset_rules(model), ensure_ascii=False).encode()).decode()
    b64 = base64.b64encode(REMOTE_PY.encode()).decode()
    return (f"{activate} && python3 -c \"import base64;exec(base64.b64decode('{b64}'))\" {shlex.quote(payload)}")


def parse_check_output(out: str) -> tuple[dict | None, str]:
    """从 ssh 输出里找 ASSET_CHECK:{...}；返回 (结果字典, 错误说明)。"""
    for line in (out or "").splitlines():
        i = line.find("ASSET_CHECK:")
        if i < 0:
            continue
        try:
            d = json.loads(line[i + len("ASSET_CHECK:"):])
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            return None, "ASSET_CHECK 不是对象"
        if "__error__" in d:
            return None, str(d["__error__"])
        return d, ""
    return None, "没有 ASSET_CHECK 输出（ComfyUI 未就绪或远端 python 失败）"


def run_check(model: dict, ssh_run, *, attempts: int = 20, interval: float = 6.0,
              timeout: int = 90) -> tuple[bool, list[str], dict]:
    """轮询远端资产检查。ssh_run(cmd, timeout=...) -> (rc, out)。"""
    import time

    last = ""
    for i in range(attempts):
        rc, out = ssh_run(build_check_command(model), timeout=timeout)
        res, err = parse_check_output(out)
        if res is not None:
            missing = [k for k, v in res.items() if not (v or {}).get("ok")]
            why = {k: (v or {}).get("why", "") for k, v in res.items() if not (v or {}).get("ok")}
            return (not missing), missing, {"result": res, "why": why, "attempt": i + 1}
        last = err or (out or "")[-200:]
        if i + 1 < attempts:
            time.sleep(interval)
    return False, [], {"error": last}


def apply_links(model: dict, ssh_run, *, timeout: int = 60) -> list[dict]:
    """执行模型配置里的 setup.links（例：把断链软链到真实文件）。不配则零操作。"""
    out = []
    for ln in ((model.get("setup") or {}).get("links") or []):
        link, target = ln.get("link"), ln.get("target")
        if not link or not target:
            continue
        cmd = f"mkdir -p {shlex.quote(str(link.rsplit('/', 1)[0]))} && ln -sf {shlex.quote(target)} {shlex.quote(link)} && echo LINK_OK"
        rc, txt = ssh_run(cmd, timeout=timeout)
        out.append({"link": link, "target": target, "ok": rc == 0 and "LINK_OK" in (txt or ""),
                    "out": (txt or "")[-160:]})
    return out


def setup_profile(model: dict) -> str:
    """模型配置里的 setup.profile（h3/qwen/all/none），供 instance_setup.sh --need。"""
    return ((model.get("setup") or {}).get("profile") or "all")


# ---------------------------------------------------------------- 自动软链
# Loader → ComfyUI models 子目录（v4 镜像的口径）
# loader 类 → ComfyUI/models 下的子目录。
#
# **这份表必须和 tools/p2/preflight.py 的同名表一致**，否则会出现
# 「预检说有权重、真机说没有」——2026-11-10 实测踩到：
# `LatentUpscaleModelLoader` 在这里没登记，落到默认的 diffusion_models，
# 而它其实归 upscale_models。软链建到了错目录，ComfyUI 当然看不见。
#
# 缺项一律回落 diffusion_models（不是错，但会让资产检查报"没有包含 X 的文件"）。
LOADER_DIRS = {
    "UNETLoader": "diffusion_models",
    "UnetLoaderGGUF": "diffusion_models",
    "CLIPLoader": "text_encoders",
    "DualCLIPLoader": "text_encoders",
    "VAELoader": "vae",
    "LoraLoaderModelOnly": "loras",
    "LoraLoader": "loras",
    "CheckpointLoaderSimple": "checkpoints",
    "CLIPVisionLoader": "clip_vision",
    "ControlNetLoader": "controlnet",
    "UpscaleModelLoader": "upscale_models",
    # 注意：这个 loader 读的是 `latent_upscale_models`，不是 `upscale_models`。
    # 2026-11-11 实测：链到 upscale_models 后节点候选列表仍是 0 项，
    # 因为 folder_paths.py:43 把 latent_upscale_models 单独注册成一个目录。
    "LatentUpscaleModelLoader": "latent_upscale_models",
    "ModelPatchLoader": "model_patches",
}


def model_filenames(model: dict) -> list[tuple[str, str, str]]:
    """[(subdir, name, loader)] —— name 可能带子目录（如 qwen/xxx.safetensors）。"""
    out = []
    for _role, f in (model.get("files") or {}).items():
        name = (f or {}).get("name")
        loader = (f or {}).get("loader") or ""
        if not name:
            continue
        out.append((LOADER_DIRS.get(loader, "diffusion_models"), str(name), loader))
    return out


def build_find_command(model: dict, maxdepth: int = 7) -> str:
    """一次 find 把 /model 下的所有权重捞回来，**归一化比对在本地做**。

    走过的三次弯路（2026-11-10，每次都是真机开完机才发现）：

      ① `-name` 精确匹配 → `ltx_2.3_...` 找不到 `ltx-2.3-...`（连接符不同）
      ② `-iname` 大小写不敏感了，但连接符仍是逐一比对，对不上
      ③ 枚举所有连接符组合 → 真名是**混合**写法
         `ltx-2.3-22b-distilled-1.1_lora-dynamic_fro09_avg_rank_111_bf16`
         前半 `-` 后半 `_`；而 12 段 = 2048 种组合，为不炸命令行必须截断，
         截断恰好漏掉最长的那些名字

    结论：**不要试图在 shell 里表达等价关系。** 直接把候选捞回来，
    在 Python 里归一后比对。`/model` 有 5211 个权重，一轮 find ~4 秒、
    8000 行输出，比拼 2048 个 `-iname` 便宜，而且彻底消除这一类问题。
    """
    if not model_filenames(model):
        return "true"
    return (f"find /model -maxdepth {maxdepth} -type f \\( "
            f"-iname '*.safetensors' -o -iname '*.ckpt' -o -iname '*.pt' -o "
            f"-iname '*.pth' -o -iname '*.bin' -o -iname '*.gguf' \\) "
            f"-printf '%f\\t%p\\n' 2>/dev/null | head -8000")


def _norm_name(s: str) -> str:
    """文件名归一：小写 + 把 `-` 和 `.` 都折算成 `_`。

    **只**做这两件事。版本号（rank_111 vs 105）、精度（fp8 vs int8）、
    规模（3b vs 7b）必须保持可区分 —— 归一过头会让作业静默用错权重，
    那种错误在成片里根本看不出来。
    """
    return str(s or "").strip().lower().replace("-", "_").replace(".", "_")



def parse_find_output(out: str) -> dict[str, str]:
    """'名字\\t路径' → {名字: 路径}（同名取第一个）。

    只收 `/model` 下的绝对路径 —— find 输出里可能混进 ssh 的告警行
    （"Permanently added ... to the list of known hosts"），那些要丢掉。
    """
    found: dict[str, str] = {}
    for line in (out or "").splitlines():
        if "\t" not in line:
            continue
        name, path = line.split("\t", 1)
        name, path = name.strip(), path.strip()
        if name and path.startswith("/model") and name not in found:
            found[name] = path
    return found


def build_link_script(model: dict, found: dict[str, str],
                      comfy: str = "/root/ComfyUI/models") -> tuple[str, list[str]]:
    """按已找到的路径生成软链脚本；返回 (script, 缺失文件名列表)。

    查找顺序：精确同名 → 归一后同名。归一能对上 `ltx_2.3_` vs `ltx-2.3-`
    这类纯排版差异（见 build_find_command 的注释）。
    """
    lines = ["set -u"]
    missing = []
    # 归一索引：归一名 → 真实路径（同名取第一个）
    by_norm: dict[str, str] = {}
    for k, v in found.items():
        by_norm.setdefault(_norm_name(k), v)
    for subdir, name, _loader in model_filenames(model):
        base = Path(name).name
        target = found.get(base) or by_norm.get(_norm_name(base))
        if not target:
            missing.append(name)
            continue
        link = f"{comfy}/{subdir}/{name}"
        lines.append(f"mkdir -p {shlex.quote(str(Path(link).parent))}")
        lines.append(f"ln -sf {shlex.quote(target)} {shlex.quote(link)}")
    lines.append("echo LINK_DONE")
    return "\n".join(lines), missing


def load_compat_links(path: Path | None = None) -> dict[str, str]:
    """读 `tools/p1/compat_links.txt`：`ComfyUI 侧名字 → 真实文件路径`。

    这张表是**人工确认过的改名映射**。典型例子：

        qwen_std_vae.safetensors        → qwen_image_vae.safetensors
        gemma-3-12b-it-abliterated_...  → ..._heretic_lora_rank64_bf16.safetensors

    两边的名字**故意不同**，因为模型卡写的是 ComfyUI 侧的名字（工作流图里引用的
    就是它），而库里真实文件叫别的名字。有软链就能跑。

    2026-11-11 踩到的坑：自动补链**不读这张表**，只靠 `find` 按名字找，
    所以表里登记过的改名全部失效 —— gemma 那条明明写进了 compat_links，
    实例上依然报"模型资产不可见"。两套机制各干一半，谁也不认谁。
    """
    p = path or (ROOT / "tools" / "p1" / "compat_links.txt")
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, target = parts[1].strip(), parts[2].strip()
        if name and target.startswith("/model"):
            out[Path(name).name] = target
    return out


def link_model_files(model: dict, ssh_run, *, timeout: int = 240) -> list[str]:
    """把模型卡里的权重要么链好、要么明确报缺。返回缺失文件名列表。

    两条来源合并：
      ① `find /model` 扫出来的真实文件（覆盖新导入的 pack_* 模型）
      ② `compat_links.txt` 里人工确认的改名映射（覆盖"名字故意不同"的那批）

    只用 ① 会漏掉所有改名映射；只用 ② 会漏掉新模型
    （2026-11-07 的 fasth3 作业就是这么挂的）。两者都要。
    """
    rc, out = ssh_run(build_find_command(model), timeout=timeout)
    found = parse_find_output(out)
    if not found:
        # find 挂了（超时/路径不通）不算"缺失"，交给后面的资产检查报错
        return []
    # ② 合并人工映射：键是 ComfyUI 侧名字，值是真实路径
    for name, target in load_compat_links().items():
        found.setdefault(name, target)
    script, missing = build_link_script(model, found)
    ssh_run(script, timeout=timeout)
    return missing
