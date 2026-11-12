#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例槽位表：让「一台实例」成为长生命的一等公民，而不是作业的附属品。

## 要解决什么

现在的执行模型是 **一条作业 = 一个实例的完整生命周期**：

    run.py 九步：provision（建机）→ … → release（销毁）

`step_release` 是必走的一步，所以**作业结束 = 实例销毁**。同一个工作流提交 5 份，
就是建 5 次机、装 5 次模型。

实测的装载代价（`reports/p2/p21/06-M1-连测报告.md`）：

| 模型 | 首条（含装载） | 稳态 | 装载开销 |
|---|---|---|---|
| SDXL | 26.3s | 4.7–5.1s | **~21s** |
| Qwen | 77.1s | 21.5–25.4s | **~53s** |

5 份同工作流串行复用一台机器，只付一次装载。

## 分组键怎么定（方案 C）

    group = model_key
    条件  = 目标工作流的 {节点类, 资产文件} ⊆ 该实例的「已验证集」

**为什么不用裸 `model_key`**：实测同模型的不同工作流节点集能差 6–9 类。
比如 `sdxl_juggernaut` 挂 5 个工作流，节点并集 15 类，单个工作流只有 6–9 类：

    sdxl_t2i          6 类
    sdxl_t2i_lora     7 类  ⚠ LoraLoader
    sdxl_img2img      8 类  ⚠ ImageScale
    sdxl_inpaint      9 类  ⚠ VAEEncodeForInpaint, ImageToMask, ImageBlur
    detail_polish     9 类  ⚠ UpscaleModelLoader, ImageUpscaleWithModel

其中绝大多数"独占节点"是 ComfyUI **内置**的（ImageScale / ImageBlur /
LoadImage / SaveVideo …），不需要额外资产。**真正要额外资产的只有两个**：

    UpscaleModelLoader  → 要 4x-UltraSharp.pth 之类的放大模型
    LoraLoader          → 要 LoRA 权重文件

而且这两个用的是**占位符**（`{{UPSCALE_MODEL}}` / `{{LORA}}`），
具体指向哪个文件要等参数填进来才知道 —— 所以检查必须分两个时机：

    排队时    查节点类在不在「已验证集」（不用渲染）
    渲染后    查参数指到的具体文件在不在（必须等参数）

**为什么不用 `workflow_key` 分组**：太碎。`sdxl_juggernaut` 那 5 个工作流
**全部共用同一份** `juggernautXL_v8Rundiffusion_V8.safetensors`，
分成 5 台机纯浪费。

## 「已验证集」为什么不直接用 object_info

`object_info` 只能证明 ComfyUI **认识**这个节点类，不能证明它的依赖就绪。
比如 `UpscaleModelLoader` 认识，但 `4x-UltraSharp.pth` 可能没软链好。

所以「已验证」= **这台机器上真跑成功过的工作流**，用它们的节点并集/资产并集。
冷启动时（新实例什么都没跑过）用 `object_info` 当"候选集"，跑成功一条后升级。

## 产物会冲突吗（已核实）

不会。`tools/p20/render.py:149` 把 `job["job_id"]` 直接当 `filename_prefix`：

    graph, meta = render(wf, model, res["params"], job["job_id"], ...)

实测产物名形如 `00-primary-p1-20261108T1352-3d1616_00001_.mp4` ——
每条作业的输出天然带自己的 id 前缀，同一实例上多条作业不会互相覆盖。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLOTS_JSON = ROOT / "reports" / "p2" / "instance_slots.json"
SCHEMA = "p2.instance_slots/v1"


def _now() -> float:
    return time.time()


def _load() -> dict:
    if SLOTS_JSON.exists():
        try:
            d = json.loads(SLOTS_JSON.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("slots"), dict):
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": SCHEMA, "slots": {}}


def _save(d: dict) -> None:
    SLOTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = SLOTS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SLOTS_JSON)


@dataclass
class Slot:
    """一台可复用的实例。

    字段刻意保持"能重建"：所有值都来自 job.json / registry，
    槽位表本身丢了也能从历史作业重新算出来（`rebuild_from_jobs()`）。
    """

    uid: str
    model_key: str
    gpu_type: str = "4090"
    state: str = "running"                 # running / draining
    started_at: float = 0.0
    idle_since: float = 0.0
    workflows_ok: list[str] = field(default_factory=list)     # 跑成功过的工作流
    verified_nodes: list[str] = field(default_factory=list)   # 上述工作流的节点并集
    verified_assets: list[str] = field(default_factory=list)  # 上述工作流的资产并集
    busy_jobs: list[str] = field(default_factory=list)        # 正在跑的作业
    last_used: float = 0.0

    # ---- 序列化
    def as_dict(self) -> dict:
        return {"uid": self.uid, "model_key": self.model_key, "gpu_type": self.gpu_type,
                "state": self.state, "started_at": self.started_at,
                "idle_since": self.idle_since, "workflows_ok": self.workflows_ok,
                "verified_nodes": self.verified_nodes,
                "verified_assets": self.verified_assets,
                "busy_jobs": self.busy_jobs, "last_used": self.last_used}

    @classmethod
    def from_dict(cls, d: dict) -> "Slot":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    # ---- 兼容性
    def accepts(self, need_nodes: set[str], need_assets: set[str],
                *, image_nodes: set[str] | None = None) -> tuple[bool, str]:
        """这个实例能不能接一个新工作流。

        **节点分两类，不能混为一谈**（2026-11-09 实测踩到）：

          · 镜像里没有   → 这个工作流根本跑不了，任何实例都不行
          · 这台上没跑过 → 只是没验证过，多半能跑（内置节点人人都有）

        如果拿"已验证集"去卡节点，`sdxl_img2img` 会因为缺
        `ImageScale` / `LoadImage` / `VAEEncode` 被拒 —— 而这些全是
        ComfyUI 内置节点，每台实例都有。结果是每个工作流都换机器，
        规则 B 直接失效。

        所以节点判定用 `image_nodes`（镜像能力），**不是** `verified_nodes`：

            节点 ⊆ image_nodes        → 能跑（不要求这台机器跑过）
            节点 ⊄ image_nodes        → 镜像缺节点，换机器也没用

        资产才用"已验证集"：`{{UPSCALE_MODEL}}` 这类占位符指到的具体文件，
        只有真跑过才知道这台机器上在不在。
        """
        nodes = set(need_nodes or ())
        if image_nodes:
            miss_img = sorted(nodes - set(image_nodes))
            if miss_img:
                return False, f"镜像缺节点类：{', '.join(miss_img[:3])}（换机器也没用）"
        else:
            # 拿不到镜像能力时退回已验证集（保守）
            miss_v = sorted(nodes - set(self.verified_nodes))
            if miss_v and self.verified_nodes:
                return True, f"节点未验证：{', '.join(miss_v[:3])}（放行，镜像能力未知）"

        # 资产：空已验证集 = 冷启动，放行（第一条的装载代价必须付）
        if not self.verified_assets:
            return True, "冷启动（首条按新机器处理）"
        miss_a = sorted(set(need_assets or ()) - set(self.verified_assets))
        if miss_a:
            # 模型卡声明的资产（每个工作流都有）不算"缺" —— 那是这台机器的底座
            return False, f"缺资产：{', '.join(miss_a[:3])}"
        return True, "已验证集覆盖"


# ---------------------------------------------------------------- CRUD
def all_slots() -> dict[str, Slot]:
    return {k: Slot.from_dict(v) for k, v in _load()["slots"].items()}


def get(uid: str) -> Slot | None:
    d = _load()["slots"].get(uid)
    return Slot.from_dict(d) if d else None


def upsert(slot: Slot) -> Slot:
    d = _load()
    d["slots"][slot.uid] = slot.as_dict()
    _save(d)
    return slot


def remove(uid: str) -> None:
    d = _load()
    if d["slots"].pop(uid, None) is not None:
        _save(d)


def touch(uid: str, *, job_id: str | None = None, busy: bool = False) -> None:
    """更新使用时间/占用状态。借用的实例在作业结束时调这个（不销毁）。"""
    s = get(uid)
    if not s:
        return
    s.last_used = _now()
    if busy:
        if job_id and job_id not in s.busy_jobs:
            s.busy_jobs.append(job_id)
        s.idle_since = 0.0
    else:
        if job_id and job_id in s.busy_jobs:
            s.busy_jobs.remove(job_id)
        if not s.busy_jobs:
            s.idle_since = _now()
    upsert(s)


def mark_workflow_ok(uid: str, workflow_key: str, *, nodes: set[str] | None = None,
                     assets: set[str] | None = None) -> None:
    """记一条"这台机器上跑成功过这个工作流"，并扩充已验证集。"""
    s = get(uid)
    if not s:
        return
    if workflow_key and workflow_key not in s.workflows_ok:
        s.workflows_ok.append(workflow_key)
    if nodes:
        s.verified_nodes = sorted(set(s.verified_nodes) | set(nodes))
    if assets:
        s.verified_assets = sorted(set(s.verified_assets) | set(assets))
    upsert(s)


# ---------------------------------------------------------------- 需求计算
def workflow_needs(workflow_key: str, model_key: str | None = None) -> tuple[set[str], set[str]]:
    """一个工作流需要哪些节点类、哪些资产文件名。

    资产分两类来源：
      ① 模型卡 `files` 里声明的（这个模型必有的权重）
      ② 图里的 loader 占位符（`{{UPSCALE_MODEL}}` / `{{LORA}}`）——
         这类拿不到具体文件，只记"需要这个 loader 节点"，资产由渲染后检查。
    """
    try:
        from tools.p20 import registry
    except Exception:                      # noqa: BLE001
        return set(), set()

    nodes: set[str] = set()
    assets: set[str] = set()
    try:
        wf = registry.get_workflow(workflow_key)
    except Exception:                      # noqa: BLE001
        return set(), set()

    g = wf.get("graph_obj") or {}
    if not g:
        gp = wf.get("graph")
        if gp:
            p = Path(gp) if Path(gp).is_absolute() else ROOT / str(gp)
            try:
                g = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                g = {}
    for n in g.values():
        ct = n.get("class_type")
        if ct:
            nodes.add(str(ct))

    mk = model_key or ((wf.get("model") or {}).get("key") or "")
    if mk:
        try:
            model = registry.get_model(mk)
            for f in (model.get("files") or {}).values():
                name = (f or {}).get("name")
                if name:
                    assets.add(str(name))
        except Exception:                  # noqa: BLE001
            pass
    return nodes, assets


def image_node_classes() -> set[str]:
    """当前镜像里有哪些节点类（来自 object_info 快照）。

    用快照而不是真机查：排队决策要快（毫秒级），而且建机前还没有机器可查。
    快照过期会退化成"保守放行"，不会误拒。
    """
    for p in (ROOT / "reports/p2/p21/evidence/objectinfo-v4.json",):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            inner = d.get("object_info") if isinstance(d.get("object_info"), dict) else d
            if inner:
                return set(inner.keys())
    return set()


def find_compatible(model_key: str, workflow_key: str, *, nodes: set[str] | None = None,
                    assets: set[str] | None = None,
                    min_vram: int = 0) -> tuple[Slot | None, str]:
    """找一台可以接这个工作流的现成实例。返回 (slot|None, 理由)。

    挑选顺序：同 model_key → 兼容 → 显存够 → 当前空闲作业数最少 → 最近用过。

    `min_vram`（2026-11-12 加）：显存下限，来自 `oom_escalate.floor_vram()`。
    这条**必须**在复用路径上生效，否则升卡是个笑话：

        24G 卡 OOM → 决定升到 48G → 释放 24G 实例 → 下一轮建机前先看槽位
        → 槽位里有另一台同模型的 24G 实例正空闲 → 复用它 → 再次 OOM

    复用是为了省装载时间（SDXL ~21s / Qwen ~53s），但省 53 秒换一轮失败
    不划算。所以显存不够的槽位直接不参与复用 —— 宁可新建一台 48G。
    """
    if nodes is None or assets is None:
        n2, a2 = workflow_needs(workflow_key, model_key)
        nodes = nodes if nodes is not None else n2
        assets = assets if assets is not None else a2

    cands = [s for s in all_slots().values()
             if s.model_key == model_key and s.state == "running"]
    if not cands:
        return None, f"没有 {model_key} 的可用实例"
    if min_vram:
        from tools.p2 import gpu_ladder
        big = [s for s in cands if gpu_ladder.vram_of(s.gpu_type) >= min_vram]
        if not big:
            got = ", ".join(f"{s.uid}({s.gpu_type} "
                            f"{gpu_ladder.vram_of(s.gpu_type) or '?'}G)" for s in cands[:3])
            return None, (f"{len(cands)} 台 {model_key} 实例显存都不足 {min_vram}G"
                          f"（{got}）—— 不复用，新建大卡")
        cands = big
    image_nodes = image_node_classes()
    ok_any = []
    why = []
    for s in cands:
        ok, reason = s.accepts(set(nodes or ()), set(assets or ()),
                               image_nodes=image_nodes)
        if ok:
            ok_any.append(s)
        else:
            why.append(f"{s.uid}: {reason}")
    if not ok_any:
        return None, "；".join(why[:3]) or "无兼容实例"
    ok_any.sort(key=lambda s: (len(s.busy_jobs), -s.last_used))
    return ok_any[0], "复用"


# ---------------------------------------------------------------- 自愈
def rebuild_from_jobs(jobs_dir: Path | None = None) -> int:
    """从历史作业重建槽位表（槽位表丢了/被删了都能救回来）。

    只收 DONE 的作业：它们的 `instance.uhost_id` + registry 元数据足以重建
    「哪台机器上跑成功过哪些工作流」。
    """
    root = Path(jobs_dir) if jobs_dir else ROOT / "reports" / "p1" / "jobs"
    if not root.is_dir():
        return 0
    seen: dict[str, Slot] = {}
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            j = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if j.get("state") != "DONE":
            continue
        reg = ((j.get("model") or {}).get("registry") or {})
        wk = reg.get("workflow_key")
        mk = reg.get("model_key") or ((j.get("model") or {}).get("key"))
        uid = ((j.get("instance") or {}) or {}).get("uhost_id")
        if not (wk and mk and uid):
            continue
        s = seen.get(uid) or Slot(uid=str(uid), model_key=str(mk))
        nodes, assets = workflow_needs(str(wk), str(mk))
        s.workflows_ok = sorted(set(s.workflows_ok) | {str(wk)})
        s.verified_nodes = sorted(set(s.verified_nodes) | nodes)
        s.verified_assets = sorted(set(s.verified_assets) | assets)
        seen[uid] = s
    d = _load()
    for uid, s in seen.items():
        d["slots"].setdefault(uid, s.as_dict())
    _save(d)
    return len(seen)
