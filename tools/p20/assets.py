#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.0 模型资产：把「模型需要哪些权重可见」从 setup 脚本里抽出来。

P1 的 instance_setup.sh 把 H3/Qwen 的 6 个文件名写死在远端 python 里，换模型就得改脚本。
P2.0 的模型配置声明 files（角色→文件+loader/input+匹配规则），这里：

  1. 生成一段远端 Python（base64 传入，避免引号地狱），读 ComfyUI /object_info，
     逐条判断「Loader 的候选列表里有没有这个文件」；
  2. 输出 ASSET_CHECK:{json}，本机解析成 {asset_key: bool}；
  3. 缺失时给出人话原因（loader 不存在 / 文件名不匹配）。

remote check 不依赖 sshpass：调用方传进来一个 ssh_run(cmd) 可调用对象（run.py 已有）。
"""
from __future__ import annotations

import base64
import json
import shlex

REMOTE_PY = r'''
import base64, json, sys, urllib.request
spec = json.loads(base64.b64decode(sys.argv[1]).decode())
try:
    oi = json.loads(urllib.request.urlopen('http://127.0.0.1:8188/object_info', timeout=30).read())
except Exception as e:
    print('ASSET_CHECK:' + json.dumps({'__error__': '%s: %s' % (type(e).__name__, e)}))
    raise SystemExit(0)
res = {}
for a in spec:
    key, loader, inp, rule = a['key'], a['loader'], a['input'], a.get('match') or {}
    try:
        vals = oi[loader]['input']['required'][inp][0]
    except Exception:
        res[key] = {'ok': False, 'why': 'loader/input 不存在: %s.%s' % (loader, inp)}
        continue
    # 新版 ComfyUI：combo 输入写成 ["COMBO", {"options": [...], ...}]
    if isinstance(vals, str) and vals == 'COMBO':
        meta = oi[loader]['input']['required'][inp][1] if len(oi[loader]['input']['required'][inp]) > 1 else {}
        vals = (meta or {}).get('options') or []
    if not isinstance(vals, list):
        vals = [vals]
    if 'equals' in rule:
        ok = any(v == rule['equals'] for v in vals)
        why = '未找到 %s' % rule['equals']
    elif 'contains' in rule:
        ok = any(rule['contains'] in v for v in vals if isinstance(v, str))
        why = '没有包含 %s 的文件' % rule['contains']
    elif 'regex' in rule:
        import re
        ok = any(isinstance(v, str) and re.search(rule['regex'], v) for v in vals)
        why = '没有匹配 /%s/ 的文件' % rule['regex']
    else:
        ok = bool(vals)
        why = '候选列表为空'
    res[key] = {'ok': bool(ok), 'why': '' if ok else why}
print('ASSET_CHECK:' + json.dumps(res, ensure_ascii=False))
'''


def asset_rules(model: dict) -> list[dict]:
    """模型需要可见的资产规则。优先 model.assets；否则由 files 自动派生（equals 文件名）。"""
    explicit = model.get("assets") or []
    if explicit:
        return [{"key": a.get("key") or f"{a['loader']}.{a['input']}",
                 "loader": a["loader"], "input": a["input"],
                 "match": a.get("match") or {"equals": a.get("name") or ""}}
                for a in explicit]
    out = []
    for role, f in (model.get("files") or {}).items():
        rule = f.get("match") or {"equals": f.get("name")}
        out.append({"key": role, "loader": f["loader"], "input": f["input"], "match": rule})
    return sorted(out, key=lambda x: x["key"])


def build_check_command(model: dict, *, activate: str = "source /root/miniconda3/bin/activate") -> str:
    payload = base64.b64encode(json.dumps(asset_rules(model), ensure_ascii=False).encode()).decode()
    b64 = base64.b64encode(REMOTE_PY.encode()).decode()
    return (f"{activate} && python3 -c \"import base64;exec(base64.b64decode('{b64}'))\" {shlex.quote(payload)}")


def parse_check_output(out: str) -> tuple[dict | None, str]:
    """从 ssh 输出里找 ASSET_CHECK:{...}；返回 (结果字典, 错误说明)。"""
    for line in (out or "").splitlines():
        i = line.find("ASSET_CHECK:")
        if i < 0:
            continue
        try:
            d = json.loads(line[i + len("ASSET_CHECK:"):])
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            return None, "ASSET_CHECK 不是对象"
        if "__error__" in d:
            return None, str(d["__error__"])
        return d, ""
    return None, "没有 ASSET_CHECK 输出（ComfyUI 未就绪或远端 python 失败）"


def run_check(model: dict, ssh_run, *, attempts: int = 20, interval: float = 6.0,
              timeout: int = 90) -> tuple[bool, list[str], dict]:
    """轮询远端资产检查。ssh_run(cmd, timeout=...) -> (rc, out)。"""
    import time

    last = ""
    for i in range(attempts):
        rc, out = ssh_run(build_check_command(model), timeout=timeout)
        res, err = parse_check_output(out)
        if res is not None:
            missing = [k for k, v in res.items() if not (v or {}).get("ok")]
            why = {k: (v or {}).get("why", "") for k, v in res.items() if not (v or {}).get("ok")}
            return (not missing), missing, {"result": res, "why": why, "attempt": i + 1}
        last = err or (out or "")[-200:]
        if i + 1 < attempts:
            time.sleep(interval)
    return False, [], {"error": last}


def apply_links(model: dict, ssh_run, *, timeout: int = 60) -> list[dict]:
    """执行模型配置里的 setup.links（例：把断链软链到真实文件）。不配则零操作。"""
    out = []
    for ln in ((model.get("setup") or {}).get("links") or []):
        link, target = ln.get("link"), ln.get("target")
        if not link or not target:
            continue
        cmd = f"mkdir -p {shlex.quote(str(link.rsplit('/', 1)[0]))} && ln -sf {shlex.quote(target)} {shlex.quote(link)} && echo LINK_OK"
        rc, txt = ssh_run(cmd, timeout=timeout)
        out.append({"link": link, "target": target, "ok": rc == 0 and "LINK_OK" in (txt or ""),
                    "out": (txt or "")[-160:]})
    return out


def setup_profile(model: dict) -> str:
    """模型配置里的 setup.profile（h3/qwen/all/none），供 instance_setup.sh --need。"""
    return ((model.get("setup") or {}).get("profile") or "all")
