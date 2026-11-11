#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流接口盘点：图里有多少个「用户可给」的输入，registry 暴露了几个。

## 为什么要这个

2026-11-11 你的要求：

> 检查现有的工作流，必须分析每一个工作流，他有多少个接口，我们就得要有多少个接口。

问题在于「接口」不是均匀的。一个工作流图里可能有二十几个可给值的输入，
但大部分是**模型文件名、采样器名、步数**这类不该让用户碰的；真正对用户
有意义的只有「我给它什么素材 / 什么文字」。所以这个工具不是简单地数节点，
而是把输入分成三类，分别对待：

    media    图片 / 视频 / 音频  ⚠ **必须**暴露 —— 少了就等于功能残缺
    text     提示词 / 反向提示词 / 文本  ⚠ 应该暴露
    model    权重文件名、采样器、步数、尺寸  ✗ 不该暴露（暴露了用户也选不对）

判据是**上游是不是加载器**：一个 `KSampler.steps` 是 widget（值写在图里），
而 `LoadImage.image` 的上游是文件。只有「文件/素材/文本」才值得做成接口。

## 三类输入怎么识别

① 加载器节点的**文件字段**（`LoadImage.image`、`LoadAudio.audio`、`LoadVideo.file`）

   看节点的 required 里哪个字段的类型是 COMBO（候选=文件名列表）
   或者字段名本身叫 image/audio/file/audio/video

② `COMFY_AUTOGROW_V3` 槽位（`ref_images.ref_image_0`、`ref_audios.ref_audio_0`）

   这类是「可选的多路参考」：`min`/`max` 写明上限，9 张图就是 9 个位置。
   它们在图里可能只连了 1-2 个，但**能力上限**是 max 个。

③ 纯文本字段（`prompt`、`text`、`neg_prompt`）

   STRING 类型且不是文件名字段。

## 输出

表格：每个工作流 图里能给的 / registry 暴露的 / 缺的。
`--gaps` 只列出有缺口的，`--json` 给机器读。

    python3 tools/p20/workflow_ports.py                # 全部工作流
    python3 tools/p20/workflow_ports.py --gaps         # 只看有缺口的
    python3 tools/p20/workflow_ports.py --workflow KEY # 单个
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OBJECT_INFO = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-v4.json"

DYNAMIC_TYPES = frozenset({
    "COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3", "COMFY_MATCHTYPE_V3",
})

# 媒体类字段名：出现这些名字的文件槽位，一律视为"用户该给素材"
MEDIA_FIELD_HINTS = {
    "image": "image", "image_1": "image", "image1": "image",
    "image_2": "image", "image_3": "image",
    "mask": "mask",
    "audio": "audio", "audio_1": "audio",
    "video": "video", "file": "video",
}

# 纯文本字段
TEXT_FIELDS = frozenset({"prompt", "text", "neg_prompt", "negative_prompt",
                         "prompt_2", "prompt_3", "text_positive", "text_negative"})

# 这些节点是"加载器"：它们的文件字段才是接口
def _is_loader(cls: str) -> bool:
    return cls.startswith("Load") or cls.endswith("Loader")


# AUTOGROW 里哪些是"用户接口"、哪些是内部编排。
# 白名单而不是黑名单：宁可漏报一个也不要把内部节点误当接口，
# 因为漏报只是少一个上传框，误报会让 UI 出现 50 个空输入框。
_USER_FACING_AUTOGROW = (
    "ReferenceToVideo", "ReferenceToImage", "AddGuide",
    "VideoToVideo", "ImageToVideo", "AudioToVideo",
    "Conditioning", "ControlNet",
)


def _is_user_facing_autogrow(cls: str, field_name: str) -> bool:
    """AUTOGROW 槽位是不是「用户该给素材」的那种。

    `BatchImagesNode.images`（50 个位置）只是把上游 LoadImage 打包 ——
    它没有"参考"语义，用户不该看到 50 个框。
    `MiniMaxH3ReferenceToVideo.ref_images`（9 个）才是真的要用户给 9 张图。
    """
    if any(tok in cls for tok in _USER_FACING_AUTOGROW):
        return True
    # 兜底：字段名带 ref_ 前缀的也算（ref_images / ref_audios / ref_videos）
    return field_name.startswith("ref_")


@dataclass
class Port:
    """一个「用户可给的接口」。"""
    kind: str          # image | video | audio | text
    node: str
    field: str
    cls: str
    required: bool = False
    max_count: int = 1
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.node}.{self.field}"


@dataclass
class Report:
    workflow: str
    kind: str
    model: str
    ports: list[Port] = field(default_factory=list)
    exposed: list[str] = field(default_factory=list)   # registry 已有的参数名
    extra: list[str] = field(default_factory=list)     # registry 有、图里找不到的
    filled_groups: set[str] = field(default_factory=set)  # 已被参数绑定的 autogrow 组

    def _by_kind(self, k: str) -> list[Port]:
        return [p for p in self.ports if p.kind == k]

    @property
    def media_ports(self) -> list[Port]:
        return [p for p in self.ports if p.kind in ("image", "video", "audio", "mask")]

    @property
    def text_ports(self) -> list[Port]:
        return self._by_kind("text")

    def gaps(self) -> dict[str, list[Port]]:
        """按类型列出「图里有、registry 没暴露」的接口。

        匹配方式：registry 的一个参数可能绑多个 input（一图多绑），
        所以按 **binding 覆盖** 判，不按名字判 —— 参数叫 `image_2` 还是
        `ref_image_1` 不重要，重要的是那个 input 有没有被绑上。
        """
        bound = set()
        for p in self.exposed_bindings:
            bound.add(p)
        out: dict[str, list[Port]] = {}
        for p in self.media_ports + self.text_ports:
            if p.key in bound:
                continue
            # AUTOGROW 槽位：只要**这个组里任意一个位置**被绑，就算已暴露。
            #
            # 关键在于：参数绑的是 **LoadImage 节点**（`137.image`），
            # 而槽位是 `ref_images.ref_image_0 ← 137`。名字对不上，
            # 必须走图连线反查 —— 见 scan() 里填的 `filled_groups`。
            if "." in p.field:
                group = p.field.split(".")[0]
                if group in self.filled_groups:
                    continue
            out.setdefault(p.kind, []).append(p)
        return out

    exposed_bindings: list[str] = field(default_factory=list)


def _load_oi() -> dict:
    d = json.loads(Path(OBJECT_INFO).read_text(encoding="utf-8"))
    return d.get("object_info") if isinstance(d.get("object_info"), dict) else d


def _spec_type(spec) -> str:
    if isinstance(spec, list) and spec:
        t = spec[0]
        return t if isinstance(t, str) else "COMBO"
    return ""


def _spec_meta(spec) -> dict:
    return spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}


def _classify_field(cls: str, name: str, spec, *, is_optional: bool) -> Port | None:
    """判断一个输入字段是不是「用户该给的接口」。"""
    t = _spec_type(spec)
    meta = _spec_meta(spec)
    low = name.lower()

    # ① AUTOGROW：多路参考。
    #
    # **但不是所有 AUTOGROW 都是用户接口**。要回溯上游：
    #   `MiniMaxH3ReferenceToVideo.ref_images`（max=9）→ 用户传图 ✓
    #   `BatchImagesNode.images`（max=50）→ 只是把上游 LoadImage 打包，本身不是接口 ✗
    #
    # 判据：类名里带 Reference / Audio / Video 这类"对外语义"的才算；
    # 纯粹的 `Batch*` / `Math*` / `Colored*` 是内部编排节点。
    if t == "COMFY_AUTOGROW_V3":
        if not _is_user_facing_autogrow(cls, name):
            return None
        tpl = meta.get("template") or {}
        prefix = tpl.get("prefix") or (name + "_")
        mx = int(tpl.get("max") or 1)
        req = ((tpl.get("input") or {}).get("required") or {})
        inner = next(iter(req.values()), None)
        inner_t = _spec_type(inner) if inner else ""
        kind = {"IMAGE": "image", "AUDIO": "audio", "VIDEO": "video"}.get(inner_t, "")
        if not kind:
            return None
        return Port(kind, node="", field=f"{name}.{prefix}0", cls=cls,
                    required=False, max_count=mx,
                    note=f"多路参考，最多 {mx} 个")

    # ② 加载器的文件字段
    if _is_loader(cls):
        # 候选是文件名列表（COMBO 且选项里有 .safetensors/.png/.wav 等）
        opts = meta.get("options") or (spec[0] if isinstance(spec, list) and isinstance(spec[0], list) else None)
        looks_file = False
        if isinstance(opts, list) and opts:
            sample = [str(o) for o in opts[:5] if not isinstance(o, dict)]
            if any(x.endswith((".safetensors", ".ckpt", ".pt", ".bin", ".pth", ".gguf"))
                   for x in sample):
                looks_file = False        # 模型权重，不是用户素材
            elif any(x.endswith((".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov",
                                 ".webm", ".wav", ".mp3", ".flac"))
                     for x in sample):
                looks_file = True
        if low in MEDIA_FIELD_HINTS or looks_file:
            kind = MEDIA_FIELD_HINTS.get(low)
            if not kind:
                # 用选项推断
                kind = "image"
                if isinstance(opts, list) and any(str(o).endswith((".wav", ".mp3", ".flac")) for o in opts):
                    kind = "audio"
                elif isinstance(opts, list) and any(str(o).endswith((".mp4", ".mov", ".webm")) for o in opts):
                    kind = "video"
            return Port(kind, node="", field=name, cls=cls, required=not is_optional)
        return None

    # ③ 纯文本
    #
    # 但有两种 STRING 不是"用户该写的提示词"，必须排除：
    #
    #   a) `TextGenerate*` 这类**LLM 改写节点**：它的 prompt 是"给 LLM 的指令"，
    #      用户的创作意图应该写在最终的 CLIPTextEncode 里，而不是这里。
    #      实测 `TextGenerateLTX2Prompt.prompt` 默认是 `''`，
    #      `TextGenerate.prompt` 默认是 `"Replace costumes for the character"` ——
    #      都是模板指令，暴露给用户只会让人填错地方。
    #
    #   b) 值写成 `['1277', 0]`（对另一个节点的引用）的字段：那是连线，
    #      不是字面量。暴露出来用户改了也不生效（渲染时会覆盖）。
    if name in TEXT_FIELDS or (t == "STRING" and low in TEXT_FIELDS):
        if cls.startswith("TextGenerate"):
            return None
        return Port("text", node="", field=name, cls=cls, required=not is_optional)
    return None


# 这些节点的 text 输入是"检测/追踪的目标描述"，不是给用户写创作提示词的。
# 实测：scail2 里有 4 个 CLIPTextEncode 的值是 `'human'`，
# 它们接到 SAM3_VideoTrack.conditioning —— 那是"告诉 SAM3 检测人"，
# 用户改成 "a dancing cat" 只会让检测器失效。
#
# 判据看**下游**而不是节点自己：同一个 CLIPTextEncode，接到 KSampler 是提示词，
# 接到 SAM3_VideoTrack 是检测目标。所以这个常量是下游类名。
_DETECTOR_CONSUMERS = (
    "SAM3_VideoTrack", "SAM3_Detect", "SAM2", "GroundingDino",
    "SCAIL2ColoredMask", "BBoxDetect",
)


def _feeds_detector(graph: dict, node_id: str) -> bool:
    """这个节点的输出是否流进了检测/追踪类节点。"""
    for n in graph.values():
        if not isinstance(n, dict):
            continue
        cls = str(n.get("class_type") or "")
        if not any(tok in cls for tok in _DETECTOR_CONSUMERS):
            continue
        for v in (n.get("inputs") or {}).values():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) == str(node_id):
                return True
    return False


def scan(workflow_key: str, oi: dict) -> Report | None:
    from tools.p20 import registry
    try:
        wf = registry.get_workflow(workflow_key)
    except Exception:                      # noqa: BLE001
        return None

    graph = json.loads((ROOT / wf["graph"]).read_text(encoding="utf-8"))
    model = str((wf.get("model") or {}).get("key") or wf.get("model_key") or "")
    rep = Report(workflow=workflow_key, kind=str(wf.get("kind") or ""), model=model)

    params = wf.get("params") or {}
    rep.exposed = sorted(params)

    # registry 已绑定的 input（"node.field" 形式）
    bound: list[str] = []
    bound_nodes: set[str] = set()
    for pname, pspec in params.items():
        for b in (pspec.get("bindings") or []):
            node, inp = str(b.get("node") or ""), str(b.get("input") or "")
            if node and inp:
                bound.append(f"{node}.{inp}")
                bound_nodes.add(node)
    rep.exposed_bindings = bound

    # **哪些 AUTOGROW 组已经被占用了**。
    # 做法：看每个 AUTOGROW 槽位（`ref_images.ref_image_0`）连的节点 id，
    # 如果那个节点被某个参数绑了 → 这一组算"已暴露"。
    # 为什么必须走连线：参数绑的是 LoadImage 节点（`137.image`），
    # 槽位名 `ref_images.ref_image_0` 和参数名 `image` 完全没有字符串关系。
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        for k, v in (node.get("inputs") or {}).items():
            if "." not in k:
                continue
            group = k.split(".")[0]
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in bound_nodes:
                rep.filled_groups.add(group)

    # 图里能给的
    for nid, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        cls = node["class_type"]
        spec = oi.get(cls)
        if not spec:
            continue
        inp = spec.get("input") or {}
        for grp, is_opt in (("required", False), ("optional", True)):
            for fname, fspec in (inp.get(grp) or {}).items():
                p = _classify_field(cls, fname, fspec, is_optional=is_opt)
                if p:
                    p.node = str(nid)
                    # text 类要查下游：接到检测器的是"检测目标描述"，不是创作提示词
                    if p.kind == "text" and _feeds_detector(graph, str(nid)):
                        continue
                    # 值是对另一节点的引用（['1277', 0]）→ 那是连线不是字面量
                    if p.kind == "text":
                        cur = (node.get("inputs") or {}).get(fname)
                        if isinstance(cur, list) and len(cur) == 2 and str(cur[0]) in graph:
                            continue
                    rep.ports.append(p)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="工作流接口盘点")
    ap.add_argument("--workflow", help="只查一个")
    ap.add_argument("--gaps", action="store_true", help="只看有缺口的")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kind", choices=["image", "video"], help="只查某类")
    a = ap.parse_args(argv)

    from tools.p20 import registry
    oi = _load_oi()
    keys = [a.workflow] if a.workflow else [w["key"] for w in registry.list_workflows()]
    reps = [r for r in (scan(k, oi) for k in keys) if r]
    if a.kind:
        reps = [r for r in reps if r.kind == a.kind]

    if a.json:
        print(json.dumps([{
            "workflow": r.workflow, "kind": r.kind, "model": r.model,
            "ports": [p.__dict__ for p in r.ports],
            "exposed": r.exposed, "exposed_bindings": r.exposed_bindings,
            "gaps": {k: [p.__dict__ for p in v] for k, v in r.gaps().items()},
        } for r in reps], ensure_ascii=False, indent=2))
        return 0

    if a.gaps:
        reps = [r for r in reps if r.gaps()]

    print(f"{len(reps)} 个工作流\n")
    total_gap = 0
    for r in sorted(reps, key=lambda x: (-len(x.gaps()), x.workflow)):
        g = r.gaps()
        n = sum(len(v) for v in g.values())
        total_gap += n
        mark = "✗" if n else "✓"
        print(f"{mark} {r.workflow}  [{r.kind}/{r.model[:26]}]  "
              f"图里可给 {len(r.media_ports) + len(r.text_ports)} · "
              f"已暴露 {len(r.exposed)} · 缺 {n}")
        if n:
            for kind in ("image", "video", "audio", "mask", "text"):
                items = g.get(kind) or []
                if not items:
                    continue
                names = ", ".join(
                    (f"{p.field}×{p.max_count}" if p.max_count > 1 else p.field)
                    for p in items[:8])
                more = "…" if len(items) > 8 else ""
                print(f"      {kind:<6} 缺: {names}{more}")
    print(f"\n合计缺口 {total_gap} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
