#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流/模型 变更的统一入口：**只有这一条路，不能绕**。

## 为什么需要

工作流由五样东西构成，它们**必须一起变**：

    graph     workflows/<key>.api.json        节点图
    params    registry/workflows/<key>.json   参数契约（类型/默认/绑定）
    model     registry/models/<key>.json      权重槽位
    compat    真机证据                         能不能跑
    index     registry/registry.db            派生索引（可重建）

历史上出过的事故，全部来自"只改了一部分"：

  · 图里要 `image_2`，params 里没这个键 → 上传不上去
  · 标了 verified，但镜像里缺节点 → 开了机才报错（¥0.13/次）
  · 改了 params 默认值，忘了 `p20ctl import` → UI 还是旧的
  · 手改 `compat.json` 把 experimental 写成 verified → 没有证据
  · prompt 和 neg_prompt 填成同一句话 → 出片必糊，但作业 PASS

所以把动作收敛成 5 个动词，每个都保证五样东西一致：

    wfctl new      新增（选模板 → 反推参数 → 门禁 → 落盘 → 导入）
    wfctl update   改已有（改完自动门禁 + 校验 + 导入）
    wfctl verify   真机跑完 → 写兼容性证据（唯一能标 verified 的入口）
    wfctl check    全量体检（配置 + 门禁 + 预演 + 预检，不花钱）
    wfctl guide    打印新增工作流的七步流程

## 新增工作流的七步（`wfctl new` 会自动跑完 1–5）

    ① 拿图      从 ComfyUI「导出 API 格式」，落进 workflows/<key>.api.json
    ② 选骨架    wfctl new --template i2v     （8 个模板，见 wf_templates）
    ③ 反推参数  wf_infer 读图推 params/槽位/output/期望，人只确认不手填
    ④ 过门禁    wf_lint 19 条规则（历史事故全部转成规则）
    ⑤ 导入索引  p20ctl import + validate
    ⑥ 真机验证  p20ctl new --workflow K --allow-experimental --run    （花钱）
    ⑦ 转正      wfctl verify --key K --job-id <job>                   （唯一入口）

第 ③ 步是这次新加的关键：节点 id 是 ComfyUI 导出时生成的随机数字串
（`1349`、`src`、`ks`），手填 bindings 必然出错，而错了的后果不是报错，
是渲染时 `_set()` 抛异常 —— 那时实例已经开好、钱已经花了。

## 设计原则

**能自动做的不要让人记。** update 改完自动 validate + import，不要求你
记得先跑什么后跑什么。参数契约由 wf_infer 从图反推，人只做确认。

**不能自动的要说清代价。** verify 要建机花钱，不会在 update 里自动触发 ——
但会明确告诉你"这条现在还是 experimental"。

**机器判不了的不许编。** 张量形状（`fasth3_tensor 24 vs 768`）、显存够不够、
推理会不会 NaN，本地没有求解器。这些不写成门禁规则，免得变成
"检查通过 = 一定能跑"的假承诺。

**失败要说人话。** 每条错误都带"该改哪个文件的哪一段"。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WF_DIR = ROOT / "registry" / "workflows"
MODEL_DIR = ROOT / "registry" / "models"
GRAPH_DIR = ROOT / "workflows"


def _say(msg: str) -> None:
    print(msg, flush=True)


def _die(msg: str, code: int = 1):
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _p20(args: list[str], *, quiet: bool = False) -> tuple[int, str]:
    """跑 p20ctl 子命令，返回 (rc, 输出)。"""
    cmd = [sys.executable, "-m", "tools.p20.p20ctl"] + args
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    if not quiet:
        print(out.rstrip(), flush=True)
    return r.returncode, out


def _import_and_validate() -> bool:
    """重建索引 + 全量体检。两个都得过。"""
    rc1, out1 = _p20(["import"], quiet=True)
    if rc1 != 0:
        _die(f"导入失败：\n{out1[-800:]}")
    _say(f"  ✓ 索引已重建：{out1.strip().splitlines()[-1] if out1.strip() else ''}")

    rc2, out2 = _p20(["validate"], quiet=True)
    if rc2 != 0:
        _die(f"体检不通过：\n{out2[-1500:]}")
    # validate 对 experimental 只警告不失败，把警告数报出来
    warns = [l for l in out2.splitlines() if l.startswith("⚠")]
    if warns:
        _say(f"  ! {len(warns)} 条 experimental（执行需 --allow-experimental）")
    _say("  ✓ 体检通过")
    return True


def _lint(key: str = "") -> bool:
    """门禁：历史事故转成的规则集。任一 ERROR 就拦。"""
    from tools.p20 import wf_lint as L
    rep = L.lint_all(only=key)
    if not rep.ok():
        _say(L.format_report(rep))
        return False
    warns = rep.warnings
    if warns:
        _say(f"  ! 门禁提示 {len(warns)} 条（不阻塞，发布前跑 --strict 会拦）")
        for f in warns:
            _say(f"    [{f.rule}] {f.workflow}.{f.where}: {f.detail}")
    else:
        _say(f"  ✓ 门禁通过（{len(L.RULES)} 条规则）")
    return True


def _guard() -> bool:
    """控制台入口守卫：确保前端/工作流没有被改到错地方。"""
    from tools import console_guard as G
    r = G.audit()
    if not r.ok:
        _say(G.format_report(r))
        return False
    _say("  ✓ 控制台入口统一")
    return True


# ---------------------------------------------------------------- new
def cmd_new(a) -> int:
    """新增工作流：模板 → 反推 → 门禁 → 落盘 → 导入。

    七步里的 ①–⑤ 全在这里跑完，人只做两件事：给图、确认三处机器判不了的。
    """
    key = a.key
    from tools.p20 import config_schema as _cs
    if not _cs.KEY_RE.match(str(key)):
        _die(f"key 非法：{key!r}\n"
             f"  规则：小写字母开头，只含小写字母/数字/下划线，2–64 字符\n"
             f"  例：pack_video_ltx2_3_i2v、qwen_image_t2i")
    card_path = WF_DIR / f"{key}.json"
    if card_path.exists() and not a.force:
        _die(f"{card_path.relative_to(ROOT)} 已存在。"
             f"改已有工作流用 `wfctl update --key {key}`；确要覆盖加 --force")

    # ① 图入库
    src = Path(a.graph)
    if not src.is_file():
        _die(f"图文件不存在：{src}")
    try:
        graph = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"图不是合法 JSON：{e}\n"
             "  从 ComfyUI 导出的必须是「API 格式」（Save (API Format) 按钮），"
             "不是普通 Save（UI 格式）—— 后者顶层是 nodes 数组，不是节点对象")
    if not isinstance(graph, dict) or not all(
            isinstance(v, dict) and "class_type" in v for v in graph.values()):
        _die("图不是 ComfyUI API 格式：顶层应是 {节点id: {class_type, inputs}}。\n"
             "  在 ComfyUI 里用「Save (API Format)」导出，不要用普通 Save。")
    dst = GRAPH_DIR / f"{key}.api.json"
    # 记下「本来有没有」—— 失败回滚时只能删本次新建的，
    # 用 --force 覆盖别人已有文件时不许把原件一起删掉。
    existed_graph = dst.is_file()
    existed_card = card_path.is_file()
    dst.write_text(json.dumps(graph, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    _say(f"  ✓ 图已入库：{dst.relative_to(ROOT)}（{len(graph)} 个节点）")

    # 从这里往后任何一步失败都不许留半成品：一张没有卡引用的
    # `workflows/<key>.api.json` 是孤儿文件 —— `p20ctl import` 不认它、
    # `wfctl check` 不查它，下次同 key 重跑还会撞「已存在」。
    # 2026-11-11 实测过：槽位校验拦住时图已经拷进库，得手工 `rm`。
    try:
        return _new_after_graph(a, key, card_path, graph, dst)
    except BaseException:
        for p_, existed in ((dst, existed_graph), (card_path, existed_card)):
            if existed:
                continue                  # 原本就有的不动
            try:
                p_.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _new_after_graph(a, key: str, card_path: Path, graph: dict, dst: Path) -> int:
    """`new` 主流程：图已经落盘之后的部分。抽出来是为了失败时能统一清理。"""
    from tools.p20 import config_schema as _cs

    # ② 模型卡
    if a.model and not (MODEL_DIR / f"{a.model}.json").is_file():
        _die(f"模型卡不存在：registry/models/{a.model}.json\n"
             f"  先建模型卡（含 files 里每个权重的 name/loader/input/match）")

    # ③ 反推参数契约（不手填 bindings）
    from tools.p20 import wf_infer as INF, wf_templates as T
    oi = _object_info()
    inf = INF.infer(graph, oi)
    for w in inf.warnings:
        _say(f"  ! {w}")

    # ③b 槽位 vs 模型卡：早拦，别等 p20ctl import 抛异常
    #
    # 图里每个「权重文件名输入」都会被推成一个槽位，但模型卡不一定覆盖它。
    # 2026-11-11 实测：`video_ltx2_3_flf2v` 会推出 ckpt + text_encoder，
    # 而对应的模型卡只声明了 ckpt —— 于是 import 阶段才报「缺槽位」，
    # 那时图已经拷进库、卡已经写了一半，回滚要手工来。
    for role in [x.strip() for x in (a.drop_slots or "").split(",") if x.strip()]:
        if role in inf.slots:
            inf.slots.pop(role)
            _say(f"  · 按 --drop-slots 放弃槽位 {role}（保留图里的硬编码文件名）")
        else:
            _say(f"  ! --drop-slots 指定了 {role}，但图里没有这个槽位，忽略")

    model_key = a.model or _guess_model_from_slots(inf.slots)
    if model_key:
        mp = MODEL_DIR / f"{model_key}.json"
        if mp.is_file():
            mfiles = set(json.loads(mp.read_text(encoding="utf-8")).get("files") or {})
            lack = sorted(set(inf.slots) - mfiles)
            if lack:
                extra = sorted(mfiles - set(inf.slots))
                _die(
                    f"模型卡 {model_key} 覆盖不了图里推出来的槽位。\n"
                    f"  图要的槽位 : {sorted(inf.slots)}\n"
                    f"  模型卡有   : {sorted(mfiles)}\n"
                    f"  缺         : {lack}\n"
                    f"  卡里多的   : {extra or '（无）'}\n"
                    f"\n  三条路，选一条：\n"
                    f"  a) 换个模型卡：--model <已有覆盖这些槽位的卡>\n"
                    f"     现有卡里覆盖 {sorted(inf.slots)} 的有："
                    f"{_models_covering(set(inf.slots)) or '（没有，见 b/c）'}\n"
                    f"  b) 给 {model_key} 补 files.{lack[0]}（照现有槽位的写法：\n"
                    f"     name/loader/input/match，name 要等于图里那个文件名）\n"
                    f"  c) 这个槽位如果本来就该硬编码在图中（换模型也不动它），\n"
                    f"     加 --drop-slots {','.join(lack)} 明确放弃由 registry 管理")

    # ④ 选骨架：显式 --template 优先，否则按图的输入形态推荐
    has_video = any(p["type"] == "video" for p in inf.params.values())
    has_img = sum(1 for p in inf.params.values() if p["type"] == "image")
    kind = (inf.output or {}).get("kind") or a.kind or "image"
    tpl_key = a.template
    if tpl_key:
        tpl = T.get(tpl_key)
        if not tpl:
            _die(f"没有模板 {tpl_key!r}。可选：{', '.join(T.TEMPLATES)}")
    else:
        rec = T.pick_by_kind(kind, has_video_input=has_video,
                             has_two_images=has_img >= 2, has_image_input=has_img >= 1)
        tpl = T.get(rec[0])
        _say(f"  · 按图形态推荐模板 {rec}，采用 {tpl.key}"
             + (f"（如需换用 --template {'/'.join(rec[1:])}）" if len(rec) > 1 else ""))

    # 模板提供**骨架**（该有哪些参数、说明怎么写），反推提供**真实值**（绑到哪）。
    # 两边都缺的字段直接报错 —— 宁可拦住也不要落一张半成品卡。
    params = _merge_template(tpl, inf, key)

    if not params:
        _die("推不出任何参数。请确认图里有保存节点、采样器、提示词编码节点。")

    # ⑤ 生成卡
    card = {
        "schema": _cs.WORKFLOW_SCHEMA,
        "key": key,
        "name": a.name or f"{tpl.name}（待命名）",
        "version": 1,
        "kind": kind,
        "engine": "comfyui",
        "graph": str(dst.relative_to(ROOT)),
        "model": {"key": a.model or _guess_model_from_slots(inf.slots), "slots": inf.slots},
        "params": params,
        "output": inf.output or {"node": "", "input": "filename_prefix",
                                 "kind": kind, "ext": "mp4" if kind == "video" else "png"},
        "expect_defaults": inf.expect,
        "limits": {"max_cost": 1.6, "max_minutes": 40},
        "required_gpu": ["4090", "4090_48G"],
        "description": a.description or "",
        "notes": f"wfctl new 从图反推（模板 {tpl.key}），未真机验证（compat=experimental）",
    }
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    _say(f"  ✓ 卡已生成：{card_path.relative_to(ROOT)}"
         f"（{len(params)} 个参数 / {len(inf.slots)} 个槽位）")

    # ⑥ 门禁 —— 落盘后立刻查，查完再导入
    _say("\n  门禁：")
    if not _lint(key):
        _die(f"卡没过门禁。改好 registry/workflows/{key}.json 后跑 `wfctl update --key {key}`")

    # ⑦ 导入
    if not _import_and_validate():
        return 1

    _say("")
    _say("下一步：")
    _say(f"  1. 人审三处（机器判不了）：")
    _say(f"     · 正负提示词方向 —— 把 prompt / neg_prompt 的默认值念一遍")
    for s in inf.suggestions:
        if s.confidence != "high":
            _say(f"     · {s.name}：{s.note}")
    _say(f"     · 模型卡 {card['model']['key'] or '（未填！）'} 的 files 是否覆盖"
         f" {sorted(inf.slots)}")
    _say(f"  2. 预演（¥0）：python3 -m tools.p2.asset_preflight --workflow {key}")
    _say(f"  3. 真机：python3 -m tools.p20.p20ctl new --workflow {key} "
         f"--allow-experimental --run")
    _say(f"  4. 转正：python3 -m tools.wfctl verify --key {key} --job-id <job>")
    return 0


def _object_info() -> dict:
    """真机 object_info 快照。没有就返回空 —— 反推退化为「只看图结构」。"""
    p = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-linked.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _models_covering(roles: set[str]) -> list[str]:
    """哪些模型卡的 files 覆盖了这些槽位角色。"""
    hits = []
    for p in sorted(MODEL_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if roles <= set(d.get("files") or {}):
            hits.append(d.get("key", p.stem))
    return hits


def _guess_model_from_slots(slots: dict) -> str:
    """按槽位角色反查最匹配的模型卡 —— 只是建议，人必须确认。"""
    if not slots:
        return ""
    want = set(slots)
    best, best_score = "", -1
    for p in sorted(MODEL_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        have = set((d.get("files") or {}))
        score = len(want & have) - len(want ^ have)
        if score > best_score:
            best, best_score = d.get("key", p.stem), score
    return best


def _merge_template(tpl, inf, key: str) -> dict:
    """把模板骨架和反推结果合成参数契约。

    规则：
      · 模板有的参数 → 用模板的说明/类型/范围（那是给人看的产品定义）
      · 反推有的绑定 → 覆写模板的 REVIEW_SLOTS 占位
      · 只在一侧有的 → 也保留（模板多的说明该参数可选；反推多的说明图里真有）
      · 模板里还剩 REVIEW_SLOTS 的参数 → 直接丢掉并告警
        （宁可少一个参数，也不要落一张绑定到 "REVIEW_SLOTS" 的卡）
    """
    out: dict = {}
    dropped: list[str] = []

    for name, spec in tpl.params.items():
        real = inf.params.get(name)
        merged = dict(spec)
        if real:
            merged["bindings"] = real.get("bindings") or []
            for k in ("default", "values", "min", "max"):
                if k in real and k not in merged:
                    merged[k] = real[k]
            if real.get("required") and not merged.get("required"):
                merged["required"] = True
        # 占位符没被真实绑定替掉 → 丢掉
        binds = merged.get("bindings") or []
        if not binds or any(str(b.get("node", "")).startswith("REVIEW_") for b in binds):
            dropped.append(name)
            continue
        out[name] = merged

    # 反推有、模板没有的参数（例如模型特有的 guidance、image_3）
    for name, spec in inf.params.items():
        if name in out:
            continue
        s = dict(spec)
        s.setdefault("description", f"（自动推断：{name}）**请补一句人话说明**")
        out[name] = s

    if dropped:
        _say(f"  ! 模板里这些参数在图里找不到对应节点，已丢弃：{dropped}")
        _say(f"    （说明这张图和模板 {tpl.key} 的形态不完全一致，"
             f"必要时用 --template 换一个）")
    return out


# ---------------------------------------------------------------- update
def cmd_update(a) -> int:
    card_path = WF_DIR / f"{a.key}.json"
    if not card_path.is_file():
        _die(f"{card_path.relative_to(ROOT)} 不存在。新增用 `wfctl new`")
    key = a.key
    # 自动 +1 版本（配置变了就是新版本，老作业按老版本快照复跑）
    if not a.no_bump:
        d = json.loads(card_path.read_text(encoding="utf-8"))
        old = int(d.get("version") or 1)
        d["version"] = old + 1
        card_path.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        _say(f"  ✓ 版本 {old} → {old + 1}")
    _say(f"  · 门禁 {card_path.relative_to(ROOT)}")
    if not _lint(a.key):
        _die(f"卡没过门禁。改好 registry/workflows/{a.key}.json 再跑 update")
    return 0 if _import_and_validate() else 1


# ---------------------------------------------------------------- verify
def cmd_verify(a) -> int:
    """真机跑完 → 写证据。**这是唯一能把 experimental 变 verified 的入口。**"""
    card_path = WF_DIR / f"{a.key}.json"
    if not card_path.is_file():
        _die(f"工作流不存在：{a.key}")
    d = json.loads(card_path.read_text(encoding="utf-8"))
    model = (d.get("model") or {}).get("key") or ""

    # 转正前必须过门禁 —— 门禁漏的错会被永久写进 compat.json，
    # 之后再没人回头看这张卡。跑完真机更要查，不是跑通了就没事。
    if not _lint(a.key):
        _die(f"卡没过门禁，不许转正。改好 registry/workflows/{a.key}.json 再跑 verify")

    jf = ROOT / "reports" / "p1" / "jobs" / a.job_id / "job.json"
    mf = ROOT / "reports" / "p1" / "jobs" / a.job_id / "manifest.json"
    if not jf.is_file():
        _die(f"作业不存在：{a.job_id}")
    if not mf.is_file():
        _die(f"作业没有 manifest（没跑到 PERSISTED）：{a.job_id}")

    job = json.loads(jf.read_text(encoding="utf-8"))
    man = json.loads(mf.read_text(encoding="utf-8"))
    reg = (man.get("registry") or {})

    # 三条硬校验：不满足就不许标 verified
    if reg.get("workflow_key") != a.key or reg.get("model_key") != model:
        _die(f"作业与工作流对不上：\n"
             f"  作业 meta: {reg.get('workflow_key')} × {reg.get('model_key')}\n"
             f"  当前卡    : {a.key} × {model}")
    if job.get("state") != "DONE":
        _die(f"作业状态是 {job.get('state')}，不是 DONE")
    if man.get("verdict") != "PASS":
        _die(f"manifest verdict 是 {man.get('verdict')}，不是 PASS")

    # 质量分：只是提醒，不阻止（report 模式语义）
    q = man.get("quality") or {}
    if q.get("overall") is not None:
        g = q.get("grade") or "?"
        _say(f"  · 质量分 {q['overall']}（{g}）")
        if g == "D" and not a.allow_low_quality:
            _die(f"质量等级 D（{q['overall']} 分），不建议转正。\n"
                 f"  真要转正加 --allow-low-quality，但请先看 issues：\n"
                 + "\n".join(f"    - {i}" for i in (q.get("artifacts") or [{}])[0].get("issues") or []))

    rc, out = _p20(["compat", "verify", "--workflow", a.key,
                    "--model", model, "--evidence", a.job_id])
    if rc != 0:
        _die(f"写证据失败：\n{out[-800:]}")
    if not _import_and_validate():
        return 1
    _say("")
    _say(f"✓ {a.key} 已转正（证据 {a.job_id}）")
    return 0


# ---------------------------------------------------------------- check
def cmd_check(a) -> int:
    """全量体检，零花费。"""
    ok = True
    _say("① 控制台入口")
    ok &= _guard()

    from tools.p20 import wf_lint as L
    _say(f"\n② 工作流卡门禁（{len(L.RULES)} 条规则，¥0）")
    rep = L.lint_all(only=a.key or "")
    ne, nw = len(rep.errors), len(rep.warnings)
    if ne or nw:
        _say(f"  {len(rep.checked)} 条 · 错误 {ne} · 警告 {nw}")
        for f in rep.findings:
            mark = "✗" if f.level == "ERROR" else "!"
            _say(f"    {mark} [{f.rule}] {f.workflow}.{f.where}: {f.detail}")
        if ne:
            ok = False
    else:
        _say(f"  ✓ 门禁通过（{len(rep.checked)} 条）")

    _say("\n③ registry 配置")
    rc, out = _p20(["validate"], quiet=True)
    warns = [l for l in out.splitlines() if l.startswith("⚠")]
    tail = [l for l in out.splitlines() if l.strip().startswith("工作流")]
    _say("  " + (tail[-1] if tail else out.strip().splitlines()[-1]))
    if rc != 0:
        _say(out[-1200:])
        ok = False
    else:
        _say(f"  ✓ 配置合法（{len(warns)} 条 experimental）")

    _say("\n④ 开机前资产预演（¥0，不建机）")
    try:
        from tools.p2 import asset_preflight as AP
        oi_p = Path(a.object_info) if getattr(a, "object_info", None) else None
        rows = AP.forecast_all(oi=AP.load_object_info(oi_p),
                               weights=AP.load_weights(), only=a.key or "")
        s = AP.summarize(rows)
        _say(f"  {s['total']} 条 · 能过 {s['ok']} · 会失败 {s['would_fail']}"
             f" · 证据不足 {s['unknown']}")
        bad = [r for r in rows if r.status == AP.STATUS_FAIL]
        if bad:
            _say("")
            _say(AP.format_report(bad))
            # 默认不改 ok：多数失败是**已知的**（真缺权重/缺节点，已在
            # 第 ④ 步单独统计）。但查单条时，失败就是失败 —— 那正是
            # "我这条能不能跑"的直接答案，不能含糊。
            if a.key:
                ok = False
            else:
                _say("  （以上是已知阻塞，不影响整体判定；"
                     "查单条时它们会让 check 失败）")
    except Exception as e:                       # noqa: BLE001
        _say(f"  ! 资产预演跳过（{type(e).__name__}: {e}）")

    _say("\n⑤ 环境预检（节点 + 权重，粗筛）")
    try:
        from tools.p2 import preflight as P
        oi = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-v4.json"
        wf_path = ROOT / "reports" / "p2" / "p21" / "evidence" / "weights-full.txt"
        probe = P.Probe()
        if oi.is_file():
            probe = P.Probe.load(object_info=oi)
        if wf_path.is_file():
            loaded = P.Probe.load(weights=wf_path)
            probe.weight_files = loaded.weight_files
            probe.weights_known = loaded.weights_known
            probe.weights_complete = True
        rows = P.check_all(probe=probe, compat_links=P.load_compat_links(),
                           only=a.key or "")
        s = P.summarize(rows)
        _say(f"  {s['total']} 条 · 结构可跑 {s['ready']} · 阻塞 {s['blocked']}")
        for k, v in s["by_cause"].items():
            if v:
                _say(f"    {k}: {v}")
        if a.key and s["blocked"]:
            _say("\n" + P.format_table(rows))
            ok = False
    except Exception as e:                       # noqa: BLE001
        _say(f"  ! 预检跳过（{type(e).__name__}: {e}）")

    _say("")
    _say("✓ 全部通过" if ok else "✗ 有问题，见上")
    return 0 if ok else 1


# ---------------------------------------------------------------- guide
GUIDE = """
新增一条工作流的七步 —— 每一步失败该看哪儿

  ① 拿图
      ComfyUI 里「Save (API Format)」导出，不是普通 Save。
      判据：文件顶层是 {\"节点id\": {\"class_type\": ..., \"inputs\": {...}}}，
      不是 {"nodes": [...]}。
      失败 → JsonDecodeError 或「图不是 API 格式」，重新导出即可。

  ② 落盘 + 反推 + 门禁 + 导入（一条命令跑完）
      python3 -m tools.wfctl new --key my_wf --name "显示名" \\
          --graph /path/to/my_wf.api.json --model <模型key> --template i2v
      不给 --template 时按图的输入形态自动推荐（见下）。

      失败 → 按输出改 registry/workflows/my_wf.json，然后：
      python3 -m tools.wfctl update --key my_wf

  ③ 人审三处（机器判不了，必须自己看）
      a) 正负提示词方向
         把 prompt 和 neg_prompt 的默认值各念一遍，方向反了出片必糊且不报错。
      b) 模型卡
         registry/models/<key>.json 的 files 要覆盖卡里 model.slots 的每个角色。
         缺一个角色 → 渲染时 apply_model 抛「缺槽位」，而那时实例已开好。
      c) 该暴露的参数是否都暴露了
         尤其 frames/length：它必须绑到决定 latent 长度的节点。
         绑到 ImageFromBatch.length 是裁切，改了成片长度不变（2026-11-11 踩过）。

  ④ 预演（¥0，不建机）
      python3 -m tools.p2.asset_preflight --workflow my_wf
      查：图里要的节点在不在镜像里、权重在不在共享库、值在不在候选列表。
      查不到：张量形状 / 显存 / 推理 NaN —— 这三类只能真机跑才知道。

  ⑤ 真机验证（花钱，约 ¥0.13–0.5/次）
      python3 -m tools.p20.p20ctl new --workflow my_wf --allow-experimental --run
      失败 → 作业目录在 reports/p1/jobs/<job_id>/，看 manifest.json 的 issues。

  ⑥ 转正（唯一能写 verified 的入口）
      python3 -m tools.wfctl verify --key my_wf --job-id <job_id>
      硬条件：作业 DONE + manifest PASS + 作业 meta 与卡一致 + 过门禁。
      质量分 D 会拦下来，确要转正加 --allow-low-quality。

  ⑦ 上线
      ./deploy.sh
      会跑 repo-guard → sync_share → 重启 → healthcheck。

模板怎么选

  t2i      text → image                只要提示词
  i2i      image + text → image        拿图改风格/加细节
  t2v      text → video                只给提示词出片
  i2v      image + text → video        首帧推运动（最常用）
  flf2v    image + image_2 + text      首尾帧插值
  v2v      video + image + text        动作迁移 / 整片重绘
  upscale  image → image               超分放大
  inpaint  image (+mask) → image       擦除 / 局部修复

看模板详情：python3 -m tools.p20.wf_templates i2v

门禁规则（历史事故全部转成规则）

  python3 -m tools.p20.wf_lint --rules

零花费的全量体检

  python3 -m tools.wfctl check
"""


def cmd_guide(a) -> int:
    print(GUIDE.strip())
    return 0


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="wfctl",
        description="工作流/模型 变更的唯一入口（新增/修改/验证/体检）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="新增工作流")
    p.add_argument("--key", required=True, help="工作流 key（小写下划线）")
    p.add_argument("--name", required=True, help="中文显示名")
    p.add_argument("--kind", required=True, choices=("image", "video"))
    p.add_argument("--graph", required=True, help="ComfyUI 导出的 API 图路径")
    p.add_argument("--model", required=True, help="模型 key（registry/models 下）")
    p.add_argument("--params", help="（已弃用，参数由图反推；保留仅为兼容）")
    p.add_argument("--template", help="骨架模板：t2i/i2i/t2v/i2v/flf2v/v2v/upscale/inpaint"
                                      "（不给则按图形态推荐）")
    p.add_argument("--drop-slots", dest="drop_slots", default="",
                   help="逗号分隔：这些槽位不由 registry 管理（图里硬编码，换模型也不动）")
    p.add_argument("--description", default="", help="给人看的一句话说明")
    p.add_argument("--force", action="store_true", help="覆盖已存在的卡")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("update", help="改已有工作流（校验 + 导入）")
    p.add_argument("--key", required=True)
    p.add_argument("--no-bump", action="store_true", help="不递增版本号")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("verify", help="真机跑完 → 写兼容性证据（会花钱）")
    p.add_argument("--key", required=True)
    p.add_argument("--job-id", required=True, dest="job_id")
    p.add_argument("--allow-low-quality", action="store_true", dest="allow_low_quality",
                   help="质量 D 也允许转正")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("check", help="全量体检（¥0）")
    p.add_argument("--key", help="只看一条")
    p.add_argument("--object-info", dest="object_info", metavar="FILE",
                   help="真机 object_info 快照（默认用 evidence 里的）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("guide", help="打印新增工作流的七步流程")
    p.set_defaults(func=cmd_guide)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
