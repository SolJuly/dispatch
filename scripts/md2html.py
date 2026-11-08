#!/usr/bin/env python3
"""极简 Markdown → HTML（仅覆盖本项目报告的写法，零第三方依赖）。

支持：ATX 标题、围栏代码块、表格、引用、分隔线、有序/无序列表（单层）、
段落、行内 `code`、**粗体**、*斜体*、[文字](链接)、裸 URL 自动链接。
用途：让 P2.0 报告在公网静态页面上直接可读，而不必下载 .md。
"""
from __future__ import annotations

import html
import re

CSS = """
:root { color-scheme: light dark; }
body { margin:0; background:#f6f7f9; color:#1b1f24;
  font:16px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
main { max-width:960px; margin:0 auto; padding:36px 20px 72px; background:#fff;
  border-left:1px solid #e3e6ea; border-right:1px solid #e3e6ea; min-height:100vh; }
h1 { font-size:27px; margin:0 0 .6em; }
h2 { font-size:21px; margin:1.8em 0 .6em; padding-bottom:6px; border-bottom:1px solid #e3e6ea; }
h3 { font-size:17px; margin:1.5em 0 .4em; }
h4 { font-size:15px; margin:1.2em 0 .3em; }
p { margin:.7em 0; }
a { color:#0b62d6; text-decoration:none; }
a:hover { text-decoration:underline; }
code { background:#eef1f4; padding:1px 5px; border-radius:4px; font-size:13.5px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
pre { background:#11151a; color:#e6edf3; padding:14px 16px; border-radius:10px; overflow:auto; }
pre code { background:none; color:inherit; padding:0; font-size:13px; }
table { width:100%; border-collapse:collapse; margin:1em 0; font-size:14px;
  border:1px solid #e3e6ea; border-radius:10px; overflow:hidden; }
th, td { border-bottom:1px solid #eef1f4; padding:8px 11px; text-align:left; vertical-align:top; }
th { background:#fafbfc; font-weight:600; }
tr:last-child td { border-bottom:none; }
blockquote { margin:1em 0; padding:.3em 1em; border-left:3px solid #cfd6de; color:#4a5462; }
hr { border:none; border-top:1px solid #e3e6ea; margin:1.6em 0; }
ul, ol { margin:.7em 0; padding-left:1.6em; }
li { margin:.25em 0; }
.top { max-width:960px; margin:0 auto; padding:14px 20px 0; font-size:13px; color:#606a76; }
"""


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\"'>=])(https?://[^\s<）)]+)", r'<a href="\1">\1</a>', s)
    return s


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render(md: str, title: str = "") -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        if not stripped:
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue

        if re.match(r"^([-*_])\1{2,}$", stripped):
            out.append("<hr>")
            i += 1
            continue

        # 表格：当前行是 | ... |，下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and re.match(
                r"^\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1].strip()):
            head = _split_row(stripped)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>")
            continue

        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(buf))}</blockquote>")
            continue

        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            items = []
            ordered = True
            while i < n:
                mm = re.match(r"^(\d+)[.)]\s+(.*)$", lines[i].strip())
                if not mm:
                    break
                items.append(f"<li>{_inline(mm.group(2))}</li>")
                i += 1
            out.append(f"<ol>{''.join(items)}</ol>" if ordered else f"<ul>{''.join(items)}</ul>")
            continue

        if re.match(r"^[-*+]\s+", stripped):
            items = []
            while i < n and re.match(r"^[-*+]\s+", lines[i].strip()):
                txt = re.sub(r"^[-*+]\s+", "", lines[i].strip())
                items.append(f"<li>{_inline(txt)}</li>")
                i += 1
            out.append(f"<ul>{''.join(items)}</ul>")
            continue

        buf = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|```|\||>|[-*+]\s|\d+[.)]\s)", lines[i].strip()) and not re.match(
                r"^([-*_])\1{2,}$", lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(buf))}</p>")

    body = "\n".join(out)
    t = html.escape(title or "P2.0")
    return (
        f'<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{t}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        f'<div class="top"><a href="./">← 返回 P2.0 验收材料</a></div>\n'
        f"<main>\n{body}\n</main>\n</body>\n</html>\n"
    )
