#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""质量判定层：在「文件层面 PASS」之外，再回答「这一条到底值不值这个钱」。

## 为什么需要（审计 §1 L6 的唯一缺口）

现有 `verify.py` 的七项全是**技术有效性**判据：

    decode / geometry / frames / size / black / freeze / params

七项全 PASS 只证明「这是一个能播放、尺寸对、不是黑屏的 MP4」。
它完全抓不住这些情况：

  · 画面有内容但整体糊成一片（边缘能量 0.6，七项全过）
  · 动作迁移跑完了，但人物根本没动（freeze 门禁只在 expect.freeze 出现时才生效）
  · 出图是纯色块/抽象噪点（LTXV 失败样本就是这么过的）
  · 提示词理解错了方向（出来的是完全无关的画面）

审计原文：「**没有质量判定（只有"文件成功"，没有"质量成功"）**」。

## 这一层怎么做

**只做可复算、可复现、不需要模型的东西。** 不引入 CLIP/审美模型，因为：

  · 那些模型的分数不可解释，阈值只能拍脑袋
  · 换云换卡后同一张图的审美分数会变（不同 torch/CUDA 的浮点差异），
    「换云结果一致」这个目标会被打分器自己破坏
  · 更重要的是：**对用户来说"糊"和"没动"是能说清的**，用不着审美模型

所以判据是三类**内容统计量**，每个都有明确的物理含义：

    sharpness   拉普拉斯边缘能量均值  → 糊 / 抽象 / 噪点
    motion      相邻帧内容差（YDIF）   → 有没有真的动（视频）
    exposure    亮度均值与动态范围      → 全白/全黑/过曝/发灰
    texture     边缘能量的时间稳定性   → 闪烁 / 抖帧（视频）

## 与 verify.py 的关系

**不改 verify.py 的 PASS/FAIL。** 质量分是**附加信息**，落进 manifest 的
`quality` 字段，也可以按 `--quality-gate` 升级成门禁。理由：

  现存的 30 条 verified 工作流都是按七项判的。把质量分变成默认门禁，
  等于把这 30 条全部重判一次，其中必然有几条会被打低 —— 那需要人一条条看，
  不是一次代码改动能决定的。

  所以默认 `mode="report"`（只记录），门禁需要显式打开。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 阈值
# **全部由真实样本标定，不是抄来的经验值。**
#
# 标定语料：仓库内 31 条真实产物/素材（h3 I2V/T2V、wan i2v、ltxv 失败样本、
# seedvr2 放大、动作迁移驱动片），逐条算一维指标后排序。
# 复算命令见 tools/p2/tests/test_quality.py 的注释。
#
# 实测分布（sharpness / motion / luma_mean）：
#
#   v4-wan-i2v.mp4         6.32 / 19.84 / 129.7   ← 最佳
#   h3 系列                1.28–2.40 / 3.0–21 / 78–150
#   wan_i2v.mp4            0.93 / 14.13 / 191.4   ← 合法成功样本（低细节内容）
#   seedvr2-video-1080.mp4 0.90 /  2.31 /  99.0   ← 合法放大结果
#   ltxv attempt2.mp4      0.56 /  2.98 /  51.8   ← 已知失败样本（抽象噪点）
#
# 关键教训：**1.0 这条线会把两条合法成果判成糊的。**
# verify.py 的 WARN_EDGE=1.0 是「只抽 3 帧」的口径（h3 I2V 在那里是 2.40，
# 全片采样是 1.41），两个数不可混用。这里按全片采样重新定线到 0.8。
SHARP_BLUR = 0.8        # 边缘能量 < 0.8 → 缺细节（实测只命中已知失败样本）
SHARP_GOOD = 2.5        # > 2.5 → 细节充足（对齐 h3 正常区间上沿）
# ---- 运动阈值（2026-11-14 按 every=1 全帧重标）----
# **旧阈值（0.6 / 2.0）是 every=3 抽帧下标的，与全帧不可混用。**
#
# YDIF 量的是「相邻**被采样**帧的差异」。跳帧采样等于把时间跨度放大 3 倍，
# 于是同一段动作被算成 3 倍的运动量。实测同一条片（wan2_2_s2v，说话+轻微点头）：
#
#     every=1  motion=1.123   samples=157   ← 真实运动量
#     every=2  motion=1.661
#     every=3  motion=1.970   samples=53    ← 旧评分用的就是这个
#     every=4  motion=2.200
#
# 后果：旧评分系统**系统性高估运动 75%**，于是"几乎不动"的片子也能拿到
# 运动项的大半分数。这就是 s2v 旧分 97.5、全帧重算只有 82.4 的原因 ——
# 不是新评分变严了，是旧分算错了。
#
# 新阈值按全帧分布定（18 条真实产物）：
#   静止片（animate2_distilled）  0.10
#   s2v 说话点头                  1.12
#   正常动作片                    2.5–7.0
#   强运动（fasth3）              9.9
#
# 全部 18 条的全帧运动量（升序）：
#     0.16  animate2_distilled   ← 蒸馏版，几乎不动
#     0.56  h3_t2v               ← 人眼:极差（还说"镜头推近"却没什么动）
#     0.99  ltxv_2b_t2v
#     1.12  s2v                  ← 说话+轻微点头（合理的小动作）
#     2.55  h3_i2v
#     2.63  ltx2_5_flf2v
#     3.21–3.49  ltx/i2v 系
#     4.36  fasth3_i2v
#     6.41  wan21_i2v_480p
#     7.26  wan2_2_flf2v
#    13.0–19.3  animate2 / scail2 / minimax（人物动作片）
#
# 定 0.5 / 2.5：
#   0.5 → 低于它 = 基本静止。只命中 animate2_distilled(0.16) 和 h3_t2v(0.56)，
#         后者人眼判定极差，静正是它的问题之一 —— 这条线抓对了。
#   2.5 → 达到它 = 有明确运动。s2v(1.12) 只拿 0.31 的分数，符合"轻微动作
#         不该和人物动作片拿一样的运动分"的直觉。
MOTION_STATIC = 0.5
MOTION_GOOD = 2.5
LUMA_LOW = 26           # 亮度均值 < 26 → 过暗（黑电平 16 + 余量）
LUMA_HIGH = 232         # 亮度均值 > 232 → 过曝
RANGE_MIN = 28          # YMAX-YMIN < 28 → 发灰/低对比
FLICKER_CV = 0.85       # 边缘能量变异系数 > 0.85 → 闪烁/抖帧（实测最高 0.42）

# ---- 噪点判别（2026-11-09 新增）----
# 为什么加：**纯噪声视频在原来的评分里拿 100 分 A 级。**
# `noise=alls=120` 生成的片子边缘能量 95.16（正常片 0.9–6.3），清晰度项直接满分；
# 运动项满分（每帧都在变）；曝光项满分（灰度填满 0–255）。
# 但这片子是纯雪花。**「清晰度」这个指标分不清「细节」和「噪点」** ——
# 两者在高频能量上是同一件事，区别在于**结构**：噪点没有结构，细节有。
#
# 判别方法：先 gblur 再算边缘能量，看保留多少。
#   有结构的边缘 → 模糊后塌得慢（实测真实素材保留 28–55%）
#   噪点          → 模糊后几乎全塌（实测 10–15%）
# 实测数据（2026-11-09，13 条真实素材 + 8 条合成噪声）：
#
#   syn-noise/n40/n80/grain     10.7–15.0%   ← 合成噪点，全在这里
#   v4-wan-i2v                  28.3%        ← 最低的真实素材（高细节、强运动）
#   video-10s / 00-primary      34–39%
#   h3 / wan / seedvr2 系列     44–55%
#
# 线定在 20%：离噪点最高值（15.0%）留 5 个点，离真实最低值（28.3%）留 8 个点，
# 两边都不贴边。不能定 25% —— 会把 v4-wan-i2v（28.3%，最好的样本）判成噪点。
NOISE_KEEP = 20.0       # 模糊后边缘能量保留率 < 20% → 判为噪点/无结构

# ---- 内容覆盖率（2026-11-14 新增）----
# 为什么必须加：**全局平均边缘能量掩盖了「画面大部分是空的」这个失败。**
#
# 实测证据（h3_t2v_121f，人工看"质量非常差"的那条，旧评分给 32 分 D）：
# 把画面切成 6×6 块分别算边缘能量：
#
#     0.23 0.26 0.37 0.59 0.36 0.19
#     0.18 0.19 0.53 1.02 0.31 0.19
#     0.14 0.17 0.74 1.27 0.50 0.27
#     0.11 0.20 0.83 1.19 0.38 0.22
#     0.15 0.18 0.86 0.79 0.35 0.23
#     0.20 0.19 0.75 0.79 0.35 0.25
#
#   → 36 块里 31 块低于 0.8（**86% 的画面什么都没画**），只有中间一列有内容
#   → 全体均值 0.43，但「有内容的块」均值是 1.03 —— 主体其实是清楚的
#
# 对照 fasth3_i2v（97 分 A，同一提示词）：
#
#     3.74 1.14 1.14 3.29 3.47 1.74
#     ... 36 块全部 >= 0.8，低能量块 0%
#
# 这就是人眼说"差"、而旧评分只说"32 分 D"的原因：**32 分听上去像"稍微糊"，
# 实际是"八成画面是黑块"。** 全局均值把两件事平均成了一个模糊的数字。
#
# 判据：低能量块（< COVER_BLOCK_MIN）占全部块的比例 = 无内容率。
COVER_BLOCK_MIN = 0.8   # 单块边缘能量低于此值 → 该块判为"没画东西"

# ---- 分块清晰度的独立标定（2026-11-14）----
# **不能复用全局的 SHARP_BLUR/SHARP_GOOD。**
# 分块后每块只覆盖 1/36 面积，块内细节密度天然更高：
# 实测同一批 18 条片子，全局 sharp 与 content_mean 差 20%–80%，
# 直接复用全局阈值会把 9 条从 B 抬到 A（旧 A 只有 4 条）。
# 标定语料：18 条真实产物（含 2 条人眼判定"极差"的、3 条人眼判定"好"的）。
#
#     content_mean 实测分布（升序）：
#       0.000  ltxv_2b_t2v              ← 人眼:极差（整片无结构）
#       0.975  ltx2_5_flf2v
#       1.033  h3_t2v_121f              ← 人眼:极差（86% 画面是空块）
#       1.067  scail2_int8
#       1.268  seedvr2_upscale_video
#       1.545  wan2_2_i2v               ← 人眼:好
#       1.704  h3_i2v                   ← 人眼:好
#       2.454  fasth3_i2v               ← 人眼:最好
#       4.336  wan2_2_s2v               ← 人眼:最好
#
# 两条"极差"的 content_mean 分别是 0.0 与 1.03 —— **1.03 混在正常值中间**。
# 这说明 content_mean 单独不足以判差（h3_t2v 的主体确实清楚），
# 它的失败必须靠 cover_bad=0.86 那条判据表达。所以：
#   · COVER_BAD 才是"极差"的主判据（已实现）
#   · content_mean 只管"主体本身糊不糊"，阈值按真实分布定在 1.0 / 2.0
# 阈值怎么定的（关键：**不能用"达到就满分"的线性映射**）
#
# 第一版定 1.0 / 2.0 之后，A 级从 4 条涨到 8 条 —— 因为 content_mean 天然比
# 全局 sharp 高，而 `_clip01((x-1.0)/1.0)` 在 2.0 就给满 25 分，
# 等于给 8 条片子都发了满分清晰度。这不是标定，是把尺子放松了。
#
# 真实分布（18 条样本 content_mean）：
#   中位 ≈ 1.32    上四分位 ≈ 1.67    最高 4.34
# 而"人眼好"的三条是 1.545 / 1.704 / 2.454 / 4.336
#
# 所以把满分线定到 **2.5**（接近"人眼好"里偏上的位置），
# 下限定 1.0（低于它主体确实糊）。这样：
#   content 1.0 → 0 分     content 1.75 → 0.5     content 2.5+ → 满分
# 原来那些 B 级片（content 1.5–1.7）只拿一半清晰度分，不会被抬进 A。
CONTENT_BLUR = 1.0
CONTENT_GOOD = 2.5
COVER_BAD = 0.50        # 无内容率 > 50% → 画面过半是空的（硬伤）
COVER_OK = 0.15         # 无内容率 <= 15% → 正常（留 15% 容忍天空/水面/纯色背景）

# ---- 逐帧最差（2026-11-14 新增）----
# 为什么加：旧评分只看**均值**，一个 3 秒的好镜头 + 2 秒的崩坏帧，
# 均值仍然漂亮。人眼恰恰对"闪过一帧鬼脸"最敏感。
#
# 实测：h3_t2v 逐帧边缘能量 min=0.413 / max=0.660，CV=0.148 —— 它的问题是
# **每一帧都低**（100% 低于 0.8），不是偶发。所以这条对它是"确认"而非"新增"。
# 但对「大部分帧正常、少数帧崩」的片子，它是唯一能抓到的判据。
WORST_FRAME_PCTL = 10   # 取最差 10% 的帧算均值（仅作诊断指标，不参与封顶）
WORST_RATIO = 0.45      # 最差10%均值 / 全体均值 < 0.45 → 诊断用，不封顶
BROKEN_FRAME_RATIO = 0.15   # 单帧边缘能量 < 中位数 × 此值 → 该帧崩坏
# 崩坏帧判据用**绝对帧数**而不是占比（2026-11-14 再改一次）：
# minimax_multiframe 那条的末尾 2 帧是整屏纯蓝，占比只有 4.8%（42 帧里的 2 帧），
# 用 8% 占比阈值就放过去了 —— 但人眼一眼就看见。
# 占比阈值在短视频上天生失灵：5 秒的片子只有 120 帧，2 帧坏 = 1.7%，
# 而 1 帧纯蓝在 24fps 下已经可见 42ms，观众会看成"闪了一下"。
# 所以：**只要崩坏帧 >= 2 帧就判硬伤**，1 帧容忍（编码边缘情况）。
BROKEN_FRAME_MIN = 2

_R_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([\d.]+)")
_R_YMIN = re.compile(r"lavfi\.signalstats\.YMIN=([\d.]+)")
_R_YMAX = re.compile(r"lavfi\.signalstats\.YMAX=([\d.]+)")
_R_YDIF = re.compile(r"lavfi\.signalstats\.YDIF=([\d.]+)")


# ---------------------------------------------------------------- 数据模型
@dataclass
class QualityScore:
    """一条产物的质量评分卡。"""

    file: str
    kind: str = "video"           # video / image
    score: float = 0.0            # 0-100
    grade: str = "?"              # A / B / C / D
    metrics: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    gated: bool = False           # 是否参与了门禁（决定 pass/fail）
    passed: bool = True           # 门禁结论；report 模式下恒 True

    def as_dict(self) -> dict:
        return {"file": self.file, "kind": self.kind, "score": round(self.score, 1),
                "grade": self.grade, "metrics": self.metrics,
                "issues": self.issues, "gated": self.gated,
                "passed": self.passed}


@dataclass
class QualityReport:
    artifacts: list[QualityScore] = field(default_factory=list)
    overall: float = 0.0
    grade: str = "?"
    verdict: str = "PASS"          # PASS / WARN / FAIL（只有门禁打开才可能 FAIL）
    mode: str = "report"

    def as_dict(self) -> dict:
        return {"mode": self.mode, "overall": round(self.overall, 1),
                "grade": self.grade, "verdict": self.verdict,
                "artifacts": [a.as_dict() for a in self.artifacts]}


# ---------------------------------------------------------------- 探针
def _run(cmd, timeout: int = 240):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _series(path: Path, vf: str, *, limit: int = 240, timeout: int = 240) -> dict | None:
    """跑一条滤镜链，解析 signalstats 的 metadata 输出。

    返回 {yavg, ymin, ymax, ydif}。「卷积后」与「原始」必须分开跑两条，
    因为它们量的是两件不同的事 —— 合在一条 -vf 里只能拿到混在一起的 YAVG
    （2026-11-09 实测踩到：把卷积后的 YAVG 当亮度均值，得到 luma=1.4 的假过暗）。
    """
    r = _run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", vf,
              "-frames:v", str(limit), "-f", "null", "-"], timeout=timeout)
    if not r:
        return None
    text = r.stdout or ""
    return {
        "yavg": [float(x) for x in _R_YAVG.findall(text)],
        "ymin": [float(x) for x in _R_YMIN.findall(text)],
        "ymax": [float(x) for x in _R_YMAX.findall(text)],
        "ydif": [float(x) for x in _R_YDIF.findall(text)],
    }


def _stats_series(path: Path, *, limit: int = 240, every: int = 1) -> dict:
    """三趟解码：①边缘能量 ②亮度与帧差 ③模糊后的边缘能量（判噪点）。

    ## every 从 3 改成 1（2026-11-14）

    原来 every=3，即每 3 帧只看 1 帧。成本是省了 2/3，但**会漏掉坏帧**：

        minimax_multiframe 末尾 2 帧纯蓝，如果这 2 帧恰好落在跳过的位置上，
        "崩坏帧"判据就完全失效 —— 而它正是这条片子唯一的硬伤。
        81 帧的片子只采样 27 帧，抽样率 33%，丢 2 帧的概率相当高。

    坏帧是「突然变成纯色」这种瞬时事件，抗抽样能力几乎为零。
    要么全采，要么这个判据不可信。

    成本实测：5 秒 768×512 三趟合计 0.8s → 2.1s；1080p 5 秒约 4.5s。
    相对于一次作业 ¥0.1–3 的成本，多 3 秒完全可以接受。
    limit 仍保留（长视频截断），240 帧 = 10 秒 @24fps，
    对"抽样看质量"足够，且防止 1 分钟片把评分拖到分钟级。

    第三趟只为了回答一个问题：这些边缘是**结构**还是**噪点**？
    所以它拿不到时不算致命 —— 少一个噪点判别不该让整条评分失效，
    `noise_keep` 会缺席，评分逻辑按"未判定"处理（不扣分也不报警）。
    """
    sel = f"select='not(mod(n\\,{every}))',"
    edge = _series(path, sel + "convolution=0 1 0 1 -4 1 0 1 0,signalstats,"
                             "metadata=print:file=-", limit=limit)
    if edge is None:
        return {"error": "ffmpeg 超时或不可用"}
    if not edge["yavg"]:
        return {"error": "拿不到边缘序列（解码失败？）"}

    raw = _series(path, sel + "signalstats,metadata=print:file=-", limit=limit)
    if raw is None or not raw["yavg"]:
        return {"error": "拿不到亮度序列（解码失败？）"}

    # 先 gblur 再卷积：有结构的边缘塌得慢，噪点几乎全塌
    blurred = _series(path, sel + "gblur=sigma=1.5,convolution=0 1 0 1 -4 1 0 1 0,"
                                "signalstats,metadata=print:file=-", limit=limit)

    return {
        "edge": edge["yavg"],                    # 卷积后的 YAVG = 边缘能量
        "yavg": raw["yavg"],                     # 原始 YAVG = 画面亮度
        "ymin": raw["ymin"],
        "ymax": raw["ymax"],
        "ydif": raw["ydif"],
        # 模糊后的边缘序列；拿不到就是 []，上层按"未判定"处理
        "edge_blur": (blurred or {}).get("yavg") or [],
        "samples": len(raw["yavg"]),
    }


def _grid_cover(path: Path, *, n: int = 6, frames: int = 20,
                timeout: int = 240) -> dict | None:
    """把画面切成 n×n 块，逐块算边缘能量，统计「多少比例是空的」。

    为什么要分块（2026-11-14，人眼判定"差"但旧评分只给 32 分 D 时发现）：

        全局平均边缘能量 = 0.43 的那条片，人工看是"质量非常差"，
        但 0.43 这个数字说不出差在哪。切块之后一眼就清楚：

            36 块里 31 块 < 0.8 → 86% 的画面是没结构的暗块，
            只有中间一条竖列（人像）有内容，那块均值 1.03 其实很清楚。

        所以失败类型不是"模糊"，是「**主体很小、其余是空**」。
        这两件事对用户的意义完全不同，而均值把它们抹成同一个数字。

    另一个作用：判断素材驱动的片子（动作迁移、放大）时，
    空背景是**原素材就有**的，不该算模型的错。分块数据让上层能区分
    "整体均值低但覆盖率正常"（低细节内容，合法）和
    "均值低是因为大片是空的"（构图失败）。

    返回 {cover_bad, block_scores, block_mean, content_mean}；拿不到返回 None。
    """
    blocks: list[list[float]] = []
    for r in range(n):
        row: list[float] = []
        for c in range(n):
            vf = (f"crop=iw/{n}:ih/{n}:iw*{c}/{n}:ih*{r}/{n},"
                  f"convolution=0 1 0 1 -4 1 0 1 0,"
                  f"signalstats,metadata=print:file=-")
            r2 = _run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", vf,
                       "-frames:v", str(frames), "-f", "null", "-"],
                      timeout=timeout)
            if not r2:
                return None
            vals = [float(x) for x in _R_YAVG.findall(r2.stdout or "")]
            row.append(_mean(vals) if vals else 0.0)
        blocks.append(row)

    flat = [x for row in blocks for x in row]
    if not flat:
        return None
    bad = [x for x in flat if x < COVER_BLOCK_MIN]
    good = [x for x in flat if x >= COVER_BLOCK_MIN]
    return {
        "cover_bad": round(len(bad) / len(flat), 3),   # 无内容率 0..1
        "block_mean": round(_mean(flat), 3),           # 全体块均值（≈全局均值）
        "content_mean": round(_mean(good), 3) if good else 0.0,
        "blocks": len(flat),
        "blocks_bad": len(bad),
    }


def _worst_frames(edge: list[float], pct: int = WORST_FRAME_PCTL) -> float | None:
    """最差 pct% 帧的边缘能量均值。样本不足 3 帧就返回 None（不表态）。"""
    if len(edge) < 3:
        return None
    s = sorted(edge)
    k = max(1, len(s) * pct // 100)
    return _mean(s[:k])


def _broken_frames(edge: list[float]) -> dict | None:
    """统计"崩坏帧"占比 —— 与中位数比，而不是与均值比。

    为什么不能用「最差 10% / 均值」（2026-11-14 实测打回）：

        pack_video_minimax_h3_multiframe_reference 旧评分 82.1 A，
        但它的最后 2 帧是**整屏纯蓝**（ffmpeg 蓝底），人工一看就是坏片。

        逐帧边缘能量：
            中位 2.383   均值 2.029
            最低两帧 0.01 / 0.03   ← 纯色帧
            其余 1.22–3.05

        「最差 10%」（42 帧取 4 帧）均值 = 0.31，比值 0.31/2.029 = 0.15，
        确实低于 0.45 会触发 —— 但**触发的理由不对**：它把 4 帧里
        2 帧纯色 + 2 帧略暗算成一个平均数，看不出"有 2 帧完全崩了"。
        而同一规则会把「全片偏暗但每帧一致」也误判（暗片每帧都低，比值却不低）。

    正确的判据是**占比**：多少比例的帧，其边缘能量不到中位数的一个零头。
    这直接对应人眼的"闪了一下坏帧"。

        ratio < BROKEN_FRAME_RATIO（默认 0.15）→ 该帧判为崩坏
        崩坏帧占比 > BROKEN_FRAME_MAX（默认 8%）→ 硬伤

    为什么容忍 8%：片头片尾的转场淡入淡出、以及叙事上的黑场是合法手法。
    124 帧里容忍约 10 帧。
    """
    if len(edge) < 5:
        return None
    med = sorted(edge)[len(edge) // 2]
    if med < 1e-6:
        return None                    # 全片黑 —— 由黑屏判据负责，这里不表态
    thr = med * BROKEN_FRAME_RATIO
    broken = [x for x in edge if x < thr]
    return {
        "broken_ratio": round(len(broken) / len(edge), 3),
        "broken_frames": len(broken),
        "median": round(med, 3),
        "threshold": round(thr, 3),
    }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _cv(xs: list[float]) -> float:
    """变异系数（σ/μ）：衡量「稳不稳定」。μ≈0 时返回 0 避免爆炸。"""
    if not xs:
        return 0.0
    m = _mean(xs)
    if abs(m) < 1e-6:
        return 0.0
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return (var ** 0.5) / m


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


# ---------------------------------------------------------------- 评分
def score_video(path: Path) -> QualityScore:
    """视频质量分。五维：清晰度 25 / 覆盖率 15 / 运动 25 / 曝光 20 / 稳定 15。

    2026-11-14 从四维改成五维：清晰度是全局平均值，主体只占小部分画面时
    它基本在量背景。新增的覆盖率专门回答"画面有多少比例是空的" ——
    这正是人眼看一条片子说"质量非常差"时最常见的那个原因，
    而旧的四维评分表完全抓不到（详见 COVER_BAD 的注释）。
    """
    st = _stats_series(path)
    s = QualityScore(file=path.name, kind="video")
    if st.get("error"):
        s.metrics = {"error": st["error"]}
        s.issues.append(f"无法评估内容：{st['error']}")
        s.score, s.grade = 0.0, "?"
        return s

    yavg, ydif, edge = st["yavg"], st["ydif"], st["edge"]
    sharp = _mean(edge)                    # 卷积后的均值 = 边缘能量
    motion = _mean(ydif) if ydif else 0.0
    luma = _mean(yavg)                     # 原始亮度均值
    lo, hi = (min(st["ymin"]) if st["ymin"] else luma,
              max(st["ymax"]) if st["ymax"] else luma)
    rng = hi - lo
    stability = _cv(edge)                  # 用边缘序列算闪动，不用亮度（曝光会变）

    # 噪点判别：模糊后边缘能量保留率。拿不到就保持 None（未判定）。
    keep = _noise_keep(st.get("edge_blur") or [], edge)
    is_noise = keep is not None and keep < NOISE_KEEP

    # 内容覆盖率：切 6×6 块看多少比例"什么都没画"。
    # 这一步多花约 0.3–0.8s（36 次 crop+卷积，各取 20 帧），换来一个
    # 全局均值永远给不出的答案：主体是不是很小、背景是不是空的。
    grid = _grid_cover(path)
    worst = _worst_frames(edge)
    broken = _broken_frames(edge)

    s.metrics = {
        "sharpness": round(sharp, 3),
        "motion": round(motion, 3),
        "luma_mean": round(luma, 1),
        "luma_range": round(rng, 1),
        "sharpness_cv": round(stability, 3),
        "samples": st["samples"],
    }
    if keep is not None:
        s.metrics["noise_keep"] = round(keep, 1)
    if grid:
        s.metrics["cover_bad"] = grid["cover_bad"]
        s.metrics["content_mean"] = grid["content_mean"]
        s.metrics["blocks_bad"] = grid["blocks_bad"]
        s.metrics["blocks"] = grid["blocks"]
    if worst is not None and sharp > 1e-6:
        s.metrics["worst_frame"] = round(worst, 3)
        s.metrics["worst_ratio"] = round(worst / sharp, 2)
    if broken:
        s.metrics["broken_ratio"] = broken["broken_ratio"]
        s.metrics["broken_frames"] = broken["broken_frames"]
        s.metrics["frame_median"] = broken["median"]

    # 清晰度（40）：BLUR 以下线性到 0，GOOD 以上满分
    #
    # **用 content_mean（有内容的块）而不是全局 sharp**（2026-11-14 改）：
    # 全局均值会把「主体清楚 + 大片空背景」判成糊。用户看到的是主体，
    # 主体清楚就不该按"糊"扣分 —— 空背景是构图问题，交给覆盖率去表达。
    # 没有分块数据时（探测失败）退回全局 sharp，行为与旧版一致。
    if grid and grid["content_mean"] > 0:
        sharp_for_box = grid["content_mean"]
        lo, hi, unit = CONTENT_BLUR, CONTENT_GOOD, "有内容区域边缘能量"
    else:
        sharp_for_box = sharp
        lo, hi, unit = SHARP_BLUR, SHARP_GOOD, "边缘能量"
    q_sharp = _clip01((sharp_for_box - lo) / max(1e-6, hi - lo))
    if sharp_for_box < lo:
        s.issues.append(f"{unit} {sharp_for_box:.2f} < {lo}：画面糊/抽象/缺细节")
    if is_noise:
        # 噪点和糊是**相反**的失败：糊是没有高频，噪点是高频全是垃圾。
        # 两者都该落到低分，但清晰度项对噪点是满分（边缘能量极高），
        # 所以必须在这里单独否决 —— 否则纯噪声视频拿 100 分 A 级。
        q_sharp = 0.0
        s.issues.append(
            f"模糊后边缘能量只剩 {keep:.0f}% < {NOISE_KEEP}%：画面是噪点/无结构，不是细节")

    # 运动（25）：STATIC 以下 0 分，GOOD 以上满分
    q_motion = _clip01((motion - MOTION_STATIC) / (MOTION_GOOD - MOTION_STATIC))
    if motion < MOTION_STATIC:
        s.issues.append(f"帧差 {motion:.2f} < {MOTION_STATIC}：画面基本没动（视频基本是静帧）")

    # 曝光（20）：亮度在 [26,232] 内且动态范围够，才满分
    q_luma = 1.0
    if luma < LUMA_LOW:
        q_luma = _clip01(luma / LUMA_LOW)
        s.issues.append(f"亮度均值 {luma:.0f} < {LUMA_LOW}：整体过暗")
    elif luma > LUMA_HIGH:
        q_luma = _clip01((255 - luma) / max(1.0, 255 - LUMA_HIGH))
        s.issues.append(f"亮度均值 {luma:.0f} > {LUMA_HIGH}：整体过曝")
    if rng < RANGE_MIN:
        q_luma *= 0.5
        s.issues.append(f"动态范围 {rng:.0f} < {RANGE_MIN}：画面发灰/低对比")

    # 稳定（15）：变异系数越小越稳；超过 FLICKER_CV 归零
    q_stab = _clip01(1.0 - stability / FLICKER_CV)
    if stability > FLICKER_CV:
        s.issues.append(f"边缘能量变异 {stability:.2f} > {FLICKER_CV}：闪烁或抖帧")

    # 覆盖率（新增，从清晰度那 40 分里借 15）：
    # 旧权重「清晰度 40 / 运动 25 / 曝光 20 / 稳定 15」里，清晰度占了 40，
    # 而清晰度是**全局平均**的 —— 主体只占 15% 画面时，那 40 分基本在量背景。
    # 把 15 分挪给覆盖率，等于要求"要么画面都有内容，要么主体够清晰"。
    #
    #   覆盖 0%    → 1.0（满画面都有结构）
    #   覆盖 15%   → 1.0（容忍天空/水面/纯色背景）
    #   覆盖 50%   → 0.0（过半是空的，这是硬伤）
    if grid:
        cb = grid["cover_bad"]
        q_cover = _clip01((COVER_BAD - cb) / max(1e-6, COVER_BAD - COVER_OK))
    else:
        q_cover = 1.0          # 探测失败 → 不表态，不扣分（与旧版等价）
    if grid and grid["cover_bad"] > COVER_BAD:
        s.issues.append(
            f"{grid['cover_bad']*100:.0f}% 的画面块无内容"
            f"（{grid['blocks_bad']}/{grid['blocks']} 块 < {COVER_BLOCK_MIN}）"
            f"：主体太小 / 背景是大片空白或纯色")

    s.score = (25 * q_sharp + 15 * q_cover + 25 * q_motion
               + 20 * q_luma + 15 * q_stab)
    s.metrics["q_cover"] = round(q_cover, 2)

    # 木桶封顶：加权分会让「一项致命 + 三项漂亮」拿到中等分 —— 实测里
    # LTXV 失败样本（抽象噪点，sharp=0.56）靠高运动/好曝光拿了 58.4 分，
    # 反而高于合法的 wan_i2v（60.2 只差 1.8）。糊就是糊，不会因为动得好而变好，
    # 所以任一致命项触发就把总分压到该维度能接受的上限之下。
    cap, cap_why = _bucket_cap(sharp, motion, luma, rng, stability,
                               noise_keep=keep,
                               cover_bad=(grid or {}).get("cover_bad"),
                               broken_n=s.metrics.get("broken_frames"))
    if cap is not None and s.score > cap:
        s.score = float(cap)
        s.metrics["capped_by"] = cap_why

    s.grade = _grade(s.score)
    return s


def score_image(path: Path) -> QualityScore:
    """图片质量分。三维：清晰度 50 / 曝光 30 / 动态范围 20。

    图片没有时间维度，所以没有 motion / stability。
    """
    st = _stats_series(path, limit=1, every=1)
    s = QualityScore(file=path.name, kind="image")
    if st.get("error"):
        s.metrics = {"error": st["error"]}
        s.issues.append(f"无法评估内容：{st['error']}")
        s.score, s.grade = 0.0, "?"
        return s

    yavg = st["yavg"]
    sharp = _mean(st["edge"])
    luma = _mean(yavg)
    lo, hi = (min(st["ymin"]) if st["ymin"] else luma,
              max(st["ymax"]) if st["ymax"] else luma)
    rng = hi - lo

    keep = _noise_keep(st.get("edge_blur") or [], st["edge"])
    is_noise = keep is not None and keep < NOISE_KEEP

    s.metrics = {"sharpness": round(sharp, 3), "luma_mean": round(luma, 1),
                 "luma_range": round(rng, 1), "samples": st["samples"]}
    if keep is not None:
        s.metrics["noise_keep"] = round(keep, 1)

    q_sharp = _clip01((sharp - SHARP_BLUR) / (SHARP_GOOD - SHARP_BLUR))
    if sharp < SHARP_BLUR:
        s.issues.append(f"边缘能量 {sharp:.2f} < {SHARP_BLUR}：图片糊/抽象")
    if is_noise:
        # 图片同样的问题：噪点图的边缘能量极高，清晰度项会满分
        q_sharp = 0.0
        s.issues.append(
            f"模糊后边缘能量只剩 {keep:.0f}% < {NOISE_KEEP}%：图片是噪点/无结构，不是细节")

    q_luma = 1.0
    if luma < LUMA_LOW:
        q_luma = _clip01(luma / LUMA_LOW)
        s.issues.append(f"亮度均值 {luma:.0f} < {LUMA_LOW}：整体过暗")
    elif luma > LUMA_HIGH:
        q_luma = _clip01((255 - luma) / max(1.0, 255 - LUMA_HIGH))
        s.issues.append(f"亮度均值 {luma:.0f} > {LUMA_HIGH}：整体过曝")

    q_range = _clip01(rng / (RANGE_MIN * 2.5))
    if rng < RANGE_MIN:
        s.issues.append(f"动态范围 {rng:.0f} < {RANGE_MIN}：图片发灰")

    s.score = 50 * q_sharp + 30 * q_luma + 20 * q_range
    # 木桶封顶（与视频同一套理由：一项硬伤不该被其他项拉回中等分）
    if is_noise:
        # 噪点最先判、封得最狠：它是"清晰度满分但完全没内容"的唯一情形
        s.score = min(s.score, 20)
        s.metrics["capped_by"] = f"noise_keep {keep:.0f}% < {NOISE_KEEP}%"
    elif sharp < SHARP_BLUR:
        s.score = min(s.score, 32)
        s.metrics["capped_by"] = f"sharpness {sharp:.2f} < {SHARP_BLUR}"
    elif luma < LUMA_LOW or luma > LUMA_HIGH:
        s.score = min(s.score, 40)
        s.metrics["capped_by"] = f"luma {luma:.0f} 超出 [{LUMA_LOW},{LUMA_HIGH}]"
    elif rng < RANGE_MIN:
        s.score = min(s.score, 45)
        s.metrics["capped_by"] = f"luma_range {rng:.0f} < {RANGE_MIN}"
    s.grade = _grade(s.score)
    return s


def _grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 55:
        return "B"
    if score >= 35:
        return "C"
    return "D"


def _noise_keep(edge_blur: list[float], edge: list[float]) -> float | None:
    """模糊后边缘能量的保留率（%）。拿不到数据返回 None（未判定，不当成 0）。

    为什么不能拿不到就当 0：0% 意味着"肯定是噪点"，会把一次 ffmpeg 抖动
    变成"这条片子是垃圾"的结论。评分器自己出错时应当**不表态**，不是乱表态。
    """
    a = _mean(edge)
    if not edge_blur or a < 1e-6:
        return None
    return _mean(edge_blur) / a * 100.0


def _bucket_cap(sharp: float, motion: float, luma: float, rng: float,
                stability: float, *, noise_keep: float | None = None,
                cover_bad: float | None = None,
                broken_n: int | None = None
                ) -> tuple[int | None, str]:
    """木桶封顶：任一致命项存在时，总分不得高于该维度能接受的上限。

    为什么需要：加权模型在「一项彻底坏 + 三项很好」时给出中等分。
    实测证据（31 条真实样本）：

        LTXV 失败样本  sharp=0.56（糊成抽象） 但 motion=2.98 / 曝光正常
                       → 加权分 58.4，比合法的 wan_i2v（60.2）只低 1.8
        h3 I2V（正常） sharp=1.41 → 67.7

    用户看到「58 分」会以为"还行"，但那片子是花的。糊不会因为动得好而变好，
    所以用封顶表达「这一条有一个硬伤」。

    封顶值不是随便定的：都取在 B 的下沿（55）以下，让致命项一定落到 C/D。

    噪点（2026-11-09 新增）放在**最前面**检查，而且封得最狠（20 分）：
    它是唯一一种「清晰度指标满分但画面完全没内容」的失败 —— 纯噪声视频
    加权分能到 100，靠其他项永远拉不回来，只能在这里一刀切死。
    """
    if noise_keep is not None and noise_keep < NOISE_KEEP:
        return 20, f"noise_keep {noise_keep:.0f}% < {NOISE_KEEP}%（噪点/无结构）"
    # 覆盖率封顶放在清晰度之前：画面过半是空的，比"稍微糊"严重得多，
    # 而且这两件事经常同时出现（空背景把全局均值压到 BLUR 以下）。
    # 先用覆盖率解释清楚，再轮到清晰度。
    if cover_bad is not None and cover_bad > COVER_BAD:
        # 封 30（比"糊"的 32 更严）：大部分画面是空的，比"整体稍微糊"
        # 对观看体验的破坏更大 —— 糊至少还是一幅完整的画。
        return 30, (f"cover {cover_bad*100:.0f}% 的画面块无内容 "
                    f"(> {COVER_BAD*100:.0f}%)")
    # 崩坏帧封顶分级：2–5 帧封 52（C 区，片子主体还能用，尾部需要裁掉）；
    # 6 帧以上封 32（D 区，已经影响观看）。
    # 为什么不一刀切到 D：multiframe 那条前半段质量是真好（清晰度 2.03、
    # 覆盖率只有 8%），只有末尾 2 帧纯蓝。整条判 D 会把一个"裁掉尾巴就能用"
    # 的工作流判死，那是另一种误判。
    if broken_n is not None and broken_n >= 6:
        return 32, f"崩坏帧 {broken_n} 帧（画面大段变成纯色/黑块）"
    if broken_n is not None and broken_n >= BROKEN_FRAME_MIN:
        return 52, f"崩坏帧 {broken_n} 帧（片头/片尾可能有纯色帧，需裁剪）"
    if sharp < SHARP_BLUR:
        return 32, f"sharpness {sharp:.2f} < {SHARP_BLUR}"
    if luma < LUMA_LOW or luma > LUMA_HIGH:
        return 40, f"luma {luma:.0f} 超出 [{LUMA_LOW},{LUMA_HIGH}]"
    if rng < RANGE_MIN:
        return 45, f"luma_range {rng:.0f} < {RANGE_MIN}"
    if stability > FLICKER_CV:
        return 45, f"sharpness_cv {stability:.2f} > {FLICKER_CV}"
    if motion < MOTION_STATIC:
        # 静帧对视频是硬伤，但静帧本身可能是用户要的（定格/照片风），
        # 所以封顶比"糊"宽松一档，落在 C 区。
        return 52, f"motion {motion:.2f} < {MOTION_STATIC}"
    return None, ""


# ---------------------------------------------------------------- 门禁
def gate(score: QualityScore, *, min_score: float = 0.0,
         min_grade: str = "", block_issues: tuple[str, ...] = ()) -> QualityScore:
    """把评分卡按门禁规则判定，写回 passed/gated。

    门禁规则越简单越好解释：要么分数下限，要么等级下限，要么点名禁某类问题。
    不做「加权综合分 + 多维下限」那种，用户看不懂就没法预期。
    """
    score.gated = True
    ok = True
    if min_score > 0 and score.score < min_score:
        ok = False
        score.issues.append(f"质量分 {score.score:.1f} < 门禁下限 {min_score}")
    if min_grade:
        order = "DCBA"
        if order.find(score.grade) < order.find(min_grade):
            ok = False
            score.issues.append(f"质量等级 {score.grade} 低于门禁 {min_grade}")
    for kw in block_issues:
        if any(kw in i for i in score.issues):
            ok = False
    score.passed = ok
    return score


# ---------------------------------------------------------------- 入口
def assess(path: Path, *, kind: str = "video", mode: str = "report",
           min_score: float = 0.0, min_grade: str = "",
           block_issues: tuple[str, ...] = ()) -> QualityScore:
    """评一条产物。mode=report 只记录；mode=gate 才可能 passed=False。"""
    p = Path(path)
    if not p.is_file():
        s = QualityScore(file=p.name, kind=kind, score=0.0, grade="?")
        s.issues.append("文件不存在")
        s.passed = mode != "gate"
        s.gated = mode == "gate"
        return s
    s = score_image(p) if kind == "image" else score_video(p)
    if mode == "gate":
        gate(s, min_score=min_score, min_grade=min_grade, block_issues=block_issues)
    return s


def assess_job(arts: list[Path], *, kind: str = "video", mode: str = "report",
               min_score: float = 0.0, min_grade: str = "",
               block_issues: tuple[str, ...] = ()) -> QualityReport:
    """评一份作业的全部产物。

    overall 取**主产物**（列表第一个，fetch 已把主产物排首位）的分数，
    不是平均值：附加产物（pose 预览、对比片、单帧封面）质量天然低，
    平均会把好作业拉低，从而让门禁误杀。
    """
    rep = QualityReport(mode=mode)
    for a in arts:
        rep.artifacts.append(assess(a, kind=kind, mode=mode, min_score=min_score,
                                    min_grade=min_grade, block_issues=block_issues))
    if not rep.artifacts:
        rep.verdict = "FAIL" if mode == "gate" else "PASS"
        return rep
    primary = rep.artifacts[0]
    rep.overall = primary.score
    rep.grade = primary.grade
    if mode == "gate":
        rep.verdict = "PASS" if all(a.passed for a in rep.artifacts) else "FAIL"
    else:
        # report 模式：有严重问题也只 WARN，绝不改 PASS/FAIL
        rep.verdict = "WARN" if primary.issues else "PASS"
    return rep


def from_expect(expect: dict | None) -> dict:
    """从 job.expect 里读质量门禁配置（工作流卡可以按条声明）。"""
    q = (expect or {}).get("quality") or {}
    return {
        "mode": str(q.get("mode") or "report"),
        "min_score": float(q.get("min_score") or 0),
        "min_grade": str(q.get("min_grade") or ""),
        "block_issues": tuple(q.get("block_issues") or ()),
    }
