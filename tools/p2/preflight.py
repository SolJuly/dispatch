#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流预检：在**花任何钱之前**回答「这条跑得起来吗，跑不起来缺什么」。

## 现状的痛

`registry` 里 24 条 experimental（44%），UI 上标着「尚未开发」。
用户点了会被 `resolve()` 拒（409），处理是对的 —— 但**没人能说清为什么**。

现在能查到的信息是 `pack-audit-result.json`：一份 2026-11-03 的静态快照，
内容是 38 条 pack 工作流的 missing_class 清单。它有三个问题：

  1. **过期**：之后刷了镜像、补了软链、删了 6 条旧模板，清单没跟着更新
  2. **只有节点**：不知道权重在不在（"缺权重大头，名字不对占大头" 见缺口清单 §3）
  3. **不可复算**：是跑在真机上的一次性输出，没有可重跑的入口

## 这一层做什么

三源交叉，纯静态，零花费：

    工作流卡（registry/workflows/*.json）
        → 图里用到哪些 class_type、绑定了哪些参数、声明了哪个模型

    模型卡（registry/models/*.json）
        → 需要哪些权重文件、每个文件的 loader/input/匹配规则

    环境探针（object_info 快照 + 权重清单）
        → 机器上真的有哪些节点类、哪些文件

产出每条工作流的 `readiness`：

    ready       三项齐（节点在、权重在、绑定对得上）
    no_node     缺节点类 → 必须换镜像（升 ComfyUI 或补 custom_node）
    no_weight   缺权重文件 → 补软链或下载
    bad_graph   结构问题（占位符没覆盖、绑定指向不存在的节点）→ 改图/改配置

## 为什么不一上手就跑真机

缺口清单 §4 算过：34 条里大部分缺节点/权重，跑一次只会得到一条
FAILED_PROVISION，还花开机费（单条 ¥0.13+）。静态预检是免费的，
应该先用它把 24 条砍到「真正只差一次真机验证」的那几条。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REG_DIR = ROOT / "registry"

# 与 tools/p20/assets.py 的 LOADER_DIRS 保持一致（那边给远端脚本用）
LOADER_DIRS = {
    "UNETLoader": "diffusion_models",
    "UnetLoaderGGUF": "diffusion_models",
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


@dataclass
class Readiness:
    """一条工作流 × 一个模型的可用性结论。"""

    workflow: str
    model: str = ""
    status: str = "ready"        # ready / no_node / no_weight / bad_graph / no_model
    missing_nodes: list[str] = field(default_factory=list)
    missing_weights: list[str] = field(default_factory=list)
    # 抽样清单下「未确认存在」的权重：不等于缺失，需要人去核。
    # 单独一个字段而不是塞进 missing_weights，因为两者结论强度不同：
    # missing = 一定跑不起来；unverified = 可能跑得起来。
    unverified_weights: list[str] = field(default_factory=list)
    graph_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def reason(self) -> str:
        if self.ok:
            base = "可执行"
            if self.unverified_weights:
                base += f"（{len(self.unverified_weights)} 项权重未确认存在）"
            return base
        parts = []
        if self.missing_nodes:
            parts.append(f"缺 {len(self.missing_nodes)} 个节点类（{', '.join(self.missing_nodes[:3])}"
                         f"{'…' if len(self.missing_nodes) > 3 else ''}）")
        if self.missing_weights:
            parts.append(f"缺 {len(self.missing_weights)} 个权重（{', '.join(self.missing_weights[:2])}"
                         f"{'…' if len(self.missing_weights) > 2 else ''}）")
        if self.graph_errors:
            parts.append(f"图问题：{self.graph_errors[0]}")
        if self.status == "no_model":
            parts.append("模型卡不存在")
        return "；".join(parts) or "不可执行"

    def fix_hint(self) -> str:
        """给出「怎么修」的一句话，避免用户看到结论却没下一步。"""
        if self.ok:
            return ""
        if self.status == "no_model":
            return "在 registry/models/ 补模型卡"
        if self.missing_nodes:
            return "需要换镜像：升 ComfyUI 版本或补对应 custom_node"
        if self.missing_weights:
            return "补权重：有共享库就加软链，无共享库就走下载"
        if self.graph_errors:
            return "改图或改配置：占位符/绑定/输出节点对不上"
        return ""

    def as_dict(self) -> dict:
        return {"workflow": self.workflow, "model": self.model, "status": self.status,
                "ok": self.ok, "reason": self.reason(), "fix_hint": self.fix_hint(),
                "missing_nodes": self.missing_nodes,
                "missing_weights": self.missing_weights,
                "unverified_weights": self.unverified_weights,
                "graph_errors": self.graph_errors, "notes": self.notes}


# ------------------------------------------------------------------ 探针
@dataclass
class Probe:
    """机器侧已知事实。

    **两个维度各有「已知 / 未知」与「完整 / 抽样」两档**，这不是过度设计：

    - `known=False`：没给这份证据 → 该维度整个跳过
    - `known=True, complete=False`：给了但是**抽样**（如缺口调查的输出）
      → 只能用来**确认存在**，不能用来**确认缺失**

    为什么非要这个区分（2026-11-09 实测踩到）：
    `reports/p2/p21/evidence/missing-weights.txt` 只有 95 行，是当初
    「查了几个常见目录没找到」的调查记录，**不是**完整盘点。把它当成
    `weights_known` 用的结果是 31 条 verified 工作流全部被报成 `no_weight` ——
    而它们明明昨天还在真机上跑通过。

    这就是审计里那句「查询成功但没数据被当成一切正常」的镜像错误：
    **有数据但不完整被当成了完整**。抽样清单只能证明"在"，不能证明"不在"。
    """

    node_classes: set[str] = field(default_factory=set)
    weight_files: set[str] = field(default_factory=set)
    node_classes_known: bool = False    # False = 没给节点快照，跳过节点检查
    node_classes_complete: bool = True
    weights_known: bool = False         # False = 没给权重清单，跳过权重检查
    weights_complete: bool = True
    # 已确认的名字映射（name → 真实路径）；命中的权重不查 weights 也不报缺
    compat_links: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_object_info(cls, oi: dict) -> "Probe":
        """object_info 是一台机器上完整节点类的枚举，天然完整。"""
        return cls(node_classes=set(oi.keys()), node_classes_known=bool(oi),
                   node_classes_complete=bool(oi))

    @classmethod
    def from_files(cls, paths, *, complete: bool = True) -> "Probe":
        return cls(weight_files={Path(p).name for p in paths}, weights_known=bool(paths),
                   weights_complete=complete)

    @classmethod
    def load(cls, *, object_info: Path | str | None = None,
             weights: Path | str | None = None,
             weights_complete: bool = True) -> "Probe":
        """从证据文件加载。

        object_info       ComfyUI /object_info 的快照 JSON
        weights            一行一个路径的文本文件（`find /model -type f` 的输出）
        weights_complete   False = 这份清单是抽样，只用于确认存在
        """
        p = cls()
        if object_info:
            d = json.loads(Path(object_info).read_text(encoding="utf-8"))
            d = d.get("object_info") or d
            p.node_classes = set(d.keys())
            p.node_classes_known = bool(p.node_classes)
            p.node_classes_complete = p.node_classes_known
        if weights:
            lines = Path(weights).read_text(encoding="utf-8", errors="ignore").splitlines()
            p.weight_files = {Path(x.strip()).name for x in lines
                              if x.strip().startswith("/")}
            p.weights_known = bool(p.weight_files)
            p.weights_complete = weights_complete
        return p


# ------------------------------------------------------------------ 预检
def graph_classes(graph: dict) -> set[str]:
    """图里用到的所有 class_type。"""
    out = set()
    for node in (graph or {}).values():
        if isinstance(node, dict) and node.get("class_type"):
            out.add(str(node["class_type"]))
    return out


def _load_graph(rel: str) -> dict:
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / rel
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def check_workflow(wf: dict, model: dict | None, probe: Probe) -> Readiness:
    """单条工作流 × 模型的预检。

    `model=None` 表示模型卡不存在（registry 里引用了没导入的模型）。
    `probe` 里 `*_known=False` 的维度会被跳过 —— 宁可说"没检查"，也不要
    在缺数据时谎报"通过"。这正是上一轮审计最贵的错误类型。
    """
    key = str(wf.get("key") or "?")
    mk = str((wf.get("model") or {}).get("key") or "")
    r = Readiness(workflow=key, model=mk)

    if model is None:
        r.status = "no_model"
        return r

    graph = _load_graph(str(wf.get("graph") or ""))

    # ---- 1. 节点类
    if probe.node_classes_known:
        used = graph_classes(graph)
        # 只检查「非官方」的 class 是否缺失？不行 —— 官方 class 也可能因
        # ComfyUI 版本不同而缺失（QwenImage21Cache 就是官方但 0.36 没有）。
        # 所以一律按快照判定：快照里没有 = 这台机器上跑不了。
        r.missing_nodes = sorted(c for c in used if c not in probe.node_classes)
    else:
        r.notes.append("未提供节点快照，跳过节点检查")

    # ---- 2. 权重
    if probe.weights_known:
        for _role, f in (model.get("files") or {}).items():
            f = f or {}
            name = str(f.get("name") or "")
            if not name:
                continue
            base = Path(name).name
            # ① 已确认的名字映射（compat_links）：最强证据，直接放过。
            #    它证明「这个 ComfyUI 名字在实例上能用」，真实文件叫别的名字无所谓。
            if base in probe.compat_links:
                continue
            # ② 在权重清单里同名
            if base in probe.weight_files:
                continue
            # ③ 按模型卡的 match 规则再给一次机会（平台库文件名常带额外后缀）
            rule = f.get("match") or {}
            if rule and any(_match(x, rule) for x in probe.weight_files):
                continue
            r.missing_weights.append(name)
        if not probe.weights_complete:
            # 抽样清单：缺的可能是"清单里没采到"而不是"机器上没有"。
            # 这类不能升级成 no_weight，只能标注「疑似缺」，让人去核。
            if r.missing_weights:
                r.notes.append(
                    f"权重清单是抽样（{len(probe.weight_files)} 个文件），"
                    f"{len(r.missing_weights)} 项未命中只代表『未确认存在』，不代表缺失")
                r.unverified_weights = list(r.missing_weights)
                r.missing_weights = []
    else:
        r.notes.append("未提供权重清单，跳过权重检查")

    # ---- 3. 图结构（占位符覆盖 / 绑定存在性）
    r.graph_errors = _graph_errors(wf, graph)

    # ---- 结论
    if not graph:
        r.status = "bad_graph"
        r.graph_errors.insert(0, f"图文件缺失或不可解析：{wf.get('graph')}")
    elif r.graph_errors:
        r.status = "bad_graph"
    elif r.missing_nodes:
        r.status = "no_node"
    elif r.missing_weights:
        r.status = "no_weight"
    else:
        r.status = "ready"
    return r


def _norm(s: str) -> str:
    """文件名归一：小写 + 连字符折算成下划线。

    为什么需要（2026-11-09 实测）：同一个文件在两处写法不同，纯属排版差异：

        模型卡写的   ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_...
        库里真实叫   ltx-2.3-22b-distilled-1.1_lora-dynamic_fro09_...
        模型卡写的   ltx-2.3-id-lora-talkvid-3k
        库里真实叫   LTX-2.3-ID-LoRA-TalkVid-3K

    按原名比会判成"缺失"，于是 2 条工作流白排一轮真机验证，
    而它们要的东西明明就在库里。

    **只归一大小写和 `-`/`_`，不做别的模糊。** 版本号（111 vs 105）、
    精度（fp8 vs int8）、模型规模（7b vs 3b）都必须保持可区分 ——
    把 `rank_105` 匹配到 `rank_111` 会让作业静默用错权重，
    那种错误在成片里根本看不出来。
    """
    return str(s or "").strip().lower().replace("-", "_")


def _match(name: str, rule: dict) -> bool:
    """按模型卡的 match 规则判断候选文件名是否等价。

    规整后再比（见 _norm），避免大小写/连字符差异造成假缺失。
    """
    import re
    if not rule:
        return False
    cand, nc = name, _norm(name)
    if "equals" in rule:
        want = str(rule["equals"])
        return cand == want or nc == _norm(want)
    if "contains" in rule:
        want = str(rule["contains"])
        return want in cand or _norm(want) in nc
    if "regex" in rule:
        for t in (cand, nc):
            try:
                if re.search(str(rule["regex"]), t):
                    return True
            except re.error:
                return False
    return False


def _graph_errors(wf: dict, graph: dict) -> list[str]:
    """图层面的结构问题：绑定指向不存在的节点、占位符没被覆盖、输出节点错。

    必须用 `check_graph=True` —— 这是本函数存在的全部意义。
    一开始写成 False 的结果是：54 条全部报 ready，包括那 24 条
    连占位符都没覆盖的 pack 工作流。**校验器自己骗自己**比没有校验器更糟。
    """
    if not graph:
        return []
    from tools.p20 import config_schema as cs
    try:
        return cs.validate_workflow(wf, root=ROOT, check_graph=True)
    except Exception as e:                       # noqa: BLE001
        return [f"配置校验异常：{type(e).__name__}: {e}"]


def load_compat_links(path: Path | str | None = None) -> dict[str, str]:
    """读 `tools/p1/compat_links.txt`：`dir<TAB>卡里写的名字<TAB>真实路径`。

    为什么必须读这个文件（2026-11-09 实测）：预检只看 `weights-full.txt`
    里 **/model 下的真实文件名**，而 `compat_links.txt` 记录的是
    「ComfyUI 里该用什么名字 → 实际指向哪个文件」的**已确认映射**。

    两者的名字经常不同，而且是**故意不同**的：

        qwen25vl7b_std.safetensors  →  qwen2.5vl-7b-bf16.safetensors
        qwen_std_vae.safetensors    →  qwen_image_vae.safetensors

    原因见缺口清单 §3：「平台镜像里的文件名五花八门」。模型卡写的是
    **ComfyUI 侧的名字**（工作流图里引用的就是它），真实文件叫别的名字
    完全正常 —— 有软链就能跑。

    不读这个文件的后果：`qwen_image_t2i` 等 4 条 **verified（真机跑通过）**
    被预检报成 `no_weight`。判据漏了一条证据来源，就会把已验证说成不可用。
    """
    p = Path(path) if path else ROOT / "tools" / "p1" / "compat_links.txt"
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
        if name:
            out[Path(name).name] = target
    return out


def check_all(*, probe: Probe | None = None, only: str = "",
              compat_links: dict[str, str] | None = None) -> list[Readiness]:
    """全量预检。probe=None 时只做图结构检查（不需要机器数据）。

    compat_links 非空时，它的键会被当成「已确认存在」的名字。
    """
    probe = probe or Probe()
    out: list[Readiness] = []

    # compat_links 是「名字 → 真实路径」的已确认映射，属于**强证据**：
    # 这些名字在实例上确实能用（instance_setup.sh 就是照它建软链的）。
    # 直接并进 weight_files，让权重检查认它们。
    if compat_links and probe.weights_known:
        probe = Probe(
            node_classes=probe.node_classes,
            node_classes_known=probe.node_classes_known,
            node_classes_complete=probe.node_classes_complete,
            weight_files=probe.weight_files | set(compat_links),
            weights_known=True,
            weights_complete=probe.weights_complete,
            compat_links=dict(compat_links),
        )

    from tools.p20 import registry as p20registry
    try:
        wfs = p20registry.list_workflows()
    except Exception:
        wfs = []
    if not wfs:                       # 库没导入时直接读 JSON，保证工具可用
        wfs = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted((REG_DIR / "workflows").glob("*.json"))]

    for wf in wfs:
        if only and only not in str(wf.get("key")):
            continue
        mk = str((wf.get("model") or {}).get("key") or "")
        model = None
        if mk:
            try:
                model = p20registry.get_model(mk)
            except Exception:
                p = REG_DIR / "models" / f"{mk}.json"
                model = (json.loads(p.read_text(encoding="utf-8"))
                         if p.is_file() else None)
        out.append(check_workflow(wf, model, probe))
    return out


def drift_report(rows: list[Readiness]) -> list[dict]:
    """交叉校验：registry 说 verified，但当前环境预检说跑不起来 → 环境漂移。

    这是这一层最有价值的输出。它抓的是「昨天还能跑，今天跑不了」：

        .compshare/asset_index.json 里的文件被清过
        镜像换了（v3 → v4）导致节点集变化
        有人手动改过 /model 的软链
        平台更新了共享库

    这类问题不会自己暴露 —— 用户点了才报错，而那时已经开了机（花了钱）。
    预检能在提交之前就发现，零成本。
    """
    from tools.p20 import registry as R
    out = []
    for r in rows:
        if r.ok:
            continue
        try:
            compat = R.compat_for(r.workflow, r.model)
        except Exception:
            continue
        if (compat or {}).get("status") not in ("verified", "equivalent"):
            continue
        out.append({
            "workflow": r.workflow, "model": r.model,
            "registry_says": compat.get("status"),
            "env_says": r.status,
            "reason": r.reason(),
            "fix_hint": r.fix_hint(),
            "basis": compat.get("basis") or "",
        })
    return out


def summarize(rows: list[Readiness]) -> dict:
    c: dict[str, int] = {}
    for r in rows:
        c[r.status] = c.get(r.status, 0) + 1
    return {
        "total": len(rows),
        "counts": c,
        "ready": c.get("ready", 0),
        "blocked": len(rows) - c.get("ready", 0),
        "unverified_weights": sum(1 for r in rows if r.unverified_weights),
        "by_cause": {
            "缺节点（要换镜像）": c.get("no_node", 0),
            "缺权重（要补软链/下载）": c.get("no_weight", 0),
            "图/配置问题（要改配置）": c.get("bad_graph", 0),
            "模型卡缺失": c.get("no_model", 0),
        },
    }


def format_table(rows: list[Readiness], *, ready_too: bool = False) -> str:
    lines = []
    for r in sorted(rows, key=lambda x: (x.status != "ready", x.workflow)):
        if r.ok and not ready_too:
            continue
        lines.append(f"{r.status:10s} {r.workflow:52s} {r.reason()}")
    return "\n".join(lines)
