#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑 9 条 ready 的 experimental 工作流（P4 真机验证）。

## 为什么需要这个脚本

这 9 条工作流在 registry 里都是 `experimental`（从没在真机上跑过），
预检（`tools/p2/preflight.py` + 镜像快照）判定它们的权重和节点都齐了，
所以具备跑通的条件 —— 缺的只是「真跑一次并把结果记成兼容性证据」。

手动一条条敲 9 次容易出错（参数名写错、素材配错、重复花钱），所以集中在这里：
参数表是**声明式**的，每条作业的素材/提示词都写死，跑之前可以 `--dry-run` 全部预演。

## 素材从哪来

`reports/p2/p21/inputs/` 里已有的图/视频（按内容哈希命名）。这些是之前
P2.1 连测留下的素材，实测可用：

    in-a6891d7b.png   1024x1024  苹果静物
    in-ff45f4b6.png    768x1024  竹林和服人像
    in-d9b9cba6.png   1620x1080  溪边白衣人像
    in-5935a044.png   1024x1024
    in-867e2ac9.png    768x1024
    in-22a23224-t4-16fps.mp4  720x1280 16fps 4s  （scail2 的 video 输入）

注意 `in-*.jpg` 那 7 张是 **1×1 占位图**（实测），不能当素材用。

## 成本

约 ¥1.64（3 image × ¥0.102 + 6 video × ¥0.222，全部来自实测）。
失败重试另算，最坏全失败 ¥0.78（建机+setup 的固定开销）。

## 用法

    python3 tools/p20/run_experimental.py --dry-run     # 只预演，¥0
    python3 tools/p20/run_experimental.py               # 真跑
    python3 tools/p20/run_experimental.py --only pack_image_qwen_image_layered
    python3 tools/p20/run_experimental.py --stop-after 1  # 跑 1 条先看看
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

P20CTL = ROOT / "tools" / "p20" / "p20ctl.py"
INPUTS = ROOT / "reports" / "p2" / "p21" / "inputs"

# 素材（按内容哈希命名，改素材要同时改这里的注释）
APPLE = "in-a6891d7b.png"          # 1024x1024 苹果静物
GIRL_BAMBOO = "in-ff45f4b6.png"    # 768x1024 竹林和服人像
GIRL_CREEK = "in-d9b9cba6.png"     # 1620x1080 溪边白衣人像
SQ_A = "in-5935a044.png"           # 1024x1024
SQ_B = "in-867e2ac9.png"           # 768x1024
VID_720 = "in-22a23224-t4-16fps.mp4"   # 720x1280 16fps 4s

# 九条作业的完整参数。参数名必须与 registry 里的 schema 完全一致 ——
# 写错一个字母就会在 resolve 阶段被拒（不会花钱，但白等一次）。
#
# 提示词按各工作流的默认值调整：默认值往往指向"手提包/角色定义"这类
# 与原素材无关的内容，直接照搬会让产物对不上输入图，
# 那样即使跑通也说明不了这个工作流真的可用。
JOBS: list[dict] = [
    {
        "workflow": "pack_image_flux2_klein_image_edit_4b_distilled",
        "note": "图生图编辑：两张参考图 + 提示词",
        "params": {
            "image": APPLE, "image_2": APPLE,
            "prompt": "stylize the apple with warm golden light",
            "neg_prompt": "blurry, low quality",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_image_qwen_image_edit_2511_int8",
        # 2026-11-10：原始 graph 从 ComfyUI 导出时丢了必填输入
        # （TextEncodeQwenImageEditPlus 缺 prompt、
        #   FluxKontextMultiReferenceLatentMethod 缺 reference_latents_method、
        #   SaveImageAdvanced 的 format 子字段没展开），已在
        # workflows/pack/image_qwen_image_edit_2511_int8.api.json 补好，
        # registry 也补了 prompt / neg_prompt 两个参数。
        "note": "图生图编辑：一张参考图 + 编辑指令",
        "params": {
            "image": GIRL_BAMBOO,
            # 别写"水彩/油画"这类风格词：质量判据按边缘能量打分，
            # 会把它当成"糊"判 D（实测 soft watercolor 拿 32/D）。
            # 中性描述 + 保留细节，才能既验工作流又不被风格扣分。
            "prompt": "same person, same pose, detailed kimono fabric, "
                      "sharp facial features, natural bamboo forest lighting",
            "neg_prompt": "blurry, low quality",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_image_qwen_image_layered",
        "note": "分层输出（RGBA 多图层）",
        "params": {
            "image": APPLE,
            "prompt": "a ripe red apple on a wooden table by the window",
            "neg_prompt": "blurry, low quality",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_video_fasth3_ominiref_r2v",
        "note": "参考图生视频（omniref）",
        # 这个工作流按「多参考图」建模（ref_image_0 / ref_image_1 都要），
        # 少给一张会让 MiniMaxH3ReferenceToVideo 的 latent 尺寸错配：
        # 实测报 "tensor a (24) must match tensor b (768)"。
        "params": {
            "image": GIRL_BAMBOO,
            "image_2": GIRL_CREEK,
            "prompt": "subject_definitions:\n<Subject 1> is the woman in <Picture 1>: "
                      "a young woman in a cream kimono standing in a bamboo forest.\n"
                      "<Subject 2> is the setting in <Picture 2>: a woman sitting by a "
                      "quiet creek at golden hour.\n\nsummary: The camera slowly "
                      "pushes in as she turns her head toward the light.",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_video_ltx2_3_flf2v",
        "note": "首尾帧生视频：两张图必须不同（同图等于静止）",
        "params": {
            "image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
            "prompt": "The camera moves from the bamboo forest to a quiet "
                      "stream at golden hour.",
            "neg_prompt": "blurry, out of focus, overexposed",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_video_ltx2_5_flf2v",
        "note": "首尾帧生视频（2.5 版），schema 无 prompt / seed",
        "params": {
            "image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
            "neg_prompt": "blurry, out of focus, overexposed",
        },
    },
    {
        "workflow": "pack_video_minimax_h3_r2v",
        "note": "两图参考生视频",
        "params": {
            "image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
            "prompt": "A young woman walks from the bamboo grove toward the creek.",
            "seed": 20261110,
        },
    },
    {
        "workflow": "pack_video_wan21_scail2_character_replacement",
        "note": "角色替换：video 输入 + 一张替换角色的图",
        "params": {
            "image": VID_720, "image_2": GIRL_CREEK,
            "prompt": "A young woman with dark hair, white dress, walking outdoors",
            "neg_prompt": "A young woman with dark hair",
        },
    },
    {
        "workflow": "pack_video_wan21_scail2_character_replacement_int8",
        "note": "角色替换 int8 版，多一个 prompt_2 / prompt_3",
        "params": {
            "image": VID_720, "image_2": GIRL_CREEK,
            "prompt": "girl, white dress",
            "prompt_2": "girl, white dress",
            "prompt_3": "A young East Asian woman with long dark hair, wearing a "
                        "white dress, walking in natural light",
            "neg_prompt": "blurry, low quality",
        },
    },
]


def preflight_params(job: dict) -> list[str]:
    """跑之前检查素材存在、参数名与 schema 对得上、必填项齐全。

    这一步是 ¥0 的，但它能挡掉 90% 的"跑到一半失败"——
    比如素材名写错会在 step_submit 上传时才炸，那时机器已经建完了。
    """
    from tools.p20 import registry

    errs: list[str] = []
    wf = registry.get_workflow(job["workflow"])
    schema = wf.get("params") or {}
    params = job["params"]

    # ① 参数名必须在 schema 里
    for name in params:
        if name not in schema:
            errs.append(f"参数 {name!r} 不在 schema 里（可用：{sorted(schema)}）")

    # ② 必填项齐全
    for name, spec in schema.items():
        if isinstance(spec, dict) and spec.get("required") and name not in params:
            errs.append(f"缺必填参数 {name!r}")

    # ③ 媒体文件必须真实存在且不是 1×1 占位图
    for name, val in params.items():
        spec = schema.get(name) or {}
        if spec.get("type") not in ("image", "video"):
            continue
        p = INPUTS / str(val)
        if not p.is_file():
            errs.append(f"{name}={val} 文件不存在")
            continue
        if p.stat().st_size < 200:
            errs.append(f"{name}={val} 只有 {p.stat().st_size} 字节，疑似占位图")
    return errs


JOBCTL = ROOT / "tools" / "p1" / "jobctl.py"
SPEC_DIR = ROOT / "reports" / "p20" / "specs"


def make_spec(job: dict) -> tuple[Path, str]:
    """把一条作业的参数解析成 spec 文件，返回 (spec 路径, job_id 前缀信息)。

    为什么不走 `p20ctl new --params`：那个参数要的是**文件路径**，
    而我们的参数里带换行和多行提示词，塞进 `--set` 更难读。
    为什么不走 `webui.api.create_job`：它会主动拒绝 experimental
    （见 create_job 里那两处 409），而这批作业**全部是** experimental ——
    跑它们正是为了把实验态变成已验证态。
    所以直接调 `registry.resolve(allow_experimental=True)` + `_make_spec`，
    这才是 create_job 内部真正在用的那两步。
    """
    from tools.p20 import p20ctl as p20cli
    from tools.p20 import registry

    res = registry.resolve(job["workflow"], params=job["params"],
                           allow_experimental=True)
    spec = p20cli._make_spec(res, job_key=job["workflow"], task_ref="P2.1-实验转正")
    # 实验态作业默认按短任务跑（跑完立即回收，不空转）——
    # 这几条都是 3~6 分钟的短作业，给空转窗口纯属浪费。
    from tools.p2 import lifecycle
    tc, src = lifecycle.normalize_task_class("short")
    spec.update(task_class=tc, task_class_source=src)
    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    path = SPEC_DIR / f"exp-{job['workflow']}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, f"{job['workflow']} → {res['workflow']['key']} v{res['workflow']['version']} × {res['model']['key']}"


def build(job: dict, *, dry_run: bool, keep_on_fail: bool = False,
          force: bool = False) -> dict:
    """生成 spec → 建作业 →（可选）执行。"""
    try:
        spec_path, desc = make_spec(job)
    except Exception as e:                       # noqa: BLE001
        return {"workflow": job["workflow"], "rc": 1,
                "out": f"resolve/spec 失败：{type(e).__name__}: {e}"}

    cmd = [sys.executable, str(JOBCTL), "new", "--spec", str(spec_path),
           "--task", "P2.1-实验转正"]
    # 修工作流时要重跑同一组参数 —— 不去重就会拿到"同参数作业已完成"的拒绝。
    # 这里的重跑是有意的：参数没变，但**图变了**，这次跑是在验新图。
    if force:
        cmd.append("--force")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=600)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        return {"workflow": job["workflow"], "rc": p.returncode, "out": out[-2000:]}

    # 取最后一个 job_id（jobctl 可能先打印"同参数旧作业"的历史 id）
    import re
    found = re.findall(r"p1-\d{8}T\d{4}-[0-9a-f]{6}", out)
    if not found:
        return {"workflow": job["workflow"], "rc": 1,
                "out": f"建作业成功但取不到 job_id：{out[-600:]}"}
    jid = found[-1]

    if dry_run:
        return {"workflow": job["workflow"], "rc": 0, "job_id": jid,
                "out": f"{desc}\n（--dry-run：已建作业 {jid}，未执行、未建机）"}

    log = ROOT / "reports" / "p1" / "jobs" / jid / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(ROOT / "tools" / "p1" / "run.py"), jid,
           "--batch", "--wait-lock", "14400"]
    # 实验态作业第一次跑，失败是常态（400 / 缺节点 / 参数越界）。
    # 默认 **--no-keep**：失败即销毁，不烧钱。
    # 但 --keep-on-fail 时不传它 —— 实例保留 15 分钟（run.py 的 KEEP_MINUTES，
    # 有定时关机兜底），可以 SSH 上去看 ComfyUI 现场：
    #   400 的 node_errors 有时候不够，需要看 /root/ComfyUI/*.log 或者手动重放 graph。
    # 踩过：第一版一律 --no-keep，400 失败后实例秒销毁，
    # 只剩一句 "HTTP Error 400"，现场全丢，只能再花 ¥0.10 重来。
    if not keep_on_fail:
        cmd.append("--no-keep")
    with log.open("ab") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(ROOT))
    return {"workflow": job["workflow"], "rc": 0, "job_id": jid,
            "out": f"{desc}\n已启动 {jid}（日志 {log.relative_to(ROOT)}）"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑 9 条 ready 的 experimental 工作流")
    ap.add_argument("--dry-run", action="store_true", help="只预演参数，不建机不花钱")
    ap.add_argument("--only", help="只跑这一个 workflow_key")
    ap.add_argument("--skip", action="append", default=[], help="跳过某个 workflow_key")
    ap.add_argument("--stop-after", type=int, default=0, help="跑 N 条就停（先验一条用）")
    ap.add_argument("--yes", action="store_true", help="跳过花费确认")
    ap.add_argument("--keep-on-fail", action="store_true",
                    help="失败时保留实例 15 分钟（排查 400 用；有定时关机兜底）")
    ap.add_argument("--force", action="store_true",
                    help="同参数已有完成作业也强制重跑（改了 graph 要验新图时用）")
    a = ap.parse_args(argv)

    jobs = JOBS
    if a.only:
        jobs = [j for j in jobs if j["workflow"] == a.only]
        if not jobs:
            print(f"✗ 没有这个工作流：{a.only}", file=sys.stderr)
            return 1
    if a.skip:
        jobs = [j for j in jobs if j["workflow"] not in a.skip]
    if a.stop_after > 0:
        jobs = jobs[:a.stop_after]

    # ---- 预检（¥0）----
    print(f"预检 {len(jobs)} 条作业的参数与素材…\n")
    bad = 0
    for j in jobs:
        errs = preflight_params(j)
        mark = "✓" if not errs else "✗"
        print(f"  {mark} {j['workflow']}")
        print(f"      {j['note']}")
        for e in errs:
            print(f"      ! {e}")
            bad += 1
    if bad:
        print(f"\n✗ {bad} 个问题，先修再跑（这一轮没花钱）", file=sys.stderr)
        return 1
    print(f"\n✓ 参数与素材全部就绪")

    if a.dry_run:
        print("\n--dry-run：以下作业会被创建，但不执行（¥0）\n")
        for j in jobs:
            print(f"  {j['workflow']}")
        print(f"\n真跑预计花费 ¥{3 * 0.102 + 6 * 0.222:.2f}（按实测单价）")
        return 0

    est = 0.0
    for j in jobs:
        est += 0.102 if j["workflow"].startswith("pack_image_") else 0.222
    print(f"\n即将真跑 {len(jobs)} 条，预计 ¥{est:.2f}（余额请自行确认 ≥ ¥5）")
    if not a.yes:
        try:
            ans = input("继续？[y/N] ").strip().lower()
        except EOFError:
            print("非交互环境，加 --yes 确认", file=sys.stderr)
            return 1
        if ans not in ("y", "yes"):
            print("已取消，¥0")
            return 0

    print()
    results = []
    for i, j in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {j['workflow']}")
        r = build(j, dry_run=False, keep_on_fail=a.keep_on_fail, force=a.force)
        results.append(r)
        status = "✓" if r["rc"] == 0 else "✗"
        print(f"  {status} rc={r['rc']}")
        for line in r["out"].strip().splitlines()[-6:]:
            print(f"    {line}")

    ok = sum(1 for r in results if r["rc"] == 0)
    print(f"\n{'='*60}")
    print(f"完成 {ok}/{len(results)}")
    for r in results:
        print(f"  {'✓' if r['rc'] == 0 else '✗'} {r['workflow']}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
