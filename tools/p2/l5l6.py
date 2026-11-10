#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L5/L6 运维 CLI：多云能力、资产缓存、跨云一致性、质量评分、工作流预检。

    python3 -m tools.p2.l5l6 providers [--describe NAME] [--matrix]
    python3 -m tools.p2.l5l6 assets    [--model KEY] [--index] [--evict-plan GB]
    python3 -m tools.p2.l5l6 conform   [--probe FILE] [--object-info FILE] [--verbose]
    python3 -m tools.p2.l5l6 quality   FILE [FILE...] [--kind video|image] [--gate 60]
    python3 -m tools.p2.l5l6 preflight [--object-info FILE] [--weights FILE] [--drift]

全部只读、零花费、可离线。唯一例外是 `conform --collect HOST`（要连 SSH）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ------------------------------------------------------------------ providers
def cmd_providers(a) -> int:
    from tools.p2 import providers as P
    if a.describe:
        print(P.describe(a.describe))
        return 0
    if a.matrix:
        print(json.dumps(P.capability_matrix(), ensure_ascii=False, indent=1))
        return 0
    print("provider 能力对照（声明值，不是实测值）\n")
    for row in P.capability_matrix():
        if "error" in row:
            print(f"  {row['provider']:12s} 不可用：{row['error']}")
            continue
        print("  " + _fmt_caps(row))
    print("\n提示：capabilities() 是**声明**。发现与实测不符时改适配器，"
          "不要在调用处绕开。")
    return 0


def _fmt_caps(row: dict) -> str:
    def b(v):
        return {True: "✓", False: "✗", None: "?"}.get(v, str(v))
    return (f"{row['name']:12s} 停机={b(row['can_stop_instance'])}"
            f" 外网={b(row['instance_has_internet'])}"
            f" OCI={b(row['can_pull_oci'])}"
            f" 共享权重={b(row['has_shared_weight_mount'])}"
            f" 定时关机={b(row['supports_scheduled_stop'])}"
            f" 竞价={b(row['supports_spot'])}"
            f" 自定义镜像={b(row['supports_custom_image'])}"
            f" 免费系统盘={row['free_system_disk_gb']}GB")


# ------------------------------------------------------------------ assets
def cmd_assets(a) -> int:
    from tools.p2 import assets as A
    from tools.p20 import registry as R

    if a.index:
        d = A.load_index()
        ents = d.get("entries") or {}
        print(f"资产索引：{len(ents)} 条 · 可删占用 {A.disk_usage(ents) / 1e9:.2f} GB")
        print(f"  来源：{A.INDEX_FILE}")
        print(f"  更新时间：{d.get('updated_at') or '—'}")
        if ents:
            print()
            print(f"  {'name':48s} {'size':>10s} {'source':>8s} {'evict':>6s}")
            for n, e in sorted(ents.items())[:60]:
                print(f"  {n[:48]:48s} {(e.get('size') or 0) / 1e9:>9.2f}G "
                      f"{e.get('source') or '?':>8s} "
                      f"{'yes' if e.get('evictable') else 'no':>6s}")
        return 0

    if a.model:
        model = R.get_model(a.model)
        res = A.resolve(model, available={}, can_link=True, can_download=True)
        s = A.summarize(res)
        print(f"模型 {model.get('key')} 需要 {s['total']} 个文件")
        for r in res:
            mark = {"hit": "✓", "linkable": "→", "download": "↓", "missing": "✗"}[r.status]
            print(f"  {mark} {r.status:9s} {r.subdir:18s} {r.name}"
                  + (f"   [{r.reason}]" if r.reason and r.status != "linkable" else ""))
        if s["needs_download"]:
            print(f"\n  需下载 {len(s['needs_download'])} 个：")
            for t in A.download_targets(res):
                print(f"    {t['url'] or '(无 url)'} → {t['dest']}")
        print(f"\n  结论：{'可执行' if s['ok'] else '不可执行'}")
        for b in s["blocked_by"]:
            print(f"    阻塞：{b}")
        return 0

    if a.evict_plan is not None:
        d = A.load_index()
        ents = d.get("entries") or {}
        limit = int(a.evict_plan * 1e9)
        used = A.disk_usage(ents)
        plan = A.evict_plan(ents, usage_bytes=used, limit_bytes=limit)
        print(f"当前可删占用 {used / 1e9:.2f} GB · 上限 {a.evict_plan:.2f} GB")
        if not plan["files"]:
            print("无需淘汰")
            return 0
        print(f"需释放 {plan['need'] / 1e9:.2f} GB，计划删 {len(plan['files'])} 个：")
        for f in plan["files"]:
            print(f"  {f['name'][:52]:52s} {f['size'] / 1e9:>8.2f}G  {f['path']}")
        print(f"\n淘汰后 {plan['after'] / 1e9:.2f} GB"
              f"（{'达标' if plan['reached'] else '⚠ 删光可删项仍不达标'}）")
        if not plan["reached"]:
            print("  需要扩大磁盘或减少并发作业 —— 不要删共享挂载（那不是我们的数据）")
        if a.apply:
            n = 0
            for f in plan["files"]:
                p = Path(f["path"])
                try:
                    p.unlink()
                    n += 1
                except OSError as e:
                    print(f"  删除失败 {p}: {e}")
            A.prune_index(ents, [f["path"] for f in plan["files"]])
            A.save_index(d)
            print(f"已删除 {n} 个文件并清理索引")
        else:
            print("（演练模式，加 --apply 才真删）")
        return 0

    # 默认：概览 + 证据覆盖率
    avail: dict = {}
    if a.evidence:
        avail = dict(A.load_compat_links())
        for extra in (a.evidence or []):
            w = Path(extra)
            if not w.is_file():
                continue
            for line in w.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("/"):
                    b = Path(line).name
                    avail.setdefault(b, A.AssetHit(name=b, path=line, source="mount"))

    models = []
    for wf in R.list_workflows():
        mk = (wf.get("model") or {}).get("key")
        if not mk:
            continue
        try:
            models.append(R.get_model(mk))
        except Exception:
            continue
    cov = A.evidence_coverage(models, avail) if avail else None

    d = A.load_index()
    ents = d.get("entries") or {}
    print(f"资产索引：{A.INDEX_FILE}")
    print(f"  条目 {len(ents)} · 可删占用 {A.disk_usage(ents) / 1e9:.2f} GB")
    if cov:
        print(f"\n证据覆盖率：{cov['known']}/{cov['required']} 项权重"
              f"（{cov['coverage']:.0%}），来源 {cov['sources']}")
        print(f"  未盘点的 {cov['unknown']} 项：既不能确认存在也不能确认缺失")
        if cov["coverage"] < 1.0:
            print("  ⚠ 现有证据是抽样（compat_links 30 行 + 缺口调查 95 行），"
                  "不是 /model 40TB 的全量盘点")
            print("     要拿到 100% 覆盖率，在 GPU 机上跑一次完整 find（见 --scan-hint）")
    else:
        print("  未给 --evidence，无法算覆盖率")
    if not ents:
        print("  索引为空。在 GPU 机上跑 --scan 后才有数据。")
    print("\n用法：--model KEY 看单个模型要哪些文件；--evict-plan GB 看淘汰计划")
    if a.scan_hint:
        print("\n完整盘点命令（在 GPU 机上跑，把输出存成 weights.txt）：")
        print("  find /model -maxdepth 8 -type f \\( -name '*.safetensors' "
              "-o -name '*.ckpt' -o -name '*.pth' -o -name '*.pt' -o -name '*.gguf' \\) "
              "-printf '%p\\n' | sort > weights.txt")
        print("  然后：python3 -m tools.p2.l5l6 preflight --object-info OI.json "
              "--weights weights.txt")
    return 0


# ------------------------------------------------------------------ conform
def cmd_conform(a) -> int:
    from tools.p2 import conformance as C

    manifest = C.load_manifest(a.manifest)

    if a.collect:
        # 真机采集：需要 sshpass
        import subprocess
        host, _, port = a.collect.partition(":")
        port = port or "23"
        pwd = a.password or ""
        cmd = ["sshpass", "-p", pwd, "ssh", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-p", port, f"root@{host}",
               "bash -s"]
        r = subprocess.run(cmd, input=C.collect_script(comfy_home=a.comfy_home),
                           capture_output=True, text=True, timeout=300)
        try:
            probe = C.parse_probe(r.stdout or "")
        except ValueError as e:
            print(f"✗ 采集失败：{e}", file=sys.stderr)
            print((r.stderr or "")[-500:], file=sys.stderr)
            return 2
        if a.save_probe:
            p = C.snapshot_probe(probe, a.save_probe)
            print(f"probe 已保存：{p}")
    elif a.probe:
        probe = C.load_probe(a.probe)
    else:
        print("需要 --probe FILE（离线复放）或 --collect HOST（真机采集）", file=sys.stderr)
        return 2

    res = C.check(manifest, probe)
    print(C.format_report(res, verbose=a.verbose))
    return 0 if res["ok"] else 1


# ------------------------------------------------------------------ quality
def cmd_quality(a) -> int:
    from tools.p2 import quality as Q

    mode = "gate" if a.gate is not None or a.min_grade else "report"
    arts = []
    for f in a.files:
        p = Path(f)
        s = Q.assess(p, kind=a.kind, mode=mode,
                     min_score=float(a.gate or 0), min_grade=a.min_grade or "")
        arts.append(s)
        flag = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(s.grade, "⚪")
        print(f"{flag} {s.score:5.1f} {s.grade}  {p.name}")
        m = s.metrics
        if m and "error" not in m:
            print(f"        清晰 {m.get('sharpness')} · 运动 {m.get('motion')} · "
                  f"亮度 {m.get('luma_mean')} · 范围 {m.get('luma_range')} · "
                  f"变异 {m.get('sharpness_cv')}")
        for i in s.issues:
            print(f"        ! {i}")
        if mode == "gate":
            print(f"        门禁：{'通过' if s.passed else '不通过'}")
    if a.json:
        print(json.dumps([s.as_dict() for s in arts], ensure_ascii=False, indent=1))
    bad = [s for s in arts if not s.passed]
    return 1 if (mode == "gate" and bad) else 0


# ------------------------------------------------------------------ preflight
def cmd_preflight(a) -> int:
    from tools.p2 import preflight as P

    probe = P.Probe()
    if a.object_info:
        probe = P.Probe.load(object_info=a.object_info)
    if a.weights:
        loaded = P.Probe.load(weights=a.weights, weights_complete=not a.sampled)
        probe.weight_files = loaded.weight_files
        probe.weights_known = loaded.weights_known
        probe.weights_complete = loaded.weights_complete
    if not a.object_info and not a.weights:
        print("⚠ 没有给 --object-info / --weights：只能做图结构检查，"
              "节点与权重两类问题查不出来。\n")

    # compat_links 是「名字 → 真实路径」的已确认映射，必须一起喂给预检，
    # 否则 qwen_image_t2i 这类 verified 会被误报成缺权重（见 load_compat_links 注释）
    links = P.load_compat_links()
    rows = P.check_all(probe=probe, only=a.filter or "", compat_links=links)
    s = P.summarize(rows)
    print(f"工作流 {s['total']} 条 · 可执行 {s['ready']} · 阻塞 {s['blocked']}"
          f"（已加载 {len(links)} 条 compat_links 映射）")
    for k, v in s["by_cause"].items():
        if v:
            print(f"  {k}: {v}")
    if s["unverified_weights"]:
        print(f"  （{s['unverified_weights']} 条有未确认存在的权重，权重清单是抽样）")

    if a.drift:
        d = P.drift_report(rows)
        print(f"\n环境漂移：{len(d)} 条（registry 说 verified，当前环境说跑不了）")
        for x in d:
            print(f"  ✗ {x['workflow']} × {x['model']}")
            print(f"      registry={x['registry_says']} env={x['env_says']}：{x['reason']}")
            if x["fix_hint"]:
                print(f"      修法：{x['fix_hint']}")
        if not d:
            print("  没有漂移 —— registry 的 verified 与当前环境一致")
        return 0 if not d else 1

    blocked = [r for r in rows if not r.ok]
    if blocked:
        print()
        print(P.format_table(rows))
    if a.json:
        print(json.dumps({"summary": s, "rows": [r.as_dict() for r in rows]},
                         ensure_ascii=False, indent=1))
    return 0 if not blocked else 1


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="l5l6", description="L5 多云与资产 / L6 质量与预检")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("providers", help="多云能力对照")
    p.add_argument("--describe", metavar="NAME", help="单家能力摘要")
    p.add_argument("--matrix", action="store_true", help="输出 JSON 矩阵")
    p.set_defaults(func=cmd_providers)

    p = sub.add_parser("assets", help="资产缓存与跨云获取")
    p.add_argument("--model", metavar="KEY", help="解析一个模型要哪些文件")
    p.add_argument("--index", action="store_true", help="看索引内容")
    p.add_argument("--evict-plan", type=float, metavar="GB", help="按上限算淘汰计划")
    p.add_argument("--apply", action="store_true", help="真的删（默认只演练）")
    p.add_argument("--evidence", nargs="*", metavar="FILE",
                   help="额外的权重路径清单（一行一个）。总是自动加载 compat_links.txt")
    p.add_argument("--scan-hint", action="store_true", help="打印完整盘点命令")
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("conform", help="跨云一致性校验")
    p.add_argument("--manifest", help="环境描述（默认 deploy/env/comfyui-wan-p21.env.json）")
    p.add_argument("--probe", metavar="FILE", help="离线复放：已有的 probe JSON")
    p.add_argument("--collect", metavar="HOST[:PORT]", help="真机采集")
    p.add_argument("--password", help="SSH 密码（配合 --collect）")
    p.add_argument("--comfy-home", default="/root/ComfyUI")
    p.add_argument("--save-probe", metavar="FILE", help="把采集结果存成证据")
    p.add_argument("--verbose", action="store_true", help="显示 INFO 项")
    p.set_defaults(func=cmd_conform)

    p = sub.add_parser("quality", help="产物质量评分")
    p.add_argument("files", nargs="+")
    p.add_argument("--kind", choices=("video", "image"), default="video")
    p.add_argument("--gate", type=float, metavar="SCORE", help="门禁分下限（给了就开启门禁）")
    p.add_argument("--min-grade", choices=("A", "B", "C", "D"), help="门禁等级下限")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_quality)

    p = sub.add_parser("preflight", help="工作流预检（不花钱）")
    p.add_argument("--object-info", metavar="FILE", help="/object_info 快照")
    p.add_argument("--weights", metavar="FILE", help="权重路径清单（一行一个）")
    p.add_argument("--sampled", action="store_true",
                   help="权重清单是抽样：未命中只算『未确认』不算缺失")
    p.add_argument("--drift", action="store_true", help="只看环境漂移")
    p.add_argument("--filter", metavar="SUBSTR", help="只看 key 含该子串的")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preflight)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
