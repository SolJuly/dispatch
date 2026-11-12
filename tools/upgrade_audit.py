#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像升级审计：拿两份 `object_info` 快照，算清「升上去会坏哪几条」。

    python3 -m tools.upgrade_audit diff \\
        --base reports/p2/p21/evidence/objectinfo-v4.json \\
        --target reports/p20/evidence/oi-0450.json

    python3 -m tools.upgrade_audit audit \\
        --target reports/p20/evidence/oi-0450.json --verbose

    python3 -m tools.upgrade_audit diff --base A.json --target B.json --json

## 为什么要做成工具，而不是一份报告

镜像升级这件事我们做过三次（v3→v4、v4→0.43、v4→0.45 论证），**每次都从零
重新推一遍**：哪些节点没了、哪些签名变了、我们 54 条工作流里哪条会死。
2026-11-12 那次是靠临时脚本算的，脚本没留下来 —— 下次升级还得再写一遍，
而且没人能保证两次的判据一致。

判据不一致的代价是真实的：如果第一次算"安全"而第二次算"不安全"，
决策就取决于当时用了哪个脚本，而不是取决于镜像到底变了什么。

所以把它固化。这个工具只做**离线比对**，不建机、不花钱、不联网。

## 判据分四档，按危害排序

    LOST      节点类在新镜像里**没了** → 用到它的工作流必死
    OUTPUT    output 数量变少 / 顺序变了 → 索引越界的连线必死
    INPUT     必填输入新增或改名 → 图里没写该字段的必死（400 required_input_missing）
    ADDED     只是多了/少了一个可选输入、多了节点 → 无害

前三档会失败，第四档不会。**混在一起统计就等于没说**：
"变了 200 处"听起来很严重，实际可能全是新增节点。

## 为什么不看 `INPUT_TYPES` 源码

因为我们没有源码 —— 只有运行时快照。`object_info` 是 ComfyUI 自己对
`/object_info` 的返回，它就是服务端**此刻**认为的输入契约。签名比对
没有比它更权威的来源，而且它正是提交时校验用的那一份。

## 一个刻意的保守选择

`INPUT` 判据只在「新增的必填项没有默认值」时才判失败。有默认值的必填项
ComfyUI 服务端会补（`graph_check` 的 `has_default` 分支同理），
按"缺失"处理会把能跑的工作流报死 —— 2026-11-10 那一版就是这么误报 53 处的。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 默认基线：v4 真机快照（1757 节点）。换基线要显式传 --base。
DEFAULT_BASE = ROOT / "reports/p2/p21/evidence/objectinfo-v4.json"

SEVERITY = {"LOST": 3, "OUTPUT": 2, "INPUT": 1, "ADDED": 0}
SEVERITY_CN = {"LOST": "节点没了", "OUTPUT": "输出变了",
               "INPUT": "必填输入变了", "ADDED": "仅新增/可选变化"}


# ------------------------------------------------------------------ 读快照
def load_oi(path: Path | str) -> dict:
    """读 object_info 快照。容忍 `{"object_info": {...}}` 包一层和裸 dict。

    缺文件时给**人话**报错而不是 traceback —— 这个工具的用法是
    `--target 新采的快照`，最常见的错误就是路径打错或忘了先采集。
    一个 FileNotFoundError 的栈对"我路径写错了"这个事实毫无增益。
    """
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"✗ 快照不存在：{p}\n"
                         f"  先在一台目标镜像的实例上采集：\n"
                         f"    curl -s http://127.0.0.1:8188/object_info > {p}")
    d = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("object_info"), dict):
        return d["object_info"]
    if not isinstance(d, dict):
        raise SystemExit(f"✗ {path} 不是 object_info（顶层不是对象）")
    return d


def _inputs(spec: dict, group: str) -> dict:
    return ((spec.get("input") or {}).get(group) or {})


def _type_name(fspec) -> str:
    """输入的类型名。COMBO 的 spec[0] 是候选列表，返回 `COMBO`。"""
    if not isinstance(fspec, list) or not fspec:
        return ""
    t = fspec[0]
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "COMBO"
    return ""


def _has_default(fspec) -> bool:
    """必填项带默认值 = 服务端会补，不算缺失。"""
    if not isinstance(fspec, list) or len(fspec) < 2:
        return False
    meta = fspec[1]
    return isinstance(meta, dict) and "default" in meta


def _outputs(spec: dict) -> list[str]:
    out = spec.get("output")
    if isinstance(out, list):
        return [str(x) for x in out]
    return []


# ------------------------------------------------------------------ 差异
@dataclass
class Finding:
    kind: str                 # LOST / OUTPUT / INPUT / ADDED
    class_type: str
    detail: str = ""
    # 需要这个节点类的工作流（只有 audit 阶段才填）
    workflows: list[str] = field(default_factory=list)

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 0)


@dataclass
class Diff:
    base_n: int = 0
    target_n: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def lost(self) -> list[str]:
        return sorted({f.class_type for f in self.findings if f.kind == "LOST"})

    @property
    def breaking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity > 0]

    @property
    def safe(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == 0]

    def by_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def diff_oi(base: dict, target: dict) -> Diff:
    """两份 object_info 的**契约**差异。

    只比对我们可能用到的部分（输入/输出签名），不比 `description`、
    `category`、`python_module` 这类元数据 —— 它们变了不影响作业。
    """
    d = Diff(base_n=len(base), target_n=len(target))
    for ct in sorted(set(base) | set(target)):
        a, b = base.get(ct), target.get(ct)
        if a is None:
            d.findings.append(Finding("ADDED", ct, "新镜像新增节点类（无害）"))
            continue
        if b is None:
            d.findings.append(Finding("LOST", ct, "新镜像里没有这个节点类"))
            continue

        # ---- 输出：数量变少或**前缀变了**才危险 ----
        #
        # 判定要分三种，混成一种就会误报（2026-11-12 实测踩到）：
        #
        #   ① ob 比 oa 短                → 真的坏（索引越界）
        #   ② ob 前 len(oa) 项与 oa 一致 → **纯追加，无害**
        #       0.43.0 的 `TextGenerate` / `TextGenerateLTX2Prompt` 就是这类：
        #       [STRING] → [STRING, STRING]。第一版把它判成"输出变了"，
        #       报了 5 处破坏性变更、6 条工作流受影响 —— 而这 6 条
        #       真机上全部跑通（全部只用 idx 0）。误报的代价是"因为假警报
        #       放弃一次本来可用的升级"，比漏报更难发现。
        #   ③ 前缀不一致（顺序/类型错位）  → 危险，idx 可能指向另一个类型
        oa, ob = _outputs(a), _outputs(b)
        if len(ob) < len(oa):
            d.findings.append(Finding(
                "OUTPUT", ct, f"输出 {len(oa)} → {len(ob)}："
                              f"{oa} → {ob}。引用索引 ≥{len(ob)} 的连线会越界"))
        elif oa and ob[:len(oa)] == oa and len(ob) > len(oa):
            d.findings.append(Finding(
                "ADDED", ct,
                f"输出纯追加 {oa} → {ob}（原有索引的含义不变，无害）"))
        elif oa != ob:
            d.findings.append(Finding(
                "OUTPUT", ct,
                f"输出顺序/类型变了：{oa} → {ob}（数量相同，引用索引可能错位）"))

        # ---- 输入：新增/移除的必填项 ----
        ra, rb = _inputs(a, "required"), _inputs(b, "required")
        added = [k for k in rb if k not in ra]
        removed = [k for k in ra if k not in rb]
        # 新增且**没有默认值** = 图里没写就 400。有默认值的服务端会补，放过。
        hard_added = [k for k in added if not _has_default(rb[k])]
        if hard_added:
            d.findings.append(Finding(
                "INPUT", ct,
                "新增必填项（无默认值）：" + ", ".join(
                    f"{k}:{_type_name(rb[k])}" for k in hard_added)))
        # 移除必填项通常无害（图里多写的字段会被忽略），但值得记录
        if removed:
            d.findings.append(Finding(
                "ADDED", ct, "移除必填项（图里多写会被忽略）：" + ", ".join(removed)))
        if added and not hard_added:
            d.findings.append(Finding(
                "ADDED", ct, "新增可选/带默认值的项：" + ", ".join(
                    f"{k}:{_type_name(rb[k])}" for k in added)))

        # ---- 类型变化：同一字段的类型改了 ----
        for k in rb:
            if k not in ra:
                continue
            ta, tb = _type_name(ra[k]), _type_name(rb[k])
            if ta and tb and ta != tb:
                d.findings.append(Finding(
                    "INPUT", ct, f"字段 {k} 类型 {ta} → {tb}（值可能不再合法）"))
    d.findings.sort(key=lambda f: (-f.severity, f.class_type))
    return d


# ------------------------------------------------------------------ 影响面
def workflow_classes(folder: Path | None = None) -> dict[str, set[str]]:
    """{工作流 key: 它用到的节点类集合}。

    直接读 registry 的图文件，不连 DB —— 升级审计要在**任何**状态下都能跑，
    包括 registry.db 还没重建的时候。
    """
    from tools.p20 import registry as R
    out: dict[str, set[str]] = {}
    for row in R.list_workflows():
        key = str(row.get("key") or "")
        if not key:
            continue
        try:
            wf = R.get_workflow(key)
        except Exception:                              # noqa: BLE001
            continue
        graph = wf.get("graph_obj")
        if not isinstance(graph, dict):
            # list_workflows() 不返回 graph_obj —— 必须 get_workflow() 再取。
            # 这条曾经让我第一次扫描报"15 条全部就绪"（空 dict 静默通过）。
            continue
        cls = {str(n.get("class_type")) for n in graph.values()
               if isinstance(n, dict) and n.get("class_type")}
        out[key] = cls
    return out


def attach_workflows(d: Diff, wf_classes: dict[str, set[str]]) -> dict[str, list[str]]:
    """把每条发现映射到「哪些工作流会受影响」。返回 {class_type: [wf...]}。

    只映射会失败的档（LOST/OUTPUT/INPUT）。ADDED 档映射出来只是噪音。
    """
    hit: dict[str, list[str]] = {}
    for f in d.findings:
        if f.severity <= 0:
            continue
        wfs = sorted(k for k, cls in wf_classes.items() if f.class_type in cls)
        f.workflows = wfs
        if wfs:
            hit[f.class_type] = wfs
    return hit


# ------------------------------------------------------------------ 输出
def format_diff(d: Diff, *, verbose: bool = False) -> str:
    lines = [f"基线 {d.base_n} 节点 → 目标 {d.target_n} 节点"
             f"（净 {d.target_n - d.base_n:+d}）", ""]
    for kind in ("LOST", "OUTPUT", "INPUT", "ADDED"):
        fs = d.by_kind(kind)
        if not fs:
            continue
        head = f"[{kind}] {SEVERITY_CN[kind]} · {len(fs)} 处"
        lines.append(head)
        show = fs
        for f in show:
            w = f" ⚠ {len(f.workflows)} 条工作流用到" if f.workflows else ""
            lines.append(f"  · {f.class_type}: {f.detail}{w}")
        lines.append("")
    if not d.breaking:
        lines.append("✓ 没有会破坏现有工作流的变更")
    else:
        n_lost, n_out, n_in = (len(d.by_kind("LOST")), len(d.by_kind("OUTPUT")),
                               len(d.by_kind("INPUT")))
        lines.append(f"✗ 破坏性变更 {len(d.breaking)} 处"
                     f"（节点没了 {n_lost} · 输出变了 {n_out} · 输入变了 {n_in}）")
    return "\n".join(lines)


def format_impact(d: Diff, wf_classes: dict[str, set[str]]) -> str:
    """按**工作流**汇总影响面 —— 决策看的是这个，不是节点数。"""
    bad: dict[str, list[str]] = {}
    for f in d.findings:
        if f.severity <= 0:
            continue
        for k, cls in wf_classes.items():
            if f.class_type in cls:
                bad.setdefault(k, []).append(f.class_type)
    if not bad:
        return f"✓ 我们的 {len(wf_classes)} 条工作流全部不受影响"
    lines = [f"✗ {len(bad)}/{len(wf_classes)} 条工作流受影响："]
    for k in sorted(bad):
        cls = sorted(set(bad[k]))
        lines.append(f"  · {k}: {', '.join(cls[:4])}"
                     f"{'…' if len(cls) > 4 else ''}")
    ok = len(wf_classes) - len(bad)
    lines.append(f"  （其余 {ok} 条不受影响）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 对比两套镜像


def audit(base_path: Path | str, target_path: Path | str,
          *, wf_classes: dict[str, set[str]] | None = None) -> Diff:
    d = diff_oi(load_oi(base_path), load_oi(target_path))
    attach_workflows(d, wf_classes if wf_classes is not None else workflow_classes())
    return d


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="upgrade_audit",
                                 description="镜像升级审计（离线，零花费）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("diff", help="比两份 object_info 快照")
    p.add_argument("--base", default=str(DEFAULT_BASE), help="基线快照")
    p.add_argument("--target", required=True, help="目标快照")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.add_argument("--no-workflows", action="store_true",
                   help="不映射到工作流（只比节点契约）")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("audit", help="只看破坏性变更 + 影响的工作流")
    p.add_argument("--base", default=str(DEFAULT_BASE))
    p.add_argument("--target", required=True)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_audit)

    a = ap.parse_args(argv)
    return a.func(a)


def cmd_diff(a) -> int:
    wfc = {} if a.no_workflows else workflow_classes()
    d = audit(a.base, a.target, wf_classes=wfc)
    if a.as_json:
        print(json.dumps({
            "base": a.base, "target": a.target,
            "base_nodes": d.base_n, "target_nodes": d.target_n,
            "breaking": [{"kind": f.kind, "class": f.class_type,
                          "detail": f.detail, "workflows": f.workflows}
                         for f in d.breaking],
            "safe_count": len(d.safe),
        }, ensure_ascii=False, indent=2))
        return 0
    print(format_diff(d, verbose=a.verbose))
    if wfc:
        print()
        print(format_impact(d, wfc))
    return 0 if not d.breaking else 1


def cmd_audit(a) -> int:
    # 注意别写成 `audit(a.target, a.target)` —— 那会自比自，永远"零变更"。
    # 第一版就是这么写的，跑 v4→0.43 报了"1818 → 1818 全部不受影响"，
    # 而真实基线是 1757。自比自是这类工具最容易犯又最难发现的错。
    wfc = workflow_classes()
    d = audit(a.base, a.target, wf_classes=wfc)
    print(format_diff(d, verbose=a.verbose))
    print()
    print(format_impact(d, wfc))
    return 0 if not d.breaking else 1


if __name__ == "__main__":
    raise SystemExit(main())
