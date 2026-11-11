#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流卡门禁：把历史事故变成**出厂规则**。

## 这个文件存在的理由

2026-11-03 ~ 11-11 之间，工作流总数从 5 条涨到 54 条，踩过的坑几乎全部
落在同几类上。每类都花过真钱才发现：

    类 1  「名字对不上」     图里要 image_2，params 里没这个键 → 上传不上去
    类 2  「值不在候选里」   text_encoder 写了个镜像里没有的文件 → 提交那一刻才被拒
    类 3  「当默认值被静默吃」required 带 default，图里没写 → 服务端补，行为和你以为的不同
    类 4  「正负提示词错位」 prompt 和 neg_prompt 是同一句话 → 出片必糊，但不报错
    类 5  「标 verified 却没证据」

这些都不是"某人粗心"，而是**仓库允许它们存在**：54 张卡，每张 8~12 个字段，
靠人记规则必然漏。所以规则要能执行。

## 设计原则

**能机器判的绝不留给人。** 每条规则必须给出「哪个文件、哪个字段、怎么修」。

**判不了的不许编。** 张量形状（`fasth3_ominiref_r2v` 那条 24 vs 768）、
显存够不够、推理会不会 NaN，本地没有求解器 —— 这些一律不写成规则，
免得变成"检查通过 = 一定能跑"的假承诺。

**规则要能解释为什么。** 每条都带 `WHY`，说清它防的是哪次事故。
新来的人看到规则就知道该怕什么，而不是"项目经理不让这么写"。

## 用法

    python3 -m tools.p20.wf_lint                # 全量体检
    python3 -m tools.p20.wf_lint --key K        # 只查一条
    python3 -m tools.p20.wf_lint --strict       # 警告也算失败（发布前用）
    python3 -m tools.p20.wf_lint --json

退出码：0 通过 / 1 有问题。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WF_DIR = ROOT / "registry" / "workflows"
MODEL_DIR = ROOT / "registry" / "models"
COMPAT = ROOT / "registry" / "compat.json"

# ---------------------------------------------------------------- 规则表
# 每条：(编号, 级别, 标题, 为什么)
# ERROR = 必须修，否则这条工作流一定出问题或一定跑不起来
# WARN  = 现在能跑，但会误导人 / 会在换镜像时炸
RULES: dict[str, tuple[str, str, str]] = {
    "WF001": ("ERROR", "卡不是合法 JSON 或缺必填顶层字段",
              "卡坏了 p20ctl import 会整体失败，连带全部工作流都导不进去"),
    "WF002": ("ERROR", "graph 文件不存在或不是合法 API 图",
              "2026-11-08：graph 路径写成相对路径，换工作目录就找不到"),
    "WF003": ("ERROR", "model.slots 指向图里不存在的节点/输入",
              "渲染时 _set() 抛错，而那时实例已经开好、钱已经花了"),
    "WF004": ("ERROR", "params.bindings 指向图里不存在的节点/输入",
              "2026-11-08：换了一版图（节点 id 全变），卡里的 bindings 还是旧的。\n"
              "             渲染时 _set() 抛「指向不存在的节点」，而那时实例已开好、钱已花"),
    "WF005": ("ERROR", "prompt 与 neg_prompt 默认值相同",
              "2026-11-11：qwen_layered 和 scail2 两张卡把正向词填进了负向框，"
              "出片必糊但作业 PASS —— 这是最贵的一类错，因为成片看起来'像风格'"),
    "WF006": ("ERROR", "prompt 默认值里含负向词",
              "2026-11-11：ltx2_3_i2v 的 prompt 默认值是 'pc game, cartoon, ugly'，"
              "那是负向词，被填进了正向框"),
    "WF007": ("WARN", "params 里暴露权重文件名（应走 model.slots）",
              "权重文件名是模型属性，不是用户参数。放进 params 等于让用户改模型，"
              "而 UI 会渲染成文本框 —— 用户打错一个字就是 use_missing_models"),
    "WF008": ("ERROR", "name 里写死状态词，与 compat 结论矛盾",
              "2026-11-11：4 条已 verified 的卡名字里还挂着「尚未开发」，"
              "用户以为不能用，实际上能用"),
    "WF009": ("ERROR", "视频卡没有时长/体积判据",
              "2026-11-09：21 个视频工作流只声明 min_bytes，出 1 秒也算 PASS。"
              "现在 expect_for 能从图推出时长，但推不出来时必须有 fps 兜底"),
    "WF010": ("ERROR", "output 指向的不是保存节点 / 前缀是连线",
              "前缀写不进去 = 产物名字不受控，收割脚本按前缀找不到文件"),
    "WF011": ("ERROR", "媒体参数类型与 loader 节点不匹配",
              "2026-11-10：type='video' 掉进文本框（后来是 UI 的锅），"
              "但卡层同样能错：声明的类型和实际 loader 对不上，上传后渲染必失败"),
    "WF012": ("WARN", "参数没有 description",
              "2026-11-11：333 个参数里有 273 个没说明，用户靠猜。"
              "说明是给终端用户看的，不是给开发者看的"),
    "WF013": ("ERROR", "参数默认值违反 card 自己声明的 type/min/max/enum",
              "2026-11-11：`qwen_image_t2i` 的 seed 默认值照抄了图里的 `{{SEED}}` 占位符，\n"
              "             类型声明是 int —— UI 会渲染出一个字符串框，提交时类型对不上"),
    "WF014": ("ERROR", "required 参数没有 default，且 UI 无法收集（类型不支持）",
              "UI 只认 string/int/float/bool/image/video/enum 六种，"
              "别的类型会渲染成文本框，提交时类型对不上"),
    "WF015": ("ERROR", "两个 model.slots 角色绑到同一个节点",
              "2026-11-09：主 LoRA 和文本编码器 LoRA 的 input 都叫 lora_name，"
              "自动推断时绑到同一节点，另一个永远没人填 —— 不报错，用错权重"),
    "WF017": ("ERROR", "prompt / neg_prompt 绑到了错误的一侧",
              "2026-11-11：4 张卡把 neg_prompt 绑到了**正向**编码器（甚至绑到另一个"
              "sampler 的正向节点）。后果是把负向词当正向词喂进去 —— 出片糊、"
              "构图崩，但作业 PASS、质量分可能还是 B。这是全仓库最隐蔽的一类错"),
    "WF018": ("ERROR", "prompt 与 neg_prompt 绑到同一个节点同一条输入",
              "两个参数互相覆盖，后应用的赢 —— UI 上改哪个都只生效一个"),
    "WF019": ("ERROR", "两个参数绑到同一个节点同一条输入（不分正负）",
              "2026-11-11：`scail2_int8` 同时有 prompt / prompt_2 / prompt_3，"
              "其中 prompt 和 prompt_3 都绑 1404.text —— 用户在 UI 上改 prompt，"
              "prompt_3 的旧值随后又把它覆盖回去，改了等于没改"),
    "WF016": ("WARN", "experimental 卡出现在 UI 的可用区",
              "未开发的工作流必须收进折叠区且默认拒绝执行，否则用户点了就跑不起来"),
}

LOADER_FOR = {
    "image": {"LoadImage"},
    "video": {"LoadVideo", "VHS_LoadVideo", "LoadVideoUpload"},
    "audio": {"LoadAudio"},
}

# 权重输入名：这些字段的值应该是模型文件名，由 model.slots 提供
WEIGHT_INPUTS = frozenset({
    "ckpt_name", "unet_name", "lora_name", "vae_name", "clip_name",
    "clip_name1", "clip_name2", "model_name", "control_net_name",
    "style_model_name", "text_encoder", "upscale_model",
})

# 负向词：出现在**正向**提示词默认值里就是填错了
NEG_WORDS = (
    "blurry", "low quality", "worst quality", "lowres", "pc game",
    "ugly", "deformed", "watermark", "卡通", "静态", "过曝",
    "低质量", "最差质量", "畸形",
)

# 名字里不许写死的状态词（该由 compat 决定）
STATUS_IN_NAME = ("尚未开发", "未开发", "待验证", "待开发", "TODO")

# UI 能渲染的类型（web/dist/app.js renderForm）
UI_TYPES = frozenset({"string", "int", "float", "bool", "image", "video", "audio", "enum"})

# output 节点必须属于保存/预览类
OUTPUT_CLASSES = ("SaveImage", "SaveImageAdvanced", "SaveVideo",
                  "VHS_VideoCombine", "SaveAnimatedWEBP", "PreviewImage")


@dataclass
class Finding:
    rule: str
    level: str
    workflow: str
    where: str
    detail: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "level": self.level, "workflow": self.workflow,
                "where": self.where, "detail": self.detail,
                "why": RULES.get(self.rule, ("", "", ""))[2]}


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "WARN"]

    def ok(self, *, strict: bool = False) -> bool:
        return not self.findings if strict else not self.errors


def _add(r: Report, rule: str, key: str, where: str, detail: str) -> None:
    lvl = RULES.get(rule, ("WARN",))[0]
    r.findings.append(Finding(rule, lvl, key, where, detail))


# ---------------------------------------------------------------- 加载
def _load_compat() -> dict[str, str]:
    if not COMPAT.is_file():
        return {}
    try:
        d = json.loads(COMPAT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {e["workflow"]: e.get("status", "experimental")
            for e in (d.get("entries") or []) if isinstance(e, dict) and e.get("workflow")}


def _graph_of(card: dict) -> dict | None:
    g = card.get("graph")
    if not isinstance(g, str) or not g:
        return None
    gp = Path(g)
    if not gp.is_absolute():
        gp = ROOT / gp
    if not gp.is_file():
        return None
    try:
        d = json.loads(gp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


# ---------------------------------------------------------------- 单条卡的检查
def lint_card(key: str, card: dict, compat_status: str = "experimental") -> list[Finding]:
    """纯函数：检查一张卡。graph 缺失时只跑与图无关的规则。"""
    r = Report()
    graph = _graph_of(card)

    # WF002 图
    if not graph:
        _add(r, "WF002", key, "graph", f'图文件不存在或解析失败：{card.get("graph")!r}')
        graph = {}

    params = card.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # WF003 模型槽位
    slots = (card.get("model") or {}).get("slots") or {}
    seen_nodes: dict[str, str] = {}
    for role, s in slots.items():
        if not isinstance(s, dict):
            continue
        nid, inp = str(s.get("node") or ""), str(s.get("input") or "")
        node = graph.get(nid)
        if graph and not isinstance(node, dict):
            _add(r, "WF003", key, f"model.slots.{role}",
                 f"指向不存在的节点 {nid}")
        elif node and inp not in (node.get("inputs") or {}):
            _add(r, "WF003", key, f"model.slots.{role}",
                 f"节点 {nid}（{node.get('class_type')}）没有输入 {inp!r}")
        # WF015 同一节点服务两个角色
        if nid in seen_nodes:
            _add(r, "WF015", key, f"model.slots.{role}",
                 f"与 {seen_nodes[nid]} 绑同一节点 {nid} —— "
                 f"另一个角色永远填不到图里，不报错但用错权重")
        seen_nodes[nid] = role

    # WF004 参数绑定 + WF007/WF011
    for pn, ps in params.items():
        if not isinstance(ps, dict):
            continue
        typ = ps.get("type")
        for i, b in enumerate(ps.get("bindings") or []):
            if not isinstance(b, dict):
                continue
            nid, inp = str(b.get("node") or ""), str(b.get("input") or "")
            node = graph.get(nid)
            if graph and not isinstance(node, dict):
                _add(r, "WF004", key, f"params.{pn}.bindings[{i}]",
                     f"指向不存在的节点 {nid}")
                continue
            if node and inp not in (node.get("inputs") or {}):
                # 连线型输入在图里可能只是没写（可选），但 required 的写错了必然炸
                _add(r, "WF004", key, f"params.{pn}.bindings[{i}]",
                     f"节点 {nid}（{node.get('class_type')}）没有输入 {inp!r}")
            # WF007 权重文件名不该是用户参数
            if inp in WEIGHT_INPUTS:
                _add(r, "WF007", key, f"params.{pn}",
                     f"绑定到权重输入 {nid}.{inp} —— 权重文件属于模型卡的 files，"
                     f"放进 params 会被 UI 渲染成文本框，用户打错字就 use_missing_models")
            # WF011 媒体类型 ↔ loader
            if typ in LOADER_FOR and node:
                ct = str(node.get("class_type") or "")
                if ct.startswith(("Load", "VHS_Load")) and ct not in LOADER_FOR[typ]:
                    _add(r, "WF011", key, f"params.{pn}",
                         f"声明 type={typ}，却绑到 {ct}（{nid}）—— "
                         f"上传后渲染必失败")

    # WF017 / WF018：正负侧绑错
    #
    # 判据不靠猜：从**真正做采样**的节点出发，沿 positive/negative 各自往上追，
    # 得到「真正的正向编码节点」和「真正的负向编码节点」，再拿参数绑定去比。
    # 追不到（结构不认识的图）就跳过，绝不在推测上判错。
    if graph:
        try:
            from tools.p20 import wf_infer as _INF
            live = _INF._reachable_from_output(graph)
            roots = _INF._sampler_roots(graph, live)
            if roots:
                entry = _INF._text_entry(graph, roots[0])
                real = {side: entry.get(side) for side in ("positive", "negative")}
                # 两侧都追到了才比。只有一侧时不做判 —— 另一侧可能是
                # ConditioningZeroOut（刻意无负向词），此时报错就是误报。
                if real["positive"] and real["negative"]:
                    for pname, side in (("prompt", "positive"), ("neg_prompt", "negative")):
                        ps = params.get(pname)
                        if not isinstance(ps, dict):
                            continue
                        got = {(str(b.get("node")), str(b.get("input")))
                               for b in (ps.get("bindings") or []) if isinstance(b, dict)}
                        want = (real[side][0], real[side][1])
                        if got and want not in got:
                            # 它到底绑到了哪一侧？
                            other = "negative" if side == "positive" else "positive"
                            where = ""
                            if real[other] and (real[other][0], real[other][1]) in got:
                                where = f"（实际绑到了 {other} 侧 —— 正负填反了）"
                            _add(r, "WF017", key, f"params.{pname}",
                                 f"应绑 {want[0]}.{want[1]}（{graph.get(want[0], {}).get('class_type')}），"
                                 f"实际绑 {sorted(got)}{where}")
                    # WF018 同一条输入
                    pb = {(str(b.get("node")), str(b.get("input")))
                          for b in (params.get("prompt") or {}).get("bindings") or []
                          if isinstance(b, dict)}
                    nb = {(str(b.get("node")), str(b.get("input")))
                          for b in (params.get("neg_prompt") or {}).get("bindings") or []
                          if isinstance(b, dict)}
                    both = pb & nb
                    if both:
                        _add(r, "WF018", key, "params.prompt/neg_prompt",
                             f"绑到同一个节点同一条输入 {sorted(both)} —— 互相覆盖")
        except Exception as e:                        # noqa: BLE001
            _add(r, "WF017", key, "params", f"正负侧检查跳过（{type(e).__name__}: {e}）")

    # WF019：**任意**两个参数绑到同一节点同一条输入 → 后应用的覆盖先应用的。
    #
    # 渲染顺序是 `for name, spec in schema.items(): apply`，所以字典序靠后的赢。
    # 用户改哪一个都可能被另一个覆盖，而且完全不报错。
    # 2026-11-11 实测 `scail2_int8` 有 3 个 prompt 参数绑到 2 个节点，
    # 其中两个绑同一个 —— 用户改 prompt 会被 prompt_3 的旧值覆盖。
    seen_bind: dict[tuple[str, str], str] = {}
    for pname in sorted(params):
        ps = params[pname]
        if not isinstance(ps, dict):
            continue
        for b in ps.get("bindings") or []:
            if not isinstance(b, dict):
                continue
            key_b = (str(b.get("node")), str(b.get("input")))
            if key_b in seen_bind:
                _add(r, "WF019", key, f"params.{pname}",
                     f"与 params.{seen_bind[key_b]} 绑同一条输入 "
                     f"{key_b[0]}.{key_b[1]} —— 后者会覆盖前者，用户改了不生效")
            else:
                seen_bind[key_b] = pname

    # WF005 / WF006 提示词
    pdef = (params.get("prompt") or {}).get("default")
    ndef = (params.get("neg_prompt") or {}).get("default")
    if pdef and ndef and str(pdef).strip() == str(ndef).strip():
        _add(r, "WF005", key, "params.neg_prompt",
             f"与 prompt 默认值完全相同：{str(pdef)[:50]!r}")
    if pdef:
        hit = [w for w in NEG_WORDS if w.lower() in str(pdef).lower()]
        if hit:
            _add(r, "WF006", key, "params.prompt",
                 f"默认值含负向词 {hit}：{str(pdef)[:60]!r}")

    # WF008 名字里的状态词
    name = str(card.get("name") or "")
    if compat_status in ("verified", "equivalent"):
        hit = [w for w in STATUS_IN_NAME if w in name]
        if hit:
            _add(r, "WF008", key, "name",
                 f"已 {compat_status}，名字里还写着 {hit} —— 用户以为不能用")

    # WF009 视频判据
    if card.get("kind") == "video":
        exp = card.get("expect_defaults") or {}
        if "duration_s" not in exp and "fps" not in card:
            _add(r, "WF009", key, "expect_defaults",
                 "既无 duration_s 也无 fps —— 出 1 秒的废片也会判 PASS")
        if "min_bytes" not in exp:
            _add(r, "WF009", key, "expect_defaults.min_bytes",
                 "缺体积下限 —— 0 字节残片也可能过")

    # WF010 output
    out = card.get("output") or {}
    onid = str(out.get("node") or "")
    if out:
        if graph and onid not in graph:
            _add(r, "WF010", key, "output.node", f"节点 {onid} 在图里不存在")
        elif graph:
            ct = str((graph[onid] or {}).get("class_type") or "")
            if not ct.startswith(OUTPUT_CLASSES):
                _add(r, "WF010", key, "output.node",
                     f"{onid} 是 {ct}，不是保存节点（{OUTPUT_CLASSES[:3]}…）")
            prefix = ((graph[onid] or {}).get("inputs") or {}).get(str(out.get("input") or ""))
            if isinstance(prefix, list):
                _add(r, "WF010", key, f"output.{out.get('input')}",
                     "是连线，前缀写不进去 —— 产物名字不受控，收割脚本找不到")

    # WF012 / WF013 / WF014 参数契约本身
    for pn, ps in params.items():
        if not isinstance(ps, dict):
            continue
        if not str(ps.get("description") or "").strip():
            _add(r, "WF012", key, f"params.{pn}", "没有 description")
        typ = ps.get("type")
        if typ not in UI_TYPES:
            _add(r, "WF014", key, f"params.{pn}",
                 f"类型 {typ!r} UI 渲染不出来（支持 {sorted(UI_TYPES)}）")
        if "default" in ps:
            v = ps["default"]
            bad = None
            if typ in ("int",) and (isinstance(v, bool) or not isinstance(v, int)):
                bad = f"声明 int，默认值是 {type(v).__name__}"
            elif typ in ("float",) and (isinstance(v, bool) or not isinstance(v, (int, float))):
                bad = f"声明 float，默认值是 {type(v).__name__}"
            elif typ == "string" and not isinstance(v, str):
                bad = f"声明 string，默认值是 {type(v).__name__}"
            elif typ == "bool" and not isinstance(v, bool):
                bad = f"声明 bool，默认值是 {type(v).__name__}"
            elif typ == "enum":
                vals = ps.get("values") or ps.get("enum") or []
                if vals and v not in vals:
                    bad = f"默认值 {v!r} 不在 values 内"
            if bad:
                _add(r, "WF013", key, f"params.{pn}.default", bad)
            if typ in ("int", "float") and not isinstance(v, bool):
                if "min" in ps and ps["min"] is not None and v < ps["min"]:
                    _add(r, "WF013", key, f"params.{pn}.default",
                         f"{v} 小于声明的 min={ps['min']}")
                if "max" in ps and ps["max"] is not None and v > ps["max"]:
                    _add(r, "WF013", key, f"params.{pn}.default",
                         f"{v} 大于声明的 max={ps['max']}")

    return r.findings


# ---------------------------------------------------------------- 全量
def lint_all(*, only: str = "") -> Report:
    rep = Report()
    compat = _load_compat()
    cards = sorted(WF_DIR.glob("*.json"))
    if only:
        cards = [p for p in cards if p.stem == only]
        if not cards:
            _add(rep, "WF001", only, "registry/workflows",
                 f"卡不存在：registry/workflows/{only}.json")
            return rep

    for p in cards:
        key = p.stem
        rep.checked.append(key)
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _add(rep, "WF001", key, p.name, f"JSON 解析失败：{e}")
            continue
        if not isinstance(card, dict):
            _add(rep, "WF001", key, p.name, "顶层不是对象")
            continue
        for req in ("schema", "key", "name", "version", "kind", "engine", "graph",
                    "model", "params", "output"):
            if req not in card:
                _add(rep, "WF001", key, p.name, f"缺字段 {req}")
        if card.get("key") and card["key"] != key:
            _add(rep, "WF001", key, p.name,
                 f"文件名是 {key}.json，卡里 key={card['key']!r}")
        rep.findings += lint_card(key, card, compat.get(key, "experimental"))
    return rep


# ---------------------------------------------------------------- 人话输出
def format_report(rep: Report, *, strict: bool = False) -> str:
    if not rep.findings:
        return f"✓ {len(rep.checked)} 条工作流全部通过门禁（{len(RULES)} 条规则）"

    lines = []
    by_wf: dict[str, list[Finding]] = {}
    for f in rep.findings:
        by_wf.setdefault(f.workflow, []).append(f)

    for wf, items in sorted(by_wf.items()):
        errs = [i for i in items if i.level == "ERROR"]
        head = f"✗ {wf}" if errs else f"! {wf}"
        lines.append(head)
        for f in sorted(items, key=lambda x: (x.level != "ERROR", x.rule)):
            mark = "✗" if f.level == "ERROR" else "!"
            title = RULES.get(f.rule, ("", f.rule, ""))[1]
            lines.append(f"   {mark} [{f.rule}] {f.where}: {f.detail}")
            lines.append(f"        ({title})")
    lines.append("")
    ne, nw = len(rep.errors), len(rep.warnings)
    lines.append(f"{len(rep.checked)} 条 · 错误 {ne} · 警告 {nw}")
    lines.append("✓ 全部通过" if rep.ok(strict=strict)
                 else ("✗ 有错误" if ne else "! 只有警告（--strict 下算失败）"))
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wf_lint", description="工作流卡门禁（¥0）")
    ap.add_argument("--key", default="", help="只查一条")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true", help="列出所有规则")
    a = ap.parse_args(argv)

    if a.rules:
        for rid, (lvl, title, why) in sorted(RULES.items()):
            print(f"{rid} [{lvl}] {title}")
            print(f"    为什么：{why}")
        return 0

    rep = lint_all(only=a.key)
    if a.json:
        print(json.dumps({"ok": rep.ok(strict=a.strict),
                          "checked": rep.checked,
                          "findings": [f.as_dict() for f in rep.findings]},
                         ensure_ascii=False, indent=1))
    else:
        print(format_report(rep, strict=a.strict))
    return 0 if rep.ok(strict=a.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
