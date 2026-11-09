#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资产缓存层：把「权重文件在哪 / 有没有 / 能不能用 / 该淘汰谁」变成可查询的索引。

## 为什么需要这一层

改造前的状态（2026-11-09 审计 §5）：

    文档 §7.2 中央资产仓库    🟡 有卡无仓库
    文档 §7.4 缓存流程（5 步）🔴 0%

真实痛点是**换云就重来**：

  · 智云把所有权重放在 `/model` UPFS 40T 只读库，靠 `compat_links.txt`（30 行静态表）
    和 `assets.py` 的 `find /model` 兜底软链。这套只在智云成立。
  · 换到 AutoDL / RunPod 没有 `/model`，必须**下载**。下载到哪、下多少、
    磁盘放不放得下、下次能不能复用 —— 这些逻辑一行都没有。
  · 更贵的是：**不知道下载了什么**。模型卡写的是 `wan2.1_14B_SCAIL_2_fp16`，
    库里实际文件名是 `wan2.1_14B_SCAIL_2_fp16.safetensors`，
    同名不同路径 6 处，版本不同 3 处。没有索引就只能靠 `find` 现场猜。

## 五步流程（对应文档 §7.4）

    1. 解析 resolve()   模型卡 → 需要的文件清单 + 每个文件在哪（挂载/缓存/需下载）
    2. 校验 verify()    大小 + 可选 sha256；下载后用 .part 临时名，校验通过才原子改名
    3. 落位 place()     软链（有共享挂载）或下载（有外网）或报明确原因（都没有）
    4. 索引 index()     把「文件 → 路径 → 大小 → 来源 → 最后使用时间」写进 index.json
    5. 淘汰 evict()     磁盘水位超限时按 LRU 删可删项，**共享挂载只读不可删**

## 刻意的取舍

  - **索引是派生的**：删掉 `.compshare/asset_index.json` 可以靠 `scan()` 重建。
    唯一事实来源是模型卡 + 机器上的真实文件。索引只是加速。
  - **只读挂载永不淘汰**：`/model` 是平台级共享库，跨账号共享，
    删它等于删别人的数据。`evictable=False` 是硬门禁，不是建议。
  - **不做"猜名字"匹配**：匹配规则只认模型卡里的 `match`（equals/contains/regex），
    与 `assets.py` 保持同一套语义。模糊匹配会把 A 模型的文件链给 B 模型用，
    这种错误在成片里根本看不出来，只会得到"莫名其妙的糊图"。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".compshare"
INDEX_FILE = STATE_DIR / "asset_index.json"
INDEX_SCHEMA = "p2.assets/v1"

# 权重库的搜索根（跨云可配；智云是 /model，别家可挂 NAS/OSS 到同路径）
DEFAULT_SEARCH_ROOTS = ("/model/comfyui/models", "/model/ModelScope",
                        "/model/HuggingFace", "/model")
# 本地缓存根（有外网时下载到这里，再软链进 ComfyUI/models/<类型>/）
DEFAULT_CACHE_ROOT = "/opt/dsh-assets"


# ------------------------------------------------------------------ 数据模型
@dataclass
class AssetHit:
    """一个文件在某个位置被找到了。"""

    name: str                 # 模型卡里写的名字（可能含子目录，如 qwen/xxx.safetensors）
    path: str                 # 实际绝对路径
    size: int = 0
    sha256: str = ""
    source: str = "mount"     # mount / cache / download
    evictable: bool = False   # 共享挂载与平台库一律 False
    last_used: float = 0.0
    verified_at: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "size": self.size,
                "sha256": self.sha256, "source": self.source,
                "evictable": self.evictable, "last_used": self.last_used,
                "verified_at": self.verified_at}


@dataclass
class Resolution:
    """一个模型卡文件的三态解析结果。"""

    name: str
    subdir: str
    status: str               # hit / linkable / download / missing
    path: str = ""
    reason: str = ""
    size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("hit", "linkable", "download")

    def as_dict(self) -> dict:
        return {"name": self.name, "subdir": self.subdir, "status": self.status,
                "path": self.path, "reason": self.reason, "size": self.size,
                "ok": self.ok, **({"extra": self.extra} if self.extra else {})}


# ------------------------------------------------------------------ 索引
def load_index(path: Path | None = None) -> dict:
    p = path or INDEX_FILE
    if not p.is_file():
        return {"schema": INDEX_SCHEMA, "entries": {}, "updated_at": ""}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema": INDEX_SCHEMA, "entries": {}, "updated_at": ""}
    d.setdefault("entries", {})
    return d


def save_index(d: dict, path: Path | None = None) -> None:
    p = path or INDEX_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    d["schema"] = INDEX_SCHEMA
    d["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = p.with_suffix(".part")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)               # 原子替换：中途被 kill 不会留半截 JSON


def record(entries: dict, hit: AssetHit, *, now: float | None = None) -> None:
    """把一次命中写进索引（原地改 entries）。"""
    now = now if now is not None else time.time()
    prev = entries.get(hit.name) or {}
    entries[hit.name] = {
        **hit.as_dict(),
        # last_used 取「更新的那个」：scan 会把 last_used=0 写回来，
        # 不能因此丢掉真实的最近使用时间（否则 LRU 全乱）
        "last_used": max(float(prev.get("last_used") or 0), float(hit.last_used or now)),
        "first_seen": prev.get("first_seen") or now,
    }


# ------------------------------------------------------------------ 匹配规则
def match_name(name: str, rule: dict) -> bool:
    """按模型卡的 match 规则判断候选文件名是否等价。

    与 tools/p20/assets.py 的规则保持**完全一致**（那里是远端 python 里跑的，
    这里要能在本地做出同样的判断，否则"预检说能跑、真机说没文件"）。
    """
    if not rule:
        return bool(name)
    if "equals" in rule:
        return name == rule["equals"]
    if "contains" in rule:
        return rule["contains"] in name
    if "regex" in rule:
        try:
            return bool(re.search(rule["regex"], name))
        except re.error:
            return False
    return bool(name)


LOADER_DIRS = {
    "UNETLoader": "diffusion_models",
    "UnetLoaderGGUF": "diffusion_models",
    "checkpoint_loader": "checkpoints",
    "CheckpointLoaderSimple": "checkpoints",
    "CLIPLoader": "text_encoders",
    "DualCLIPLoader": "text_encoders",
    "VAELoader": "vae",
    "LoraLoader": "loras",
    "LoraLoaderModelOnly": "loras",
    "ControlNetLoader": "controlnet",
    "LatentUpscaleModelLoader": "upscale_models",
    "UpscaleModelLoader": "upscale_models",
    "ModelPatchLoader": "model_patches",
}


def model_requirements(model: dict) -> list[dict]:
    """模型卡 → 需要哪些文件：[{role, name, subdir, loader, match}]。"""
    out = []
    for role, f in (model.get("files") or {}).items():
        f = f or {}
        name = str(f.get("name") or "")
        if not name:
            continue
        loader = str(f.get("loader") or "")
        out.append({
            "role": role,
            "name": name,
            "basename": Path(name).name,
            "subdir": LOADER_DIRS.get(loader, "diffusion_models"),
            "loader": loader,
            "match": f.get("match") or {},
            "url": f.get("url") or (f.get("extra") or {}).get("url") or "",
        })
    return out


# ------------------------------------------------------------------ 五步：1 解析
def resolve(model: dict, *, available: dict[str, AssetHit] | None = None,
            can_link: bool = True, can_download: bool = True) -> list[Resolution]:
    """把模型卡解析成「每个文件该走哪条路」。

    available 是「机器上已知存在的文件」（来自 scan 或索引）；
    can_link / can_download 来自 provider.capabilities()，体现每家云的支持面。

    返回的 status 含义：
      hit       已经在目标位置或可软链的共享库里 —— 直接能用
      linkable  在共享库里能找到，但要建软链
      download  不在，但有外网且模型卡给了 url —— 可以下
      missing   不在，也无路可取 —— 明确报原因，不要让人跑到真机才发现
    """
    available = available or {}
    out: list[Resolution] = []
    for req in model_requirements(model):
        req_hit = _find_in(req, available)
        if req_hit:
            # placed = 已经在目标位置；其余（mount/verified/cache）都要建软链
            status = "hit" if req_hit.source == "placed" else "linkable"
            why = {"verified": "已确认的名字映射（compat_links）",
                   "mount": "共享库命中",
                   "cache": "本地缓存命中",
                   "placed": "已在目标位置"}.get(req_hit.source, "命中")
            out.append(Resolution(name=req["name"], subdir=req["subdir"],
                                  status=status, path=req_hit.path,
                                  size=req_hit.size, reason=why))
            continue
        if req.get("url") and can_download:
            out.append(Resolution(name=req["name"], subdir=req["subdir"],
                                  status="download", reason="需从 url 下载",
                                  extra={"url": req["url"]}))
            continue
        if not can_link and not can_download:
            reason = "该云没有共享权重库，也没有外网 —— 无法取得权重"
        elif not can_download:
            reason = "共享库里没有，且该云无外网无法下载"
        else:
            reason = "共享库里没有，模型卡也没给下载 url"
        out.append(Resolution(name=req["name"], subdir=req["subdir"],
                              status="missing", reason=reason))
    return out


def _find_in(req: dict, available: dict[str, AssetHit]) -> AssetHit | None:
    """在已知文件里按 match 规则找一个等价的。

    优先级：完全同名 > match 规则命中。都不中就 None（不猜）。
    """
    by_base = available.get(req["basename"])
    if by_base:
        return by_base
    for name, hit in available.items():
        if Path(name).name == req["basename"]:
            return hit
    if req.get("match"):
        for name, hit in available.items():
            if match_name(Path(name).name, req["match"]):
                return hit
    return None


def summarize(res: list[Resolution]) -> dict:
    """解析结果统计（给人看的一句话 + 机器判断）。"""
    c: dict[str, int] = {}
    for r in res:
        c[r.status] = c.get(r.status, 0) + 1
    missing = [r for r in res if r.status == "missing"]
    return {
        "total": len(res),
        "counts": c,
        "ok": not missing,
        "blocked_by": [f"{r.name}: {r.reason}" for r in missing],
        "needs_download": [r.name for r in res if r.status == "download"],
        "needs_link": [r.name for r in res if r.status == "linkable"],
    }


# ------------------------------------------------------------------ 五步：2/3 校验 + 落位
def parse_find_output(out: str) -> dict[str, AssetHit]:
    """解析 `find ... -printf '%f\\t%p\\t%s\\n'` 的输出。

    与 tools/p20/assets.py 的 parse_find_output 兼容（它也吃两列），
    但这里多一列大小，用来做磁盘水位判断。
    """
    found: dict[str, AssetHit] = {}
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, path = parts[0].strip(), parts[1].strip()
        size = 0
        if len(parts) > 2:
            try:
                size = int(parts[2])
            except ValueError:
                size = 0
        if not name or not path.startswith("/"):
            continue
        if name in found:
            continue
        found[name] = AssetHit(name=name, path=path, size=size, source="mount",
                               evictable=False)
    return found


def build_find_command(names: list[str], roots=DEFAULT_SEARCH_ROOTS,
                       maxdepth: int = 7) -> str:
    """一次 find 把所有候选名找齐（带大小）。

    只搜文件不搜目录 —— 目录名也会被 -name 命中（如 ltx-2.3-22b-dev-fp8 目录），
    那种命中会让"找到了"变成假阳性。
    """
    uniq = sorted({Path(n).name for n in names if n})
    if not uniq:
        return "true"
    expr = " -o ".join(f"-name {shlex.quote(n)}" for n in uniq)
    roots_s = " ".join(shlex.quote(r) for r in roots)
    return (f"find {roots_s} -maxdepth {maxdepth} -type f \\( {expr} \\) "
            f"-printf '%f\\t%p\\t%s\\n' 2>/dev/null | head -500")


def sha256_of(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_file(path: Path, *, expect_size: int = 0, expect_sha256: str = "") -> tuple[bool, str]:
    """校验一个下载完的文件。返回 (ok, 原因)。

    先查大小再算哈希：大小不对时算哈希是白花时间（40GB 权重算一次要好几分钟）。
    """
    if not path.is_file():
        return False, "文件不存在"
    size = path.stat().st_size
    if size <= 0:
        return False, "文件为空"
    if expect_size and size != expect_size:
        return False, f"大小不符：期望 {expect_size}，实际 {size}"
    if expect_sha256:
        got = sha256_of(path)
        if got.lower() != expect_sha256.lower():
            return False, f"sha256 不符：期望 {expect_sha256[:12]}…，实际 {got[:12]}…"
    return True, ""


def place_script(resolutions: list[Resolution], *, comfy_models: str = "/root/ComfyUI/models",
                 cache_root: str = DEFAULT_CACHE_ROOT) -> str:
    """生成「落位」遥控脚本：把共享库里的文件软链进 ComfyUI/models/<类型>/。

    download 状态的不在这里处理（要走传输，见 download_targets），
    因为下载要进度、要断点续传，塞进一个 shell 里没法看进度。
    """
    lines = ["set -u"]
    for r in resolutions:
        if r.status not in ("hit", "linkable") or not r.path:
            continue
        link = f"{comfy_models}/{r.subdir}/{r.name}"
        lines.append(f"mkdir -p {shlex.quote(str(Path(link).parent))}")
        lines.append(f"ln -sf {shlex.quote(r.path)} {shlex.quote(link)}")
    lines.append("echo ASSET_PLACE_DONE")
    return "\n".join(lines)


def download_targets(resolutions: list[Resolution],
                     cache_root: str = DEFAULT_CACHE_ROOT) -> list[dict]:
    """需要下载的文件清单（含目标路径与临时文件名）。"""
    out = []
    for r in resolutions:
        if r.status != "download":
            continue
        dst = Path(cache_root) / r.subdir / r.name
        out.append({"name": r.name, "url": (r.extra or {}).get("url", ""),
                    "dest": str(dst),
                    # .part 临时名：下载中断不会污染缓存（半截文件不会被当成成品）
                    "tmp": str(dst) + ".part"})
    return out


# ------------------------------------------------------------------ 五步：5 淘汰
def evict_plan(entries: dict, *, usage_bytes: int, limit_bytes: int,
               min_free_bytes: int = 0) -> dict:
    """算「要删谁」：LRU + 只删可删项 + 达到目标水位就停。

    规则（顺序即优先级）：
      1. `evictable=False`（共享挂载/平台库）永不入选 —— 删的是别人的数据
      2. 按 last_used 升序（最久没用的先删）
      3. 删到 `usage_bytes <= limit_bytes - min_free_bytes` 就停

    返回 {freed, files, after, reached}。`reached=False` 表示删光可删项也不够，
    这时上层必须报错（磁盘要爆了），不能静默继续。
    """
    need = max(0, usage_bytes - max(0, limit_bytes - min_free_bytes))
    if need <= 0:
        return {"freed": 0, "files": [], "after": usage_bytes, "reached": True,
                "need": 0}
    cand = [(name, e) for name, e in entries.items()
            if e.get("evictable") and (e.get("size") or 0) > 0]
    cand.sort(key=lambda kv: float(kv[1].get("last_used") or 0))
    freed, files = 0, []
    for name, e in cand:
        if freed >= need:
            break
        size = int(e.get("size") or 0)
        freed += size
        files.append({"name": name, "path": e.get("path", ""), "size": size,
                      "last_used": e.get("last_used")})
    return {"freed": freed, "files": files, "after": usage_bytes - freed,
            "reached": freed >= need, "need": need}


def prune_index(entries: dict, paths: list[str]) -> int:
    """把被删掉的文件从索引里摘掉，返回摘掉条数。"""
    gone = set(paths)
    drop = [n for n, e in entries.items() if e.get("path") in gone]
    for n in drop:
        entries.pop(n, None)
    return len(drop)


# ------------------------------------------------------------------ 便捷入口
def scan_local(roots=DEFAULT_SEARCH_ROOTS, *, maxdepth: int = 7) -> dict[str, AssetHit]:
    """在**本机**扫一遍权重库（部署机/实例上跑）。

    只扫纯本地路径，不追软链（`-type f` 天然排除软链），
    因为追软链会把 ComfyUI/models 里的链也算成"库里有"，
    导致"库里有"和"链接好了"两件事混为一谈。
    """
    names: list[str] = []
    for r in roots:
        p = Path(r)
        if not p.exists():
            continue
        try:
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in (
                        ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"):
                    names.append(f.name)
                    if len(names) > 20000:
                        break
        except OSError:
            continue
    # 用同一套 find 命令的解析器，保证两条路径得到同样结构
    hits: dict[str, AssetHit] = {}
    for r in roots:
        p = Path(r)
        if not p.exists():
            continue
        for f in p.rglob("*"):
            try:
                if not f.is_file() or f.suffix.lower() not in (
                        ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"):
                    continue
                st = f.stat()
            except OSError:
                continue
            hits.setdefault(f.name, AssetHit(
                name=f.name, path=str(f), size=st.st_size, source="mount",
                evictable=False, last_used=0.0))
    return hits


def load_compat_links(path: Path | str | None = None) -> dict[str, AssetHit]:
    """从 `tools/p1/compat_links.txt` 读【已验证过的真实路径映射】。

    这个文件的格式是 `dir<TAB>name<TAB>target`，32 行，每行都是**曾经
    真机验证过能加载的**（instance_setup.sh 直接拿它建软链）。

    为什么它比 `find /model` 更有价值：

      `find` 只知道「这个名字存在于某处」，不知道「它是不是这条工作流要的那个」。
      compat_links 记录的是 `dir/name → 实际文件`，即**名字映射已经确认过**，
      包括那些 `qwen_std_vae.safetensors → qwen_image_vae.safetensors`
      这种改了名的（缺口清单 §3 说的"名字不对占大头"）。

    所以这里把它当作 `source="verified"` 的强证据，与 mount/cache 区分开。
    """
    p = Path(path) if path else ROOT / "tools" / "p1" / "compat_links.txt"
    out: dict[str, AssetHit] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        _d, name, target = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name or not target:
            continue
        # 索引按 **文件名** 建，因为 resolve() 的匹配也是按文件名做的
        out[Path(name).name] = AssetHit(
            name=Path(name).name, path=target, size=0, source="verified",
            evictable=False, last_used=0.0,
            verified_at="compat_links.txt")
    return out


def index_from_hits(hits: dict[str, AssetHit], *, path: Path | None = None,
                    now: float | None = None) -> dict:
    """把一次扫描结果写进索引（保留已有 last_used）。"""
    d = load_index(path)
    for hit in hits.values():
        record(d["entries"], hit, now=now)
    save_index(d, path)
    return d


def touch(name: str, *, path: Path | None = None, now: float | None = None) -> bool:
    """标记某资产刚被用过（LRU 的依据）。没索引项返回 False。"""
    d = load_index(path)
    e = d["entries"].get(name)
    if not e:
        return False
    e["last_used"] = now if now is not None else time.time()
    save_index(d, path)
    return True


def evidence_coverage(models: list[dict], available: dict[str, AssetHit]) -> dict:
    """证据覆盖率：现有证据能确认多少比例的权重「存在」。

    为什么必须算这个：仓库内的证据是两份**抽样**
    （compat_links.txt 30 行 + missing-weights.txt 95 行），而 /model 是 40TB 库。
    拿它们判「缺失」会把明明能跑的工作流报成不可执行 —— 反过来，
    若没人算覆盖率，就会把「查过的是这些」误读成「就这些」。

    返回 {required, known, unknown, coverage, sources}。
    `unknown` 的那些：既不能确认存在，也不能确认缺失，只能标『待盘点』。
    """
    required: set[str] = set()
    for m in models:
        for req in model_requirements(m):
            required.add(req["basename"])
    known = {n for n in required if n in available}
    by_src: dict[str, int] = {}
    for n in known:
        src = available[n].source
        by_src[src] = by_src.get(src, 0) + 1
    return {
        "required": len(required),
        "known": len(known),
        "unknown": len(required) - len(known),
        "coverage": round(len(known) / len(required), 3) if required else 1.0,
        "sources": by_src,
    }


def disk_usage(entries: dict) -> int:
    """索引里记录的资产总量（字节）。只统计可删项 —— 共享库不占我们的盘。"""
    return sum(int(e.get("size") or 0) for e in entries.values() if e.get("evictable"))
