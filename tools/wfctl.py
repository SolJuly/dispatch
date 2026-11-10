#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流/模型 变更的统一入口：**只有这一条路，不能绕**。

## 为什么需要

工作流由四样东西构成，它们**必须一起变**：

    graph     workflows/<key>.api.json        节点图
    params    registry/workflows/<key>.json   参数契约（类型/默认/绑定）
    compat    真机证据                         能不能跑
    index     registry/registry.db            派生索引（可重建）

历史上出过的事故，全部来自"只改了一部分"：

  · 图里要 `image_2`，params 里没这个键 → 上传不上去
  · 标了 verified，但镜像里缺节点 → 开了机才报错（¥0.13/次）
  · 改了 params 默认值，忘了 `p20ctl import` → UI 还是旧的
  · 手改 `compat.json` 把 experimental 写成 verified → 没有证据

所以把动作收敛成 4 个动词，每个都保证四样东西一致：

    wfctl new      新增工作流（生成卡 + 校验 + 导入）
    wfctl update   改已有工作流（改完自动校验 + 导入）
    wfctl verify   真机跑完 → 写兼容性证据（唯一能标 verified 的入口）
    wfctl check    全量体检（配置 + 守卫 + 预检，不花钱）

## 设计原则

**能自动做的不要让人记。** `update` 改完自动 `validate` + `import`，
不需要你记得先跑什么后跑什么。

**不能自动的要说清代价。** `verify` 要建机花钱，所以它不会在
`update` 里被自动触发 —— 但会明确告诉你"这条现在还是 experimental"。

**失败要说人话。** 每条错误都带"该改哪个文件的哪一段"。

## 用法

    # 新增：从 ComfyUI 导出的 API 图开始
    python3 -m tools.wfctl new \\
        --key my_new_wf --name "我的新工作流" --kind video \\
        --graph /path/to/exported.api.json \\
        --model ltx_2_3_22b_dev_fp8 \\
        --params '{"prompt": {"type":"string","required":true}}'

    # 改：直接编辑 registry/*.json 后跑这个
    python3 -m tools.wfctl update --key my_new_wf

    # 真机验证并转正（会花钱）
    python3 -m tools.wfctl verify --key my_new_wf --job-id p1-...

    # 体检（¥0）
    python3 -m tools.wfctl check
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
    key = a.key
    # 前置校验：key 规则写在 config_schema，这里提前拦，免得图都拷进去了才报错
    from tools.p20 import config_schema as _cs
    if not _cs.KEY_RE.match(str(key)):
        _die(f"key 非法：{key!r}\n"
             f"  规则：小写字母开头，只含小写字母/数字/下划线，2–64 字符\n"
             f"  例：pack_video_ltx2_3_i2v、qwen_image_t2i")
    card_path = WF_DIR / f"{key}.json"
    if card_path.exists() and not a.force:
        _die(f"{card_path.relative_to(ROOT)} 已存在。"
             f"改已有工作流用 `wfctl update --key {key}`；确要覆盖加 --force")

    # ① 图：从外部拷贝进 workflows/，保证 graph 路径在库内
    src = Path(a.graph)
    if not src.is_file():
        _die(f"图文件不存在：{src}")
    dst = GRAPH_DIR / f"{key}.api.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _say(f"  ✓ 图已入库：{dst.relative_to(ROOT)}")

    # ② 模型卡必须存在
    if not (MODEL_DIR / f"{a.model}.json").is_file():
        _die(f"模型卡不存在：registry/models/{a.model}.json\n"
             f"  先建模型卡（含 files 里每个权重的 name/loader/input/match）")

    # ③ 参数契约
    try:
        params = json.loads(a.params) if a.params else {}
    except json.JSONDecodeError as e:
        _die(f"--params 不是合法 JSON：{e}")
    if not isinstance(params, dict) or not params:
        _die("--params 不能为空：至少要有 prompt 之类的必填项")
    _fill_bindings(params, dst)
    _say(f"  ✓ 参数绑定已补全（{len(params)} 个参数）")

    # ④ 生成卡
    from tools.p20 import config_schema as cs
    card = {
        "schema": cs.WORKFLOW_SCHEMA,
        "key": key,
        "name": a.name,
        "version": 1,
        "kind": a.kind,
        "engine": "comfyui",
        "graph": str(dst.relative_to(ROOT)),
        "model": {"key": a.model, "slots": _slots_from_graph(dst, a.model)},
        "params": params,
        "output": _guess_output(dst, a.kind),
        "expect_defaults": {
            "ext": "mp4" if a.kind == "video" else "png",
            "min_bytes": 50000 if a.kind == "video" else 20000,
        },
        "limits": {"max_cost": 1.6, "max_minutes": 40},
        "required_gpu": ["4090", "4090_48G"],
        "description": a.description or "",
        "notes": "新建（未真机验证，compat=experimental）",
    }
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    _say(f"  ✓ 卡已生成：{card_path.relative_to(ROOT)}")

    if not _import_and_validate():
        return 1

    _say("")
    _say(f"下一步：")
    _say(f"  1. 检查生成的卡（尤其 model.slots 与 params.bindings，是自动推断的）")
    _say(f"  2. 预检：python3 -m tools.wfctl check --key {key}")
    _say(f"  3. 真机：python3 -m tools.p20.p20ctl new --workflow {key} "
         f"--allow-experimental --run")
    _say(f"  4. 转正：python3 -m tools.wfctl verify --key {key} --job-id <job>")
    return 0


def _fill_bindings(params: dict, graph_path: Path) -> None:
    """给每个参数补 bindings（指到图里的节点/输入）。

    为什么不让人手填：bindings 是 `{node: "节点id", input: "输入名"}`，
    而节点 id 是 ComfyUI 导出时随机生成的数字串。手填必然出错，
    错了的后果是渲染时 `_set()` 报"指向不存在的节点"，那时作业已经建好了。

    推断规则（按可信度排序）：
      1. 参数名本身是图里某个输入名 → 直接绑（prompt/seed/steps/width…）
      2. 参数名是已知别名 → 按别名表找（length→length/frames、image→image）
      3. 都不是 → 报错并列出图里所有可绑的输入，让人明确指定
    """
    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    # 图里所有 `{input名: [(节点id, class_type), ...]}`，跳过连线（连线是数组）
    candidates: dict[str, list[tuple[str, str]]] = {}
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        for inp, val in (node.get("inputs") or {}).items():
            # 连线是 ["12", 0] 这种两元数组；常量才是标量/字符串
            if isinstance(val, list) and len(val) == 2 and isinstance(val[1], int):
                continue
            candidates.setdefault(inp, []).append((str(nid), ct))

    # 别名表：参数名 ≠ 输入名时用
    ALIAS = {
        "prompt": ("text", "prompt", "positive"),
        "neg_prompt": ("negative", "neg_prompt"),
        "seed": ("seed", "noise_seed"),
        "steps": ("steps",),
        "cfg": ("cfg",),
        "width": ("width",),
        "height": ("height",),
        "frames": ("length", "num_frames", "frames"),
        "length": ("length", "num_frames"),
        "image": ("image",),
        "image_2": ("image",),
        "mask": ("mask",),
        "video": ("video",),
        "audio": ("audio",),
    }

    missing = []
    for name, spec in params.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("bindings"):
            continue                      # 用户已显式给了，尊重
        if spec.get("virtual"):
            continue                      # 虚拟参数不绑节点
        keys = [name] + list(ALIAS.get(name, ()))
        hit = None
        for k in keys:
            if k in candidates:
                # 同名多个节点时优先 sampler/encode 类（避免绑到无关节点）
                prefer = ("KSampler", "CLIPTextEncode", "EmptyLatentImage",
                          "EmptySD3LatentImage", "EmptyLTXVLatentVideo",
                          "LoadImage", "LoadVideo", "LoadAudio")
                ranked = sorted(candidates[k],
                                key=lambda x: 0 if x[1] in prefer else 1)
                hit = ranked[0]
                break
        if not hit:
            missing.append(name)
            continue
        spec["bindings"] = [{"node": hit[0], "input": keys[keys.index(
            next(k for k in keys if k in candidates))]}]

    if missing:
        avail = ", ".join(sorted(candidates)[:25])
        _die(
            "这些参数在图里找不到可绑的输入：" + ", ".join(missing) + "\n"
            "  请在 --params 里显式给出 bindings，例如：\n"
            '    {"my_param": {"type": "string", "required": true,\n'
            '                  "bindings": [{"node": "5", "input": "text"}]}}\n'
            f"  图里可绑的输入有：{avail}")


def _slots_from_graph(graph_path: Path, model_key: str) -> dict:
    """从图和模型卡推断 model.slots（角色 → 节点/输入）。

    不猜：模型卡的 files 已经写明每个角色的 loader/input，
    这里只要在图上找到"同一个 input 名"的节点即可。
    """
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"图不是合法 JSON：{e}")
    mp = MODEL_DIR / f"{model_key}.json"
    model = json.loads(mp.read_text(encoding="utf-8"))

    # 按「input 名 + 图里的当前值」双重匹配。
    #
    # 只用 input 名会撞车：一条工作流常有两个 LoRA（主 LoRA + 文本编码器 LoRA），
    # 它们的 input 都叫 `lora_name`，只按名字找会让两个角色绑到同一个节点，
    # 而另一个节点永远没人填 —— 渲染时不会报错（值都填上了），
    # 但实际用的是错的那一个 LoRA。这种错误在成片里看不出来。
    #
    # 模型卡的 files.<role>.name 就是"这个角色应该填的文件名"，
    # 拿它去图里找当前值相同的节点，能唯一确定。
    slots: dict[str, dict] = {}
    used: set[str] = set()                 # 一个节点只能服务一个角色
    for role, f in (model.get("files") or {}).items():
        want_input = (f or {}).get("input")
        want_name = str((f or {}).get("name") or "")
        if not want_input:
            continue
        exact, loose = None, None
        for nid, node in graph.items():
            if not isinstance(node, dict) or str(nid) in used:
                continue
            ins = node.get("inputs") or {}
            if want_input not in ins:
                continue
            val = str(ins.get(want_input))
            if want_name and val.endswith(want_name):
                exact = (str(nid), want_input)      # 当前值就是它
                break
            if loose is None:
                loose = (str(nid), want_input)      # 名字对得上，值还没对上
        hit = exact or loose
        if hit:
            slots[role] = {"node": hit[0], "input": hit[1]}
            used.add(hit[0])
    if not slots:
        _die("无法从图推断 model.slots：模型卡的 files 里没有与图匹配的 input。\n"
             "  请手填卡的 model.slots，格式 {\"角色\": {\"node\": \"节点id\", \"input\": \"输入名\"}}")
    return slots


def _guess_output(graph_path: Path, kind: str) -> dict:
    """找保存节点（SaveImage / VHS_VideoCombine 等）当输出。"""
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for nid, node in graph.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "")
        if ct in ("SaveImage", "PreviewImage"):
            return {"node": str(nid), "input": "filename_prefix",
                    "kind": "image", "ext": "png", "pick": ["images"]}
        if ct in ("VHS_VideoCombine", "SaveVideo", "SaveAnimatedWEBP"):
            return {"node": str(nid), "input": "filename_prefix",
                    "kind": "video", "ext": "mp4", "pick": ["videos", "gifs"]}
    _die("图里找不到保存节点（SaveImage / VHS_VideoCombine 等），无法确定 output")


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
    _say(f"  · 校验 {card_path.relative_to(ROOT)}")
    return 0 if _import_and_validate() else 1


# ---------------------------------------------------------------- verify
def cmd_verify(a) -> int:
    """真机跑完 → 写证据。**这是唯一能把 experimental 变 verified 的入口。**"""
    card_path = WF_DIR / f"{a.key}.json"
    if not card_path.is_file():
        _die(f"工作流不存在：{a.key}")
    d = json.loads(card_path.read_text(encoding="utf-8"))
    model = (d.get("model") or {}).get("key") or ""

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

    _say("\n② registry 配置")
    rc, out = _p20(["validate"], quiet=True)
    warns = [l for l in out.splitlines() if l.startswith("⚠")]
    tail = [l for l in out.splitlines() if l.strip().startswith("工作流")]
    _say("  " + (tail[-1] if tail else out.strip().splitlines()[-1]))
    if rc != 0:
        _say(out[-1200:])
        ok = False
    else:
        _say(f"  ✓ 配置合法（{len(warns)} 条 experimental）")

    _say("\n③ 环境预检（节点 + 权重）")
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
    p.add_argument("--params", help="参数契约 JSON 字符串")
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
    p.set_defaults(func=cmd_check)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
