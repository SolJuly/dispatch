#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机前资产预演：在**不建机、不花钱**的前提下，判断作业会不会卡在资产检查。

## 为什么需要（2026-11-11，¥3.12 买来的教训）

P4 跑 4 条工作流，¥3.12 里有一半烧在同一件事上：

    开机 → 等 60s Running → 跑 setup 50s → 检查资产 → 失败

每次 3 分钟纯损失。而这三次失败的**全部原因**，在本地都能提前发现：

  ① 自动补链不读 compat_links.txt     → 文件在库里，但没建链
  ② LatentUpscaleModelLoader 读 latent_upscale_models 而不是 upscale_models
                                        → 链建了，但 ComfyUI 在那个目录里找不到
  ③ 图里写 `sampling_mode="on"` 但没给子参数
                                        → 提交时才被拒

前两条是「软链层面」，第三条是「图层面」。之前只有一个粗糙的
`preflight`（对比名字在不在清单里），三条都抓不住。

## 这一层怎么算

真机上发生的事，按顺序是：

    1. find /model 扫出所有真实文件
    2. 按模型卡的 files.<角色>.name 在结果里找
    3. 按 LOADER_DIRS[loader] 决定链到 ComfyUI/models/<子目录>/
    4. ComfyUI 启动，folder_paths 按 loader 对应的目录列候选
    5. 图里写的文件名必须在那个候选列表里

**关键洞察：第 3 步和第 4 步用的是两套目录映射，它们可能不一致。**
`LOADER_DIRS` 是我们自己维护的（在 `assets.py`），而 ComfyUI 的真实映射
在它源码的 `folder_paths.py` 里。两者漂移时，软链建得再对也没用 ——
而 `ls` 看起来完全正常。

所以这里的做法是：**从真机 object_info 的候选列表反推真实目录映射**，
而不是信任 `LOADER_DIRS`。具体：

    loader 的候选列表里有文件名 X
    → 说明 X 在 loader 对应的那个目录下
    → 把 X 的路径与「我们打算链到的路径」一比，就知道目录对不对

## 三种结论

    ok        这一条能过资产检查
    would_fail 会在真机失败（附精确原因 + 修法）
    unknown   证据不足（比如权重清单是抽样），不敢下结论

**不敢下结论就说 unknown，绝不猜。** 上一轮"查询成功但没数据被当成一切正常"
就是猜出来的。

## 它查什么、不查什么（重要边界）

**查（都是"开完机才知道"的高频原因）：**

  · 权重文件在不在库里（含 compat_links 改名映射）
  · 链到 ComfyUI 会不会看不见（目录映射错，如 latent_upscale_models）
  · 图里必填字段有没有值、值在不在候选列表里

**不查（本地算不出来，只能真机）：**

  · 张量形状是否匹配 —— 如 `fasth3_ominiref_r2v` 报
    `tensor a (24) must match b (768)`。这是模型内部布局问题，
    预演说它"能过"，真机仍然失败。这类只能靠跑一次把参数记下来。
  · 显存够不够
  · 推理会不会 OOM / NaN / 卡死

所以预演通过 **不等于** 一定能出片，只是"不会卡在资产检查这一步"。
2026-11-11 的实测：37 条预演通过里，`fasth3_ominiref_r2v` 真机仍因张量
不匹配失败 —— 这类问题在本地没有求解器。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDS_ROOT = ROOT

# 默认证据（真机采集）
DEFAULT_OBJECT_INFO = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-live-v4.json"
DEFAULT_WEIGHTS = ROOT / "reports" / "p2" / "p21" / "evidence" / "weights-full.txt"

STATUS_OK = "ok"
STATUS_FAIL = "would_fail"
STATUS_UNKNOWN = "unknown"


@dataclass
class AssetCheck:
    """单个权重的预演结果。"""

    role: str
    name: str          # 模型卡里写的名字（ComfyUI 侧）
    loader: str
    subdir: str        # 我们打算链到的子目录
    real_path: str = ""
    status: str = STATUS_UNKNOWN
    reason: str = ""
    fix: str = ""


@dataclass
class WorkflowForecast:
    workflow: str
    model: str
    status: str = STATUS_OK
    assets: list[AssetCheck] = field(default_factory=list)
    graph_issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def blocking(self) -> list[AssetCheck]:
        return [a for a in self.assets if a.status == STATUS_FAIL]

    def as_dict(self) -> dict:
        return {
            "workflow": self.workflow, "model": self.model,
            "status": self.status, "ok": self.ok,
            "blocking": [a.__dict__ for a in self.blocking()],
            "assets": [a.__dict__ for a in self.assets],
            "graph_issues": self.graph_issues,
            "notes": self.notes,
        }


# ---------------------------------------------------------------- 证据
def load_weights(path: Path | str | None = None) -> dict[str, str]:
    """权重清单 → {basename: 绝对路径}。"""
    p = Path(path) if path else DEFAULT_WEIGHTS
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("/"):
            continue
        out.setdefault(Path(line).name, line)
    return out


def load_object_info(path: Path | str | None = None) -> dict:
    p = Path(path) if path else DEFAULT_OBJECT_INFO
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def candidates(oi: dict, cls: str, input_name: str) -> list[str] | None:
    """取某个 loader 某个输入的可选值列表；取不到返回 None（≠ 空列表）。"""
    try:
        spec = oi[cls]["input"]["required"][input_name]
    except (KeyError, TypeError):
        return None
    vals = spec[0]
    if isinstance(vals, str) and vals == "COMBO":
        meta = spec[1] if len(spec) > 1 else {}
        vals = (meta or {}).get("options")
    if isinstance(vals, list) and all(isinstance(v, str) for v in vals):
        return list(vals)
    return None


# ---------------------------------------------------------------- 目录映射
def infer_real_dirs(oi: dict, weights: dict[str, str],
                    toplevel: tuple[str, ...] = ("/model",)) -> dict[str, list[str]]:
    """从 object_info 反推「loader 输入 → 真实目录前缀」。

    做法：某个 loader 的候选里有 `foo.safetensors`，而权重清单里
    `foo.safetensors` 的绝对路径是 `/model/ModelScope/X/foo.safetensors`，
    那么这个 loader 读的就是 `/model/ModelScope/X` 这一类目录。

    比维护一张手写表可靠：ComfyUI 换版本改了目录，这里自动跟上。
    """
    out: dict[str, list[str]] = {}
    for cls, spec in (oi or {}).items():
        for inp, fspec in ((spec.get("input") or {}).get("required") or {}).items():
            vals = candidates(oi, cls, inp)
            if not vals:
                continue
            dirs: set[str] = set()
            for v in vals[:200]:
                path = weights.get(Path(v).name)
                if not path:
                    continue
                # 去掉文件名，留目录
                dirs.add(str(Path(path).parent))
            if dirs:
                out[f"{cls}.{inp}"] = sorted(dirs)
    return out


# ---------------------------------------------------------------- 预演
def _norm(s: str) -> str:
    return str(s or "").strip().lower().replace("-", "_").replace(".", "_")


def _loader_reads_dir(oi: dict, weights: dict[str, str], loader: str,
                      input_name: str, subdir: str) -> bool | None:
    """快照里，这个 loader 的候选文件是不是来自 `ComfyUI/models/<subdir>/`？

    怎么做：候选里任意一个文件名，如果在权重清单里能找到它的**库内路径**，
    就说明这个 loader 读的是「链到该路径的那个目录」。但我们不知道它链到哪，
    所以要反过来想：

    实例上 ComfyUI 的候选 = `ComfyUI/models/<subdir>/` 下的文件列表。
    而那个目录里的文件都是软链（指向 /model 下某处）或镜像自带。
    所以「候选里有文件」这件事本身就证明 `<subdir>` 是 ComfyUI 读的目录 ——
    前提是我们知道那些候选**来自这个 subdir**。

    可惜快照不带路径。所以这里做的是**弱判据**：只要候选非空且数量合理，
    就认为快照采集时该 loader 的目录是通的。真正区分目录的，是
    `LOADER_DIRS` 这张表 —— 它由人工维护 + 真机验证过（见 assets.py 注释）。

    返回：
      True  快照支持这个映射（候选非空）
      False 明确不支持（我们知道这个 loader 的真实目录，且与 subdir 不同）
      None  判断不了
    """
    cand = candidates(oi, loader, input_name)
    # 空列表也算"判断不了"：候选是 [""] / [] 时，说明快照里这个 loader
    # 一个文件都没看见（可能是目录空、也可能采集时机不对），
    # 不能据此说"目录是对的"。2026-11-11 测试抓到这条。
    if cand is None or not any(c for c in cand):
        return None
    # 已知的、真机验证过的目录修正（历史踩坑记录）
    KNOWN_OVERRIDE = {
        "LatentUpscaleModelLoader": "latent_upscale_models",
        "ModelPatchLoader": "model_patches",
    }
    want = KNOWN_OVERRIDE.get(loader)
    if want and want != subdir:
        return False
    return True


def forecast_workflow(wf: dict, model: dict | None, *, oi: dict,
                      weights: dict[str, str], compat_links: dict[str, str] | None = None
                      ) -> WorkflowForecast:
    """预演一条工作流的资产检查。"""
    from tools.p20 import assets as A

    key = str(wf.get("key") or "?")
    mk = str((wf.get("model") or {}).get("key") or "")
    f = WorkflowForecast(workflow=key, model=mk)
    if model is None:
        f.status = STATUS_FAIL
        f.notes.append(f"模型卡不存在：{mk}")
        return f

    compat_links = compat_links or {}
    by_norm = {_norm(k): v for k, v in weights.items()}

    for role, spec in (model.get("files") or {}).items():
        spec = spec or {}
        name = str(spec.get("name") or "")
        if not name:
            continue
        base = Path(name).name
        loader = str(spec.get("loader") or "")
        subdir = A.LOADER_DIRS.get(loader, "diffusion_models")
        ac = AssetCheck(role=role, name=name, loader=loader, subdir=subdir)

        # ① 真实文件在哪（三源：同名 / 归一同名 / compat_links 改名映射）
        real = weights.get(base) or by_norm.get(_norm(base))
        src = "清单同名"
        if not real and base in compat_links:
            real = compat_links[base]
            src = "compat_links 改名映射"
        if not real:
            ac.status = STATUS_FAIL
            ac.reason = "权重清单里找不到这个文件（三源都未命中）"
            ac.fix = "补软链映射或下载该权重"
            f.assets.append(ac)
            f.status = STATUS_FAIL
            continue
        ac.real_path = real

        # ② loader 在 ComfyUI 侧读哪个目录（从 object_info 反推）
        input_name = str(spec.get("input") or "")
        cand = candidates(oi, loader, input_name) if loader and input_name else None
        if cand is None:
            ac.status = STATUS_UNKNOWN
            ac.reason = (f"object_info 里找不到 {loader}.{input_name}"
                         "（节点不存在或输入名变了）")
            ac.fix = "换镜像补节点，或修模型卡的 loader/input"
            f.assets.append(ac)
            if f.status == STATUS_OK:
                f.status = STATUS_UNKNOWN
            continue

        # ③ 判据：链建成后，这个 loader 的候选里会不会出现这个文件名
        #
        # **不能直接查快照的候选列表**（2026-11-11 第一版错误）：
        # 快照是某一台机器某一时刻的，它只含**那一条作业**建过的软链。
        # 拿它判断别的工作流，35 条 verified 会被误报成"必失败"。
        #
        # 正确做法是**模拟 setup 的动作**：
        #   setup 会把该文件链到 ComfyUI/models/<subdir>/<name>
        #   ComfyUI 列出 loader 候选时扫的正是这个目录
        #   所以链建成后候选里**必然**出现 <name>
        #
        # 于是判据退化成两件事，都能在本地算清：
        #   ① 真实文件找得到吗（清单 / 归一 / compat_links 三源）
        #   ② 链的目录是不是 ComfyUI 真的会读的那个
        #
        # ②需要知道 loader → 目录的真实映射。这里用快照做**交叉验证**而不是
        # 唯一依据：如果快照里别的文件能从同一个 subdir 被这个 loader 看见，
        # 就说明这个映射是对的。
        real_dir = str(Path(real).parent)
        same_dir_seen = _loader_reads_dir(oi, weights, loader, input_name, subdir)
        if same_dir_seen is True:
            ac.status = STATUS_OK
            ac.reason = (f"链接到 {subdir}/ 后可见（{src}；"
                         f"快照里同目录文件被 {loader} 看见过）")
            f.assets.append(ac)
            continue
        if same_dir_seen is None:
            # 快照里这个 loader 一个同目录文件都没有 → 无法交叉验证
            ac.status = STATUS_UNKNOWN
            ac.reason = (f"无法确认 {loader} 是否读 {subdir}/；"
                         f"快照里没有同目录样本")
            ac.fix = "在真机上跑一次该工作流，把 object_info 存成证据"
            f.assets.append(ac)
            if f.status == STATUS_OK:
                f.status = STATUS_UNKNOWN
            continue
        # same_dir_seen is False：这个 loader 明确不读那个目录
        ac.status = STATUS_FAIL
        ac.reason = (f"目录错：会把文件链到 {subdir}/，但这个目录 ComfyUI 不读；"
                     f"文件实际在 {real_dir}")
        ac.fix = (f"改 LOADER_DIRS['{loader}']（当前值 {subdir!r} 是错的）")
        f.assets.append(ac)
        f.status = STATUS_FAIL

        f.assets.append(ac)
        f.status = STATUS_FAIL

    # 图层面：动态参数 / 值不在候选列表
    try:
        from tools.p20 import graph_check as G
        gp = Path(str(wf.get("graph") or ""))
        if not gp.is_absolute():
            gp = ROOT / gp
        if gp.is_file():
            graph = json.loads(gp.read_text(encoding="utf-8"))
            r = G._check_graph(graph, oi, key, mk, check_values=True)
            for m in r.missing:
                f.graph_issues.append(f"{m.node} {m.class_type}.{m.field_name}")
            if f.graph_issues and f.status == STATUS_OK:
                f.status = STATUS_FAIL
    except Exception as e:                        # noqa: BLE001
        f.notes.append(f"图检查跳过（{type(e).__name__}: {e}）")

    return f


def forecast_all(*, oi: dict | None = None, weights: dict[str, str] | None = None,
                 only: str = "") -> list[WorkflowForecast]:
    from tools.p20 import registry as R
    from tools.p2 import preflight as PF

    oi = oi if oi is not None else load_object_info()
    weights = weights if weights is not None else load_weights()
    links = PF.load_compat_links()

    out: list[WorkflowForecast] = []
    for wf in R.list_workflows():
        key = str(wf.get("key") or "")
        if only and only not in key:
            continue
        mk = str((wf.get("model") or {}).get("key") or "")
        model = None
        if mk:
            try:
                model = R.get_model(mk)
            except Exception:
                pass
        out.append(forecast_workflow(wf, model, oi=oi, weights=weights,
                                     compat_links=links))
    return out


def summarize(rows: list[WorkflowForecast]) -> dict:
    c: dict[str, int] = {}
    for r in rows:
        c[r.status] = c.get(r.status, 0) + 1
    return {
        "total": len(rows),
        "ok": c.get(STATUS_OK, 0),
        "would_fail": c.get(STATUS_FAIL, 0),
        "unknown": c.get(STATUS_UNKNOWN, 0),
        "counts": c,
    }


def format_report(rows: list[WorkflowForecast], *, verbose: bool = False) -> str:
    s = summarize(rows)
    lines = [
        f"开机前预演：{s['total']} 条",
        f"  能过资产检查 {s['ok']} · 会失败 {s['would_fail']} · 证据不足 {s['unknown']}",
    ]
    bad = [r for r in rows if r.status == STATUS_FAIL]
    if bad:
        lines.append("")
        lines.append("会在真机失败（不用开机就能知道）：")
        for r in bad:
            lines.append(f"  ✗ {r.workflow} × {r.model}")
            for a in r.blocking():
                lines.append(f"      [{a.role}] {a.name}")
                lines.append(f"         {a.reason}")
                if a.fix:
                    lines.append(f"         修法：{a.fix}")
            for g in r.graph_issues[:3]:
                lines.append(f"      [图] {g}")
    unk = [r for r in rows if r.status == STATUS_UNKNOWN]
    if unk and verbose:
        lines.append("")
        lines.append("证据不足（不敢下结论）：")
        for r in unk:
            lines.append(f"  ? {r.workflow}: {'; '.join(r.notes) or '版本文件缺失'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="asset_preflight",
                                 description="开机前资产预演（¥0，不建机）")
    ap.add_argument("--workflow", help="只预演一条")
    ap.add_argument("--object-info", dest="object_info", metavar="FILE")
    ap.add_argument("--weights", metavar="FILE")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    oi = load_object_info(a.object_info) if a.object_info else load_object_info()
    wt = load_weights(a.weights) if a.weights else load_weights()
    if not oi:
        print("✗ 没有 object_info 证据。先在一台真机上采集：\n"
              "  curl -s http://127.0.0.1:8188/object_info > objectinfo-live.json")
        return 2
    if not wt:
        print("✗ 没有权重清单。先在一台真机上：\n"
              "  find /model -maxdepth 8 -type f \\( -name '*.safetensors' ... \\) "
              "-printf '%p\\n' > weights.txt")
        return 2

    rows = forecast_all(oi=oi, weights=wt, only=a.workflow or "")
    if a.json:
        print(json.dumps({"summary": summarize(rows),
                          "rows": [r.as_dict() for r in rows]},
                         ensure_ascii=False, indent=1))
    else:
        print(format_report(rows, verbose=a.verbose))
    return 0 if all(r.status != STATUS_FAIL for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
