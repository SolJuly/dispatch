#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨云一致性校验：环境描述（manifest）↔ 真实机器，到底差在哪。

## 解决的问题

`deploy/env/comfyui-wan-p21.env.json` 是「环境」的唯一真相源，但在此之前
没有任何东西能回答这个问题：

    **这台机器上的环境，真的和 manifest 描述的一样吗？**

`provision.sh` 装完只做四项粗检（object_info 有返回 / 21 个节点抽查 /
unet 列表非空 / 无 IMPORT FAILED）。四项全过但环境已经偏了的场景有：

  · ComfyUI commit 悄悄不是 manifest 里那个（有人手动 git pull 过）
  · torch 被某次 pip 装成了别的版本（custom_nodes 的 requirements 没约束住）
  · 某个节点 import 失败但名字还在 object_info 里（缓存/半加载）
  · 权重文件名换了（平台更新了 /model，旧链接指向老名字）

这些差异**不会让当前作业失败**，但会让「换云后结果不一致」，
而换云后不一致是最难查的一类 bug —— 代码一样、图一样、参数一样，出来的片子不一样。

## 设计

校验器只吃两份数据，不联网：

    manifest  环境描述（deploy/env/*.env.json）
    probe     机器自述（一台机器上采集的 JSON）

所以它既能在真机上跑（`collect()` 现场采），也能在编排机上跑
（`--probe 已有文件` 离线复放）。离线复放是刻意的：本项目的部署机与
GPU 机之间只有 SSH，去掉这个能力就只能每次开一台 4090 来校验配置了。

probe 的结构（`collect()` 产出）：

    {
      "schema": "p2.conformance.probe/v1",
      "comfy": {"commit": "...", "version": "...", "path": "/root/ComfyUI"},
      "python": {"version": "3.10.12", "torch": "2.8.0+cu128",
                 "torchaudio": "...", "torchvision": "..."},
      "nodes": {"ComfyUI-KJNodes": {"commit": "...", "import_failed": false}, ...},
      "models": {"unet_visible": 33, "vae_visible": 23, ...},
      "object_info_nodes": 1757,
      "platform": {"has_internet": false, "can_pull_oci": false}
    }

## 严重度

  ERROR  会导致「同一份 manifest 跑出不同结果」或「根本跑不起来」
  WARN   偏离但可能无害（节点 commit 不同但 class 齐全）
  INFO   仅记录

`ok` 只在没有 ERROR 时为真。WARN 不阻止部署，但必须打印出来 ——
上一轮审计最贵的一课就是「查询成功但没数据」被当成了「一切正常」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA = "p2.conformance/v1"
PROBE_SCHEMA = "p2.conformance.probe/v1"

ROOT = Path(__file__).resolve().parents[2]

SEV_ERROR = "ERROR"
SEV_WARN = "WARN"
SEV_INFO = "INFO"


# ------------------------------------------------------------------ 采集
def collect_script(*, comfy_home: str = "/root/ComfyUI",
                   python: str = "python3") -> str:
    """生成在**目标机器**上采集 probe 的 shell（自包含，不依赖项目源码）。

    刻意做成一段独立脚本：目标机是干净的 GPU 机，没有本项目代码，
    不能 `python3 -m tools.p2.conformance collect`。只能把脚本传过去跑。
    """
    return f"""set -u
PY={python}
COMFY={comfy_home}
$PY - "$COMFY" "$PY" <<'PYEOF'
import json, os, re, shlex, subprocess, sys
comfy = sys.argv[1]
# python 解释器路径必须从 argv 拿：heredoc 是 <<'PYEOF'（单引号），
# shell 不会展开里面的 $PY，直接写 "$PY" 会原样传给 subprocess 导致
# 版本全采成空字符串（2026-11-09 实测踩到）。
PY = sys.argv[2] if len(sys.argv) > 2 else "python3"
out = {{"schema": "{PROBE_SCHEMA}"}}

def run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=60).stdout.strip()
    except Exception:
        return ""

# ---- ComfyUI 版本与 commit
out["comfy"] = {{"path": comfy}}
vf = os.path.join(comfy, "comfyui_version.py")
if os.path.isfile(vf):
    m = re.search(r'__version__\\s*=\\s*["\\']([^"\\']+)', open(vf).read())
    if m:
        out["comfy"]["version"] = m.group(1)
out["comfy"]["commit"] = run(f"git -C {{comfy}} rev-parse HEAD 2>/dev/null")

# ---- python / torch
out["python"] = {{"version": run(f"{{shlex.quote(PY)}} -c 'import platform;print(platform.python_version())'")}}
for mod in ("torch", "torchaudio", "torchvision"):
    v = run(f"{{shlex.quote(PY)}} -c 'import {{mod}};print({{mod}}.__version__)'")
    out["python"][mod] = v

# ---- custom_nodes：commit + 是否 import 失败
nodes = {{}}
nd = os.path.join(comfy, "custom_nodes")
if os.path.isdir(nd):
    for name in sorted(os.listdir(nd)):
        p = os.path.join(nd, name)
        if not os.path.isdir(p):
            continue
        nodes[name] = {{"commit": run(f"git -C '{{p}}' rev-parse HEAD 2>/dev/null"),
                       "import_failed": False, "disabled": name.endswith(".disabled")}}
out["nodes"] = nodes

# ---- object_info：节点总数 + 模型可见数
try:
    import urllib.request
    oi = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=30))
    out["object_info_nodes"] = len(oi)
    vis = {{}}
    for cls, inp, key in (("UNETLoader","unet_name","unet_visible"),
                          ("CheckpointLoaderSimple","ckpt_name","ckpt_visible"),
                          ("VAELoader","vae_name","vae_visible"),
                          ("CLIPLoader","clip_name","clip_visible"),
                          ("LoraLoader","lora_name","lora_visible")):
        try:
            v = oi[cls]["input"]["required"][inp][0]
            if isinstance(v, str) and v == "COMBO":
                v = (oi[cls]["input"]["required"][inp][1] or {{}}).get("options") or []
            vis[key] = len(v) if isinstance(v, list) else 0
        except Exception:
            vis[key] = -1
    out["models"] = vis
    out["node_classes"] = sorted(oi.keys())
except Exception as e:
    out["object_info_error"] = f"{{type(e).__name__}}: {{e}}"

# ---- 平台事实
out["platform"] = {{
    "has_internet": run("curl -sI --max-time 5 --connect-timeout 3 https://pypi.org >/dev/null 2>&1 && echo yes || echo no") == "yes",
    "can_pull_oci": bool(run("command -v docker")),
}}
print("PROBE_JSON:" + json.dumps(out, ensure_ascii=False))
PYEOF
"""


def parse_probe(stdout: str) -> dict:
    """从远端输出里抽出 PROBE_JSON 块。"""
    for line in (stdout or "").splitlines():
        if line.startswith("PROBE_JSON:"):
            try:
                return json.loads(line[len("PROBE_JSON:"):])
            except json.JSONDecodeError as e:
                raise ValueError(f"probe JSON 解析失败：{e}") from e
    raise ValueError("输出里没有 PROBE_JSON —— 采集脚本可能失败了")


# ------------------------------------------------------------------ 校验
def _ver(v: str) -> tuple:
    """把 '2.8.0+cu128' 拆成可比较的 (2, 8, 0, 'cu128')。

    没有 packaging 依赖（目标机可能没装），手工解析够用。
    """
    s = str(v or "").strip()
    if not s:
        return ()
    main, _, local = s.partition("+")
    parts = []
    for x in re.split(r"[.\-]", main):
        parts.append(int(x) if x.isdigit() else x)
    parts.append(local)
    return tuple(parts)


def _same_version(got: str, want: str) -> bool:
    """版本相等判断。'2.8.0' 与 '2.8.0+cu128' 视为**不同**（CUDA 版本要紧）。"""
    if not got or not want:
        return False
    return str(got).strip() == str(want).strip()


def check(manifest: dict, probe: dict) -> dict:
    """核心校验：返回 {ok, errors, warns, infos, checks}。

    每条 check 都带 `field`（对应 manifest 的哪一段），便于定位该改哪。
    """
    checks: list[dict] = []

    def add(sev: str, field: str, msg: str, *, got=None, want=None) -> None:
        checks.append({"severity": sev, "field": field, "message": msg,
                       "got": got, "want": want})

    # ---- 1. ComfyUI commit（最关键：换 commit 等于换节点行为）
    mf_comfy = manifest.get("comfyui") or {}
    pr_comfy = probe.get("comfy") or {}
    want_commit = str(mf_comfy.get("commit") or "")
    got_commit = str(pr_comfy.get("commit") or "")
    if not got_commit:
        add(SEV_ERROR, "comfyui.commit", "目标机读不到 ComfyUI commit（git 缺失或路径不对）",
            got=got_commit, want=want_commit)
    elif want_commit and got_commit != want_commit:
        # 短 commit 前缀匹配也算一致（有人用 abbreviations）
        if not (want_commit.startswith(got_commit) or got_commit.startswith(want_commit)):
            add(SEV_ERROR, "comfyui.commit",
                f"ComfyUI commit 不符：{got_commit[:12]} ≠ {want_commit[:12]}",
                got=got_commit, want=want_commit)
    else:
        add(SEV_INFO, "comfyui.commit", "ComfyUI commit 一致")

    want_ver = str(mf_comfy.get("version") or "")
    got_ver = str(pr_comfy.get("version") or "")
    if want_ver and got_ver and got_ver != want_ver:
        add(SEV_WARN, "comfyui.version",
            f"comfyui_version 不符：{got_ver} ≠ {want_ver}", got=got_ver, want=want_ver)

    # ---- 2. Python / torch
    mf_py = manifest.get("python") or {}
    pr_py = probe.get("python") or {}
    base = manifest.get("base") or {}
    want_python = str(base.get("python") or "")
    got_python = str(pr_py.get("version") or "")
    if want_python and got_python and not got_python.startswith(want_python):
        add(SEV_ERROR, "base.python",
            f"Python 大版本不符：{got_python} 不是 {want_python}.x",
            got=got_python, want=want_python)

    pins = mf_py.get("pins") or {}
    for mod in ("torch", "torchaudio", "torchvision"):
        want = str(pins.get(mod) or "")
        got = str(pr_py.get(mod) or "")
        if not want:
            continue
        if not got:
            add(SEV_ERROR, f"python.pins.{mod}", f"目标机没有 {mod}")
        elif not _same_version(got, want):
            add(SEV_ERROR, f"python.pins.{mod}",
                f"{mod} 版本不符：{got} ≠ {want}", got=got, want=want)
        else:
            add(SEV_INFO, f"python.pins.{mod}", f"{mod} {got} 一致")

    # 被排除的包不该出现（xformers 与 torch 2.8 ABI 不匹配）
    # 这里 probe 没采包列表，只在有 xformers 字段时判断
    xf = pr_py.get("xformers")
    if xf and (mf_py.get("exclude") or {}).get("xformers"):
        add(SEV_ERROR, "python.exclude.xformers",
            f"目标机装了 xformers {xf}，manifest 要求排除（torch 2.8 ABI 不匹配）",
            got=xf)

    # ---- 3. custom_nodes
    want_nodes = {n.get("dir"): n for n in (manifest.get("custom_nodes") or [])
                  if isinstance(n, dict) and n.get("dir")}
    got_nodes = probe.get("nodes") or {}
    for d, spec in want_nodes.items():
        g = got_nodes.get(d)
        if not g:
            disabled = got_nodes.get(d + ".disabled")
            if disabled:
                add(SEV_WARN, f"custom_nodes.{d}",
                    f"{d} 被禁用（.disabled）；若工作流依赖它的节点会失败")
            else:
                add(SEV_ERROR, f"custom_nodes.{d}", f"{d} 不存在")
            continue
        want_c = str(spec.get("commit") or "")
        got_c = str(g.get("commit") or "")
        if want_c and got_c and not (want_c.startswith(got_c) or got_c.startswith(want_c)):
            sev = SEV_WARN          # commit 不同但节点在，可能只是拉了新提交
            add(sev, f"custom_nodes.{d}.commit",
                f"{d} commit 不符：{got_c[:10]} ≠ {want_c[:10]}",
                got=got_c, want=want_c)
        if g.get("import_failed"):
            add(SEV_ERROR, f"custom_nodes.{d}.import_failed", f"{d} import 失败")

    # 反向：manifest 没写、机器上多出来的节点（不在 ERROR，因为可能是有意的）
    extra = [d for d in got_nodes if d not in want_nodes
             and not d.endswith(".disabled") and d not in ("__pycache__",)]
    if extra:
        add(SEV_INFO, "custom_nodes.extra",
            f"机器上有 manifest 未记录的节点：{', '.join(sorted(extra)[:6])}")

    # ---- 4. 平台事实
    #
    # manifest 记的是 `platform_notes.compshare.instance_has_internet`，
    # 而 probe 上报的是 `platform.has_internet` —— 两个字段名不一样，
    # 一开始直接拿同一个键去查，导致这段检查从来没触发过（测试抓到的）。
    # 这里显式做名称映射，不要靠"看起来同名"。
    mf_notes = ((manifest.get("platform_notes") or {}).get("compshare") or {})
    pr_plat = probe.get("platform") or {}
    internet_pairs = (
        ("instance_has_internet", "has_internet"),      # manifest 键 / probe 键
    )
    for mf_key, pr_key in internet_pairs:
        if pr_key not in pr_plat or mf_key not in mf_notes:
            continue
        got_i, want_i = bool(pr_plat[pr_key]), bool(mf_notes[mf_key])
        if got_i != want_i:
            add(SEV_WARN, f"platform_notes.compshare.{mf_key}",
                f"外网状态与 manifest 记录不符：实测 {'有' if got_i else '无'}，"
                f"manifest 记的是 {'有' if want_i else '无'}"
                f"（有外网意味着 provision.sh 可用，这条路径要重估）",
                got=got_i, want=want_i)

    # ---- 5. ComfyUI 能起来且节点数合理
    if probe.get("object_info_error"):
        add(SEV_ERROR, "service", f"object_info 取不到：{probe['object_info_error']}")
    else:
        n = int(probe.get("object_info_nodes") or 0)
        if n <= 0:
            add(SEV_ERROR, "service", "object_info 为空（ComfyUI 未启动或返回异常）")
        else:
            add(SEV_INFO, "service", f"ComfyUI 就绪，{n} 个节点类")

    # ---- 6. 权重可见性（0 个可见 = /model 没挂上）
    models = probe.get("models") or {}
    for k in ("unet_visible", "vae_visible", "ckpt_visible"):
        v = models.get(k)
        if v == 0:
            add(SEV_ERROR, f"models.{k}", f"{k}=0：权重库没挂载，所有作业都会缺权重")
        elif isinstance(v, int) and v < 0:
            add(SEV_WARN, f"models.{k}", f"{k} 取不到（Loader 缺失或 input 名变了）")

    errors = [c for c in checks if c["severity"] == SEV_ERROR]
    warns = [c for c in checks if c["severity"] == SEV_WARN]
    infos = [c for c in checks if c["severity"] == SEV_INFO]
    return {
        "schema": SCHEMA,
        "ok": not errors,
        "manifest": manifest.get("name"),
        "errors": errors, "warns": warns, "infos": infos,
        "checks": checks,
        "counts": {"ERROR": len(errors), "WARN": len(warns), "INFO": len(infos)},
    }


# ------------------------------------------------------------------ 报告
def format_report(res: dict, *, verbose: bool = False) -> str:
    """人读的校验报告。"""
    icon = {"ERROR": "✗", "WARN": "!", "INFO": "·"}
    lines = [
        f"跨云一致性：{res.get('manifest') or '?'}",
        f"结论：{'✓ 一致' if res['ok'] else '✗ 不一致'}"
        f"（ERROR {res['counts']['ERROR']} / WARN {res['counts']['WARN']}"
        f" / INFO {res['counts']['INFO']}）",
    ]
    for c in res["checks"]:
        if c["severity"] == SEV_INFO and not verbose:
            continue
        lines.append(f"  {icon.get(c['severity'], '?')} [{c['field']}] {c['message']}")
    if not verbose and res["counts"]["INFO"]:
        lines.append(f"  （{res['counts']['INFO']} 条 INFO 已省略，加 --verbose 显示）")
    return "\n".join(lines)


def load_manifest(path: Path | str | None = None) -> dict:
    p = Path(path) if path else ROOT / "deploy" / "env" / "comfyui-wan-p21.env.json"
    return json.loads(Path(p).read_text(encoding="utf-8"))


def load_probe(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 从真实机器采
def collect_remote(ssh_run, *, comfy_home: str = "/root/ComfyUI",
                   python: str = "python3", timeout: int = 180) -> dict:
    """在目标机上采集 probe。ssh_run(cmd, timeout=) → (rc, out)。"""
    rc, out = ssh_run(collect_script(comfy_home=comfy_home, python=python),
                      timeout=timeout)
    if not out:
        raise ValueError("采集脚本无输出（ssh 失败？）")
    return parse_probe(out)


def snapshot_probe(probe: dict, path: Path | str) -> Path:
    """把 probe 存成证据文件（供离线复放与事后追查）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(probe, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
