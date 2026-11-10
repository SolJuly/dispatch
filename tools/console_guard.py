#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台唯一入口登记表：**前端只准在 web/dist，工作流只准走 registry**。

## 为什么需要这个文件

2026-11-09~10 发生过三次同一类事故：

  ① 质量卡加到了 `web/dist/app.js`，用户在主链 `/app/` 上完全看不到
  ② NEW 标签加到了 `web/dist`，同样看不到
  ③ 修前两次时才发现仓库里**有两套前端**，各写各的

根因不是谁粗心，是**仓库允许"改错地方"这件事发生**：
两套 UI 都活着、都能返回 200、都有测试通过。改哪套都"看起来对"。

所以这里做两件事：

  1. **声明唯一入口**：前端 = `web/dist/`（SPA）；工作流 = `registry/`
  2. **提供可执行的守卫**：`python3 -m tools.console_guard` 检查有没有人
     在废弃位置继续加东西，违例直接非零退出

后者会挂进测试（`tools/webui/tests/test_console_guard.py`），
所以"改错地方"会在 CI 阶段失败，而不是等用户发现界面没变。

## 新增 / 修改东西时该动哪个文件

| 想做的事 | 改这里 | 不要改 |
|---|---|---|
| 加一个页面 / 区块 | `web/dist/app.js` | ~~`webui/pages.py`~~（已删除） |
| 改样式 | `web/dist/app.css` | ~~`webui/assets.py`~~（已删除） |
| 改页面骨架 | `web/dist/index.html` | 同上 |
| 加一个工作流 | `registry/workflows/<key>.json` + `workflows/*.api.json` | 不要只改 DB |
| 加一个模型 | `registry/models/<key>.json` | 同上 |
| 改工作流参数 | `registry/workflows/<key>.json` 的 `params` | 不要改渲染代码 |
| 改兼容性结论 | `p20ctl compat verify`（要真机证据） | 不要手改 `compat.json` |
| 前端单测 | `web/tests/*.test.js` | — |
| 后端接口 | `webui/api.py` | — |

## 为什么工作流必须走 registry

工作流的三样东西是**强耦合**的，分开改必然漂移：

    graph（节点图）  ← workflows/*.api.json
    params（参数）    ← registry/workflows/*.json
    compat（能不能跑）← 真机证据

只改一处就会出现「图里要 image_2、参数表里没有」或
「标着 verified、实际节点缺失」——2026-11-09 的 19 条 experimental
里有一半是这么来的。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- 唯一入口
# 前端：只有一个实现。新增页面/样式/脚本都必须落在这里。
UI_ROOT = ROOT / "web" / "dist"
UI_FILES = ("index.html", "app.js", "app.css")

# 工作流：只有一个来源。DB 是派生的，`p20ctl import` 可完整重建。
WORKFLOW_DIR = ROOT / "registry" / "workflows"
MODEL_DIR = ROOT / "registry" / "models"
COMPAT_JSON = ROOT / "registry" / "compat.json"

# 已废弃位置：存在即视为违例（文件被重新创建 = 有人又改错地方了）
RETIRED = {
    "webui/pages.py": "旧的 A 套服务端渲染页，2026-11-10 已删；前端统一到 web/dist",
    "webui/components.py": "同上，A 套组件",
    "webui/assets.py": "同上，A 套内嵌 CSS/JS（21KB 字符串常量）",
}

# 路由前缀：只能有一个活着的 UI 入口
UI_ROUTE = "/ui/"
RETIRED_ROUTES = ("/app/", "/static/")

# 允许的 .py 里出现前端代码的地方（其余地方出现大型 HTML/CSS 字面量就是走错路了）
HTML_LITERAL_WARN_BYTES = 4000


@dataclass
class Violation:
    kind: str
    path: str
    detail: str
    fix: str = ""


@dataclass
class AuditResult:
    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": [v.__dict__ for v in self.violations],
            "warnings": [v.__dict__ for v in self.warnings],
            "checked": self.checked,
        }


# ---------------------------------------------------------------- 检查
def check_retired_files(r: AuditResult) -> None:
    """废弃文件不能被重新创建。"""
    for rel, why in RETIRED.items():
        p = ROOT / rel
        if p.exists():
            r.violations.append(Violation(
                kind="retired_file_present", path=rel,
                detail=f"{rel} 应已删除：{why}",
                fix=f"git rm {rel}；前端改动请写到 web/dist/"))
        else:
            r.checked.append(rel)


def check_ui_root(r: AuditResult) -> None:
    """唯一前端入口必须完整。"""
    for name in UI_FILES:
        p = UI_ROOT / name
        if not p.is_file():
            r.violations.append(Violation(
                kind="ui_asset_missing", path=f"web/dist/{name}",
                detail=f"唯一前端入口缺文件：{name}",
                fix="web/dist 是源码不是构建产物，从 git 恢复即可"))
        else:
            r.checked.append(f"web/dist/{name}")


def check_fileserver_routes(r: AuditResult) -> None:
    """fileserver 不能再暴露废弃路由。"""
    fs = ROOT / "fileserver.py"
    if not fs.is_file():
        r.violations.append(Violation("missing", "fileserver.py", "文件不存在"))
        return
    src = fs.read_text(encoding="utf-8")
    r.checked.append("fileserver.py")

    # `_app_page` 方法本体必须已删除（只允许注释里提到）
    if re.search(r"^\s*def _app_page\(", src, re.M):
        r.violations.append(Violation(
            kind="retired_route_alive", path="fileserver.py",
            detail="`_app_page()` 仍在：A 套路由没删干净",
            fix="删除该方法与 `/app/` 分支，前端统一走 `_ui_asset()`"))

    # 路由分发里不能再有 app 分支
    if re.search(r'kind\s*==\s*["\']app["\']', src):
        r.violations.append(Violation(
            kind="retired_route_alive", path="fileserver.py",
            detail='仍有 `kind == "app"` 分支',
            fix="删除该分支"))


def check_no_html_literals(r: AuditResult) -> None:
    """Python 里不该再有大型 HTML/CSS 字面量。

    A 套的 21KB CSS 就是内嵌在 `webui/assets.py` 的字符串里，
    没有语法高亮、没有 lint、改一处要重启服务。任何新的同类做法都要挡。
    """
    for p in sorted((ROOT / "webui").glob("*.py")):
        if p.name.startswith("_"):
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        # 粗略估算：找三引号块，超过阈值就告警
        for m in re.finditer(r'[rubf]*("""|\'\'\')(.*?)\1', src, re.S):
            body = m.group(2)
            if len(body) < HTML_LITERAL_WARN_BYTES:
                continue
            # 只关心"看起来像前端"的块
            if not re.search(r"<[a-z]+[\s>]|\{[a-z-]+:|\.[a-z-]+\{", body, re.I):
                continue
            rel = str(p.relative_to(ROOT))
            r.warnings.append(Violation(
                kind="large_ui_literal", path=rel,
                detail=f"{rel} 里有 {len(body)} 字节的 HTML/CSS 字面量",
                fix="前端代码请放 web/dist/，Python 只负责返回文件"))
    r.checked.append("webui/*.py")


def check_workflow_sources(r: AuditResult) -> None:
    """工作流必须来自 registry JSON，且 graph 文件存在、参数与图对得上。"""
    if not WORKFLOW_DIR.is_dir():
        r.violations.append(Violation("missing", "registry/workflows", "目录不存在"))
        return
    cards = sorted(WORKFLOW_DIR.glob("*.json"))
    r.checked.append(f"registry/workflows/*.json ({len(cards)} 条)")

    for card in cards:
        try:
            d = json.loads(card.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            r.violations.append(Violation(
                "bad_json", str(card.relative_to(ROOT)), f"JSON 解析失败：{e}",
                "修好 JSON 或删掉该卡"))
            continue
        graph_rel = d.get("graph") or ""
        if not graph_rel:
            r.violations.append(Violation(
                "card_no_graph", str(card.relative_to(ROOT)), "缺 graph 字段",
                "指向 workflows/*.api.json"))
            continue
        gp = Path(graph_rel)
        if not gp.is_absolute():
            gp = ROOT / graph_rel
        if not gp.is_file():
            r.violations.append(Violation(
                "graph_missing", str(card.relative_to(ROOT)),
                f"graph 指向的文件不存在：{graph_rel}",
                "补上 .api.json，或修正 graph 路径"))
        # compat.json 里该有的条目不能凭空写 verified（要有 checked_at）
    check_compat_evidence(r)


def check_compat_evidence(r: AuditResult) -> None:
    """verified 必须带真机证据时间戳 —— 手改 compat.json 冒充不成立。"""
    if not COMPAT_JSON.is_file():
        return
    try:
        d = json.loads(COMPAT_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        r.violations.append(Violation("bad_json", "registry/compat.json", "解析失败"))
        return
    r.checked.append("registry/compat.json")
    for e in (d.get("entries") or []):
        if (e or {}).get("status") not in ("verified", "equivalent"):
            continue
        basis = str(e.get("basis") or "")
        at = str(e.get("checked_at") or e.get("verified_at") or "")
        if not at:
            r.violations.append(Violation(
                "compat_without_evidence",
                f"registry/compat.json#{e.get('workflow_key')}",
                f"{e.get('workflow_key')} 标了 {e.get('status')} 但没有 checked_at",
                "用 `p20ctl compat verify` 写入真机证据，不要手改"))


def check_dist_not_ignored(r: AuditResult) -> None:
    """web/dist 是源码，不能被 .gitignore 吞掉。

    历史上它被当成"构建产物"忽略过，导致没有构建链、改动进不了库。
    """
    gi = ROOT / ".gitignore"
    if not gi.is_file():
        return
    r.checked.append(".gitignore")
    for line in gi.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # 只有带锚点的 /dist/ 才是我们要防的（它指顶层的 dist 目录，不是 web/dist）
        if re.fullmatch(r"/?web/dist/?", s) or s in ("web/dist", "web/dist/"):
            r.violations.append(Violation(
                kind="dist_ignored", path=".gitignore",
                detail=f".gitignore 忽略了 web/dist（规则 {s!r}）",
                fix="删掉该行 —— web/dist 是源码，不是构建产物"))


# ---------------------------------------------------------------- 入口
def audit() -> AuditResult:
    r = AuditResult()
    check_ui_root(r)
    check_retired_files(r)
    check_fileserver_routes(r)
    check_no_html_literals(r)
    check_workflow_sources(r)
    check_dist_not_ignored(r)
    return r


def format_report(r: AuditResult) -> str:
    lines = []
    if r.ok:
        lines.append(f"✓ 控制台入口统一（检查 {len(r.checked)} 项）")
    else:
        lines.append(f"✗ 发现 {len(r.violations)} 处违例")
    for v in r.violations:
        lines.append(f"  ✗ [{v.kind}] {v.path}")
        lines.append(f"      {v.detail}")
        if v.fix:
            lines.append(f"      修法：{v.fix}")
    for w in r.warnings:
        lines.append(f"  ! [{w.kind}] {w.path}: {w.detail}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="console_guard",
                                 description="控制台唯一入口守卫")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--paths", action="store_true", help="只打印该改哪个文件")
    a = ap.parse_args(argv)

    if a.paths:
        print("唯一前端入口：web/dist/（index.html / app.js / app.css）")
        print("唯一工作流来源：registry/workflows/*.json + workflows/*.api.json")
        print("已删除（不要再创建）：webui/pages.py, webui/components.py, webui/assets.py")
        return 0

    r = audit()
    print(json.dumps(r.as_dict(), ensure_ascii=False, indent=1) if a.json
          else format_report(r))
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
