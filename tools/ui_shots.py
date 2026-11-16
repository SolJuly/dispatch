#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台界面截图：把「H3 工作流参数面板」渲染出来存档。

## 为什么需要（2026-11-16）

改完 H3 卡片（新增 speed_mode / style 两个下拉）之后，我只验证了
`p20ctl resolve` 能跑通，**从没在真界面上确认过控件有没有渲染出来**。
JSON 里有字段不等于表单里有下拉 —— 前端是另一套渲染逻辑。

用户要「H3 工作流界面截图」，正好把这个盲区补上。

## 做法

调 Puppeteer（node）开真 Chrome，打开 `/{TOKEN}/ui/`，点 H3 卡片，
滚动到参数区截图。为什么不用纯 Python：
  标准库里没有任何能渲染 CSS/JS 的东西，而这个界面是 SPA，
  必须先跑 JS 才有 DOM。

## 前置

    cd /tmp/shot && npm i puppeteer
    npx puppeteer browsers install chrome

## 用法

    python3 tools/ui_shots.py                 # 生成到 reports/lab/ui/
    python3 tools/ui_shots.py --keep-tmp      # 保留原始截图
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "reports" / "lab"
OUT = LAB / "ui"
PAGE = LAB / "ui.html"

NODE_SRC = r"""
const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

const TOKEN = fs.readFileSync(process.argv[2], 'utf8').trim();
const OUT = process.argv[3];
const BASE = `http://127.0.0.1:8081/${TOKEN}/ui/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 2400, deviceScaleFactor: 2 });
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await sleep(1500);
  await page.screenshot({ path: path.join(OUT, '01-list.png') });

  // 点 H3 图生视频卡
  await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('*'))
      .find((e) => /图生视频 · MiniMax H3（121 帧）/.test(e.textContent || '')
                   && (e.textContent || '').length < 300);
    if (el) el.click();
  });
  await sleep(3000);
  await page.screenshot({ path: path.join(OUT, '02-selected.png') });

  // 参数区在下方，按 name 定位两个新加的 select
  const info = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('input,select,textarea'))
      .map((e) => {
        const r = e.getBoundingClientRect();
        return {
          tag: e.tagName, name: e.name, type: e.type,
          val: (e.value || '').slice(0, 40),
          top: Math.round(r.top + scrollY),
          opts: e.tagName === 'SELECT'
            ? Array.from(e.options).map((o) => o.value) : undefined,
        };
      });
    const speed = rows.find((r) => r.opts && r.opts.includes('balanced'));
    const style = rows.find((r) => r.opts && r.opts.includes('davham'));
    return { rows, speed, style };
  });
  fs.writeFileSync(path.join(OUT, 'controls.json'),
                   JSON.stringify(info, null, 2));

  if (info.speed) {
    await page.screenshot({
      path: path.join(OUT, '03-params-a.png'),
      clip: { x: 0, y: Math.max(0, info.speed.top - 560), width: 1440, height: 1150 },
    });
  }
  if (info.style) {
    await page.screenshot({
      path: path.join(OUT, '04-params-b.png'),
      clip: { x: 0, y: Math.max(0, info.style.top - 560), width: 1440, height: 1150 },
    });
  }
  await browser.close();
  console.log(JSON.stringify({
    speed_options: info.speed ? info.speed.opts : null,
    style_options: info.style ? info.style.opts : null,
    controls: info.rows.length,
  }));
})();
"""


def run_node(work: Path) -> dict:
    js = work / "shot.js"
    js.write_text(NODE_SRC, encoding="utf-8")
    token = (ROOT / ".share_token").read_text(encoding="utf-8").strip()
    r = subprocess.run(
        ["node", str(js), str(ROOT / ".share_token"), str(OUT)],
        cwd=work, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"截图失败：\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


CSS = """*{box-sizing:border-box}body{margin:0;padding:18px;background:#0e1116;color:#dfe6ee;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:26px 0 10px;color:#c8d4e2}
.sub{color:#8595a8;font-size:13px;margin-bottom:18px}
img{width:100%;display:block;border:1px solid #232c38;border-radius:10px;background:#0a0d11}
figure{margin:0 0 26px}figcaption{color:#8595a8;font-size:12.5px;margin-top:7px}
code{background:#1b222c;padding:1px 5px;border-radius:4px;color:#8fb8de;font-size:12.5px}
.box{background:#151a22;border:1px solid #232c38;border-left:3px solid #2ea043;
border-radius:8px;padding:12px 15px;margin:14px 0;font-size:13.5px}
.warn{border-left-color:#d29922}
.foot{color:#5d6b7d;font-size:12px;margin-top:30px;padding-top:14px;border-top:1px solid #1d2530}"""


def build(meta: dict) -> str:
    def fig(name: str, cap: str) -> str:
        return (f"<figure><img src='ui/{name}' loading='lazy'>"
                f"<figcaption>{cap}</figcaption></figure>")

    L: list[str] = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>H3 工作流 · 控制台界面</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>H3 工作流 · 控制台界面</h1>",
        "<div class='sub'>创作控制台 <code>#/create</code> —— 选工作流、填参数、提交。</div>",
    ]
    if meta.get("speed_options"):
        L.append(
            "<div class='box'><b>两个新下拉都渲染出来了</b>："
            f"<code>speed_mode</code> = {' / '.join(meta['speed_options'])}，"
            f"<code>style</code> = {' / '.join(meta.get('style_options') or [])}。"
            "这是改完卡片后第一次在真界面上确认。</div>")

    L += [
        "<h2>① 工作流列表</h2>",
        fig("01-list.png",
            "工作流卡片墙。每张卡标了引擎、兼容性、模型、卡片名。"
            "右上角是实时预估：工作流、模型、兼容性、预算、参数清单。"),
        "<h2>② 选中 H3 图生视频（121 帧）</h2>",
        fig("02-selected.png",
            "点中后卡片描蓝边，右上角预估切成 <code>h3_i2v_121f v1</code>。"
            "预估随参数变，改宽高/帧数会跟着跳。"),
        "<h2>③ 参数面板 A —— 采样与加速</h2>",
        fig("03-params-a.png",
            "<code>crf</code> · <code>frames</code> · <code>height</code> · "
            "<code>lora_strength</code> · <code>seed</code> · "
            "<code>speed_mode</code> · <code>steps</code>。"
            "每个字段下面都有说明。"),
        "<h2>④ 参数面板 B —— 风格</h2>",
        fig("04-params-b.png",
            "<code>style</code> · <code>style_lora</code> · "
            "<code>style_strength</code> · <code>width</code>。"),
        "<h2>要注意的地方</h2>",
        "<div class='box warn'>"
        "<b>height 显示 512，实际渲染却是 768×768。</b>"
        "H3 卡的描述写着「与宽度配套调整，保持原始素材的宽高比」，"
        "但两个值各自独立，不会自动联动 —— 要出 768×768 得手动改 height。<br><br>"
        "<b>speed_mode 描述里写「实测 409s」，那是旧数据。</b>"
        "现在 balanced 档实测 162s（移植 fasth3 的 kitchen attention + VSA 之后）。"
        "这段说明文字还没跟着更新。</div>",
        "<div class='foot'>截图用 Puppeteer + headless Chrome，2x 缩放。"
        "生成器 <code>tools/ui_shots.py</code>。</div>",
        "</body></html>",
    ]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="控制台界面截图")
    ap.add_argument("--keep-tmp", action="store_true")
    a = ap.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp/shot")
    if not (work / "node_modules" / "puppeteer").exists():
        print("✗ 缺 puppeteer。先跑：")
        print("    mkdir -p /tmp/shot && cd /tmp/shot && npm i puppeteer")
        print("    npx puppeteer browsers install chrome")
        return 2

    print("→ 截图…")
    meta = run_node(work)
    print("  ", json.dumps(meta, ensure_ascii=False))

    PAGE.write_text(build(meta), encoding="utf-8")
    print(f"✓ {PAGE.relative_to(ROOT)}")
    print(f"✓ 截图 {OUT.relative_to(ROOT)}/（*.png 不入库）")
    if not a.keep_tmp:
        for f in work.glob("shot.js"):
            f.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
