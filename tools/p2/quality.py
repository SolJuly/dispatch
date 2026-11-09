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
MOTION_STATIC = 0.6     # YDIF 均值 < 0.6 → 基本没动（动作类工作流该报警）
MOTION_GOOD = 2.0       # > 2.0 → 有明确运动（实测正常片均 > 2.3）
LUMA_LOW = 26           # 亮度均值 < 26 → 过暗（黑电平 16 + 余量）
LUMA_HIGH = 232         # 亮度均值 > 232 → 过曝
RANGE_MIN = 28          # YMAX-YMIN < 28 → 发灰/低对比
FLICKER_CV = 0.85       # 边缘能量变异系数 > 0.85 → 闪烁/抖帧（实测最高 0.42）

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


def _stats_series(path: Path, *, limit: int = 240, every: int = 3) -> dict:
    """两趟解码：①边缘能量（卷积后）②亮度与帧差（原始）。

    每趟都先抽帧降采样（every 帧取 1）并限帧数 —— 质量分是附加信息，
    不该把主链路拖慢。5 秒 1080p 实测两趟合计 ~0.5s。
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

    return {
        "edge": edge["yavg"],                    # 卷积后的 YAVG = 边缘能量
        "yavg": raw["yavg"],                     # 原始 YAVG = 画面亮度
        "ymin": raw["ymin"],
        "ymax": raw["ymax"],
        "ydif": raw["ydif"],
        "samples": len(raw["yavg"]),
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
    """视频质量分。四维：清晰度 40 / 运动 25 / 曝光 20 / 稳定 15。"""
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

    s.metrics = {
        "sharpness": round(sharp, 3),
        "motion": round(motion, 3),
        "luma_mean": round(luma, 1),
        "luma_range": round(rng, 1),
        "sharpness_cv": round(stability, 3),
        "samples": st["samples"],
    }

    # 清晰度（40）：BLUR 以下线性到 0，GOOD 以上满分
    q_sharp = _clip01((sharp - SHARP_BLUR) / (SHARP_GOOD - SHARP_BLUR))
    if sharp < SHARP_BLUR:
        s.issues.append(f"边缘能量 {sharp:.2f} < {SHARP_BLUR}：画面糊/抽象/缺细节")

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

    s.score = 40 * q_sharp + 25 * q_motion + 20 * q_luma + 15 * q_stab

    # 木桶封顶：加权分会让「一项致命 + 三项漂亮」拿到中等分 —— 实测里
    # LTXV 失败样本（抽象噪点，sharp=0.56）靠高运动/好曝光拿了 58.4 分，
    # 反而高于合法的 wan_i2v（60.2 只差 1.8）。糊就是糊，不会因为动得好而变好，
    # 所以任一致命项触发就把总分压到该维度能接受的上限之下。
    cap, cap_why = _bucket_cap(sharp, motion, luma, rng, stability)
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

    s.metrics = {"sharpness": round(sharp, 3), "luma_mean": round(luma, 1),
                 "luma_range": round(rng, 1), "samples": st["samples"]}

    q_sharp = _clip01((sharp - SHARP_BLUR) / (SHARP_GOOD - SHARP_BLUR))
    if sharp < SHARP_BLUR:
        s.issues.append(f"边缘能量 {sharp:.2f} < {SHARP_BLUR}：图片糊/抽象")

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
    if sharp < SHARP_BLUR:
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


def _bucket_cap(sharp: float, motion: float, luma: float, rng: float,
                stability: float) -> tuple[int | None, str]:
    """木桶封顶：任一致命项存在时，总分不得高于该维度能接受的上限。

    为什么需要：加权模型在「一项彻底坏 + 三项很好」时给出中等分。
    实测证据（31 条真实样本）：

        LTXV 失败样本  sharp=0.56（糊成抽象） 但 motion=2.98 / 曝光正常
                       → 加权分 58.4，比合法的 wan_i2v（60.2）只低 1.8
        h3 I2V（正常） sharp=1.41 → 67.7

    用户看到「58 分」会以为"还行"，但那片子是花的。糊不会因为动得好而变好，
    所以用封顶表达「这一条有一个硬伤」。

    封顶值不是随便定的：都取在 B 的下沿（55）以下，让致命项一定落到 C/D。
    """
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
