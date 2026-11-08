#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ComfyUI 协议适配：把「提交 / 轮询 / 取产物」的解析逻辑从 run.py 里抽出来。

run.py 仍然通过 SSH 在实例内调 127.0.0.1:8188（安全边界不变），但：
  - 提交返回怎么解析 prompt_id
  - history 条目怎么判成功/失败/取文件
  - HTTP 直连（mock 测试 / 未来内网直连 / 端口转发）

都在这里，纯函数 + 标准库，单元测试不需要 GPU。
"""
from __future__ import annotations

import json
import re
import urllib.request

PROMPT_ID_RE = re.compile(r'"prompt_id"\s*:\s*"([^"]+)"')


class ComfyError(Exception):
    """ComfyUI 协议级错误。"""


def parse_prompt_id(text: str) -> str | None:
    m = PROMPT_ID_RE.search(text or "")
    return m.group(1) if m else None


def parse_history(text: str) -> dict | None:
    """从 SSH 输出里找第一段完整 JSON（允许前后有 shell 噪声）。"""
    s = (text or "").strip()
    i = s.find("{")
    if i < 0:
        return None
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(s[i:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def status_of(entry: dict) -> str | None:
    st = (entry or {}).get("status") or {}
    return st.get("status_str")


def extract_error(entry: dict) -> str:
    st = (entry or {}).get("status") or {}
    for m in st.get("messages") or []:
        if isinstance(m, list) and m and m[0] == "execution_error":
            info = m[1] or {}
            return f"{info.get('node_type')}: {info.get('exception_message')}"
    return str(st.get("messages"))[:600]


def extract_files(entry: dict, kinds=("images", "videos", "gifs")) -> list[tuple[str, str]]:
    """收集真正落盘的产物。

    只保留 type == "output"：LoadVideo 回显的输入（type=input）、预览节点
    （type=temp）都会被 history 记进 outputs，但不在 /root/ComfyUI/output 下，
    拉取时必然 404。
    """
    out = []
    for o in ((entry or {}).get("outputs") or {}).values():
        for k in kinds:
            for f in (o.get(k) or []):
                if (f.get("type") or "output") != "output":
                    continue
                out.append((f.get("subfolder", ""), f.get("filename")))
    return sorted(set(out))


def extract_files_by_node(entry: dict, kinds=("images", "videos", "gifs")
                          ) -> list[tuple[str, str, str]]:
    """同 extract_files，但带上产出节点 id：(node_id, subfolder, filename)。

    主产物判定不能靠文件名排序：多路输出时封面 png 可能排在正片前面，
    靠「排序第一」会把整单按图片校验（冻结/黑帧全跳过）或误杀。
    """
    out = []
    for nid, o in ((entry or {}).get("outputs") or {}).items():
        for k in kinds:
            for f in (o.get(k) or []):
                if (f.get("type") or "output") != "output":
                    continue
                out.append((str(nid), f.get("subfolder", ""), f.get("filename")))
    return sorted(set(out))


def history_url(host: str = "127.0.0.1", port: int = 8188, pid: str = "") -> str:
    return f"http://{host}:{port}/history/{pid}"


def prompt_url(host: str = "127.0.0.1", port: int = 8188) -> str:
    return f"http://{host}:{port}/prompt"


def view_url(host: str = "127.0.0.1", port: int = 8188, filename: str = "",
             subfolder: str = "", type_: str = "output") -> str:
    from urllib.parse import urlencode
    q = urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
    return f"http://{host}:{port}/view?{q}"


def http_post_json(url: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def http_get_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def http_download(url: str, dest, timeout: int = 300) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
        data = r.read()
        f.write(data)
    return len(data)
