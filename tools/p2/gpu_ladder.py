#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显存阶梯：OOM 之后该换哪张卡，以及怎么判断「这真的是 OOM」。

## 为什么单独一个模块

2026-11-12 之前，显存不足的处理是**一句话**：

    ComfyUI 进程已退出（第 1523s，疑似显存不足/崩溃）→ FAILED_RUN → 作业死

三件事混在一起，所以哪件都做不好：

  1. **判定**：把「ComfyUI 崩了」和「显存不够」当成同一件事。前者可能是
     系统 OOM killer、可能是段错误、可能是我改了代码。判错就会去升卡，
     花 ¥3.80/h 重跑一遍，然后在同一个地方以同样的方式失败。
  2. **选择**：没有「往上换哪张」的概念。`required_gpu` 是用户写死的一个列表，
     `_pick_gpu` 只按**有没有货**挑，不看显存。所以 OOM 之后重跑，
     抢到的还是同一张 24G 卡 —— 一模一样的失败，钱再花一次。
  3. **收敛**：没有重试上限的概念。加上自动重试以后，如果不设闸门，
     一个必然 OOM 的作业会在 24G 卡上无限循环，每轮建机 ¥2.15/h。

所以这里只放**纯函数**：显存表、价格表、OOM 文案识别、下一档选择。
不 import compshare、不读 job.json、不建机 —— 这样它能被 run.py、
webui、以及测试三方引用而不互相牵连。

## 显存表哪来的

`4090 = 24GB` 是**实测**的：`DescribeCompShareInstances` 对
`uhost-1xlp0lbuo6m6` 返回 `GraphicsMemory: {"Rate": 3, "Value": 24}`。

其余是平台卡型名对应的公开规格（`4090_48G` 就是 48G 版本、`A100/A800` 80G、
`H20` 96G、`5090` 32G、`3090` 24G、`V100S` 32G、`3080Ti` 12G、`2080` 11G、
`P40` 24G）。**标 `_VERIFIED` 的才是实测过的**，其余按声明值用。

声明值错了的后果是「升错档」，不是「算错钱」—— 真正的扣费始终来自
`cs.price()` 的实时返回，这张表只用来**排序**和**定下限**。

## 价格表同理

2026-11-12 实测（`GetCompShareInstanceUserPrice`，Postpay，100G 系统盘）：

    2080      ¥0.42/h      4090      ¥2.15/h      5090     ¥3.99/h
    P40       ¥0.40/h      4090_48G  ¥3.80/h      A800     ¥7.36/h
    3080Ti    ¥0.74/h      3090      ¥1.19/h      H20      ¥7.50/h
    V100S     ¥1.41/h                            A100    ¥10.75/h

价格会变。变了就改这里（只影响「同档位里先试哪张」的顺序），
不要改成从环境变量读 —— 那会让同一个作业在两次运行里选不同的卡。

## 一条硬规矩：升级只升不降

`next_targets()` 只返回**显存严格更大**的卡。降级等于把已经证伪的配置
再跑一遍，只会再烧一次钱。要降级必须人工改 `required_gpu`。
"""
from __future__ import annotations

# 卡型 → 显存 GB。带 ✓ 的是实测值，其余按平台公开规格声明。
VRAM_GB: dict[str, int] = {
    "2080": 11,
    "3080Ti": 12,
    "P40": 24,
    "3090": 24,
    "4090": 24,        # ✓ 实测：GraphicsMemory.Value = 24
    "5090": 32,
    "V100S": 32,
    "4090_48G": 48,    # ✓ 卡型名即 48G
    "A100": 80,
    "A800": 80,
    "H20": 96,
}

# 键的是**实测**过显存的卡型；其余是声明值。用于在日志里区分可信度。
_VERIFIED = frozenset({"4090", "4090_48G"})

# 显存够大、但**不能**当升卡目标的卡型（`next_targets` 会跳过）。
#
# 为什么单列：显存表只回答"能不能装下"，不回答"装下之后算不算得完"。
# 我们的权重是 int8 / fp8 量化的（`comfy-aimdo` / `comfy-kitchen` 的解码内核），
# 这些内核要 tensor core：
#
#   V100S  Volta  sm_70   无 int8 tensor core、无 bf16 → 只能退到 fp32/fp16 路径
#   P40    Pascal sm_61   连 fp16 tensor core 都没有
#
# 升到这两张卡的后果不是"跑得慢一点"，而是**换一种方式失败**（内核不支持），
# 然后再花一轮钱。清单只放实测过显存的卡之外、且确实会被选到的型号 ——
# 3090/3080Ti 是 Ampere（sm_86）且显存不大于 24G，本来就不会成为目标。
_NOT_FOR_ESCALATION = {
    "V100S": "Volta sm_70，无 int8 tensor core / 无 bf16，量化权重跑不动",
    "P40": "Pascal sm_61，无 fp16 tensor core",
}

# 卡型 → ¥/小时（2026-11-12 实测，Postpay + 100G 系统盘）
HOURLY: dict[str, float] = {
    "P40": 0.40,
    "2080": 0.42,
    "3080Ti": 0.74,
    "3090": 1.19,
    "V100S": 1.41,
    "4090": 2.15,
    "4090_48G": 3.80,
    "5090": 3.99,
    "A800": 7.36,
    "H20": 7.50,
    "A100": 10.75,
}


def vram_of(gpu: str) -> int:
    """显存 GB。不认识的卡型返回 0（=「不知道」，会被当作不满足任何下限）。"""
    return int(VRAM_GB.get(str(gpu or ""), 0) or 0)


def hourly_of(gpu: str) -> float:
    return float(HOURLY.get(str(gpu or ""), 0.0) or 0.0)


def is_verified(gpu: str) -> bool:
    return str(gpu or "") in _VERIFIED


def describe(gpu: str) -> str:
    """`4090_48G 48G ¥3.80/h` —— 日志里一行说清楚。"""
    v = vram_of(gpu)
    h = hourly_of(gpu)
    mark = "" if is_verified(gpu) or not v else "（显存为声明值）"
    vs = f"{v}G" if v else "显存未知"
    hs = f"¥{h:.2f}/h" if h else "价格未知"
    return f"{gpu} {vs} {hs}{mark}"


# ------------------------------------------------------------------ OOM 识别
# 分两档，因为代价完全不同：
#
#   certain  文案里直接写着显存不够 → 升卡几乎必然有效
#   likely   进程没了但日志里没有 OOM 字样 → 可能是别的崩溃，升卡是赌博
#
# 只有 certain 自动升卡。likely 只记录 + 打印提示，因为它花钱且多半没用。
# 想让 likely 也升卡就设 `P1_OOM_ESCALATE_ON_CRASH=1`（自己承担代价）。
_OOM_CERTAIN = (
    "cuda out of memory",
    "cuda error: out of memory",
    "torch.outofmemoryerror",
    "outofmemoryerror",
    "allocation on device",
    "tried to allocate",
    "cublas_status_alloc_failed",
    "not enough memory",
    "insufficient memory",
    "显存不足",
    "内存不足",
)

# 这些字样说明**不是**显存问题，即使同时出现了 OOM 字样也不要升卡
_OOM_NOT = (
    "use_missing_models",
    "required_input_missing",
    "invalid_input_type",
    "value not in list",
    "cannot import name",
    "modulenotfounderror",
    "cuda error: no kernel image",      # 架构不匹配，换大卡没用
    "no kernel image is available",
)


def classify_oom(text: str) -> str:
    """返回 `certain` / `likely` / `no`。

    `likely` 用于「进程消失但没留下证据」的场景，由调用方决定要不要升卡。

    ## 判定规则（2026-11-12 修过一次）

    **按行判，从最后一行往前找；一行之内"不是 OOM"的字样优先。**

    为什么按行、为什么从后往前 —— 第一版是对**整段文本**先查 `_OOM_NOT`
    再查 `_OOM_CERTAIN`，于是：

        use_missing_models: 'foo_out_of_memory.safetensors' not in list   ← 旧的错
        torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB  ← 真的爆了

    整段判 → 命中 `use_missing_models` → 返回 `no` → **不升卡**。
    但日志是按时间写的，杀死本次作业的是**最后那条**错误。
    旧错误裹着 "out of memory" 字样（权重名里就有）把真 OOM 盖住了。

    为什么一行之内 NOT 仍然优先 —— 单行同时含两种字样时
    （`use_missing_models: ... out of memory checkpoint.safetensors`），
    这条错误的**本质**是缺模型，不是显存。这个方向没变。
    """
    m = (text or "").lower()
    if not m:
        return "no"
    for ln in reversed([l for l in m.splitlines() if l.strip()]):
        if any(k in ln for k in _OOM_NOT):
            return "no"
        if any(k in ln for k in _OOM_CERTAIN):
            return "certain"
    return "no"


def oom_line(text: str, *, limit: int = 240) -> str:
    """从一大段日志里摘出与 OOM 有关的那几行（给人看，不是给程序判）。"""
    out = []
    for ln in (text or "").splitlines():
        low = ln.lower()
        if any(k in low for k in _OOM_CERTAIN):
            out.append(ln.strip())
    if not out:
        return ""
    return (" | ".join(out[-3:]))[:limit]


# ------------------------------------------------------------------ 下一档
def next_targets(current_gpu: str, stock: dict[str, int] | None = None,
                 *, gpus: list[str] | None = None) -> list[tuple[str, int, float]]:
    """OOM 之后该换哪些卡：显存**严格更大**、当前**有货**、按价格升序。

    返回 `[(gpu, vram_gb, hourly), ...]`，空列表 = 没有更高的可用档位。

    为什么要看 `stock`：实测 2026-11-12，`5090`（32G，¥3.99/h）库存长期 0，
    而 `4090_48G`（48G，¥3.80/h）有 2 台。只看显存表会挑 5090（+8G 更便宜的想法），
    然后卡在下单被抢 226604 上 —— 白等一轮，"升级"没发生。

    为什么要按价格排序：同为 ≥48G 的档位有 4090_48G(¥3.80) / A800(¥7.36) /
    H20(¥7.50) / A100(¥10.75)。够用就好，先试便宜的。
    """
    cur_v = vram_of(current_gpu)
    pool = gpus if gpus is not None else list(VRAM_GB)
    cands = []
    for g in pool:
        v = vram_of(g)
        if v <= cur_v:
            continue
        if g in _NOT_FOR_ESCALATION:
            continue
        if stock is not None and int(stock.get(g, 0) or 0) <= 0:
            continue
        cands.append((g, v, hourly_of(g)))
    # 先按显存升序（这是"最小够用"的意思），同显存再按价格
    cands.sort(key=lambda t: (t[1], t[2]))
    if not cands:
        return []
    # 只保留**最小够用显存**那一档：升到 48G 能解决就不要直接跳 96G
    floor_v = cands[0][1]
    same = [t for t in cands if t[1] == floor_v]
    same.sort(key=lambda t: t[2])
    return same


def skipped_note(current_gpu: str) -> str:
    """本次升级**故意跳过**了哪些卡、为什么。给日志用。

    跳过的理由必须打出来：否则运维看到"V100S 有货为什么不用"会以为是 bug，
    或者更糟 —— 手动加 `--gpu V100S` 绕过去，然后在 kernel 不支持上失败一次。
    """
    out = []
    cv = vram_of(current_gpu)
    for g, why in _NOT_FOR_ESCALATION.items():
        if vram_of(g) > cv:
            out.append(f"{g}（{why}）")
    return "；".join(out)



def filter_by_floor(candidates: list[str], floor: int) -> list[str]:
    """只留显存 ≥ floor 的卡，保持原顺序。

    floor=0（未知）时原样返回 —— 「不知道」不能当成「必须升到 96G」。
    """
    if not floor:
        return list(candidates or [])
    return [g for g in (candidates or []) if vram_of(g) >= floor]


def summarize_stock(stock: dict[str, int]) -> str:
    """一行库存摘要，只列有货的卡（全空时明确说全空，不要打印空串）。"""
    have = [f"{g}×{n}" for g, n in sorted(stock.items(), key=lambda kv: -vram_of(kv[0]))
            if int(n or 0) > 0]
    return "有货：" + "、".join(have) if have else "所有卡型都无货"
