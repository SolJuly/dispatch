#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板自带示例素材不得残留在**可选**媒体槽位上。

## 为什么要有这条测试（2026-11-14 真机实测踩到）

`pack_video_ltx2_3_id_lora` 提交时被 ComfyUI 拒：

    LoadAudio: audio - Invalid audio file: ltx23_reference_audio.mp3

图里 `LoadAudio.inputs.audio` 硬编码了模板作者自己的参考音频名。
这个参数在卡上是 `required: false`（调用方可以不给音频），于是：

    实际链路：调用方不给 ref_audio
             → render 不给节点 276 赋新值
             → 图里保留 "ltx23_reference_audio.mp3"
             → 实例上没这个文件
             → 提交被拒（还已经建了机、花了钱）

关键区分：**必填**媒体参数由调用方提供名字，图里那个示例名会被覆盖，
不会残留；只有**可选**参数不给时才会留下模板名字。

## 正确写法

`LoadAudio{audio:""}` / `LoadImage{image:""}` —— 空值。

`render._drop_empty_loaders()` 已经实现「空加载器 → 删节点」，
并且会连带删孤儿节点、清理指向已删节点的输入。所以卡片作者只需要
把示例文件名清空，不需要额外机制。

（这条测试不检查**必填**参数的示例名：那些一定会被覆盖，
而且保留示例名对读图的人是有价值的参考。）
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

WF_DIR = _ROOT / "registry" / "workflows"

# 加载器类 → 它读文件名的那个输入
LOADER_FIELD = {
    "LoadImage": "image",
    "LoadAudio": "audio",
    "LoadVideo": "file",
}


def _graph_of(card: dict) -> dict:
    """卡片里的 graph 可能是内联 dict，也可能是相对路径字符串。"""
    g = card.get("graph")
    if isinstance(g, dict):
        return g
    if isinstance(g, str):
        p = _ROOT / g
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _cards() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(WF_DIR.glob("*.json"))]


class TestOptionalMediaDefaults(unittest.TestCase):
    def test_optional_media_slots_have_no_hardcoded_sample_file(self):
        """可选媒体参数绑定的加载器，图里不能留模板示例文件名。"""
        bad = []
        for card in _cards():
            g = _graph_of(card)
            if not g:
                continue
            for pname, spec in (card.get("params") or {}).items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("type") not in ("image", "video", "audio"):
                    continue
                if spec.get("required"):
                    continue          # 必填会被覆盖，不用管
                for b in spec.get("bindings") or []:
                    node = g.get(str(b.get("node")))
                    if not isinstance(node, dict):
                        continue
                    field = LOADER_FIELD.get(str(node.get("class_type") or ""))
                    if not field:
                        continue
                    val = (node.get("inputs") or {}).get(field)
                    if isinstance(val, str) and val.strip():
                        bad.append(
                            f"{card['key']}: {pname} → 节点 {b.get('node')} "
                            f"{node['class_type']}.{field} = {val!r}")
        self.assertEqual(bad, [],
                         "可选媒体槽位残留模板示例文件名（不提供时会导致提交失败）：\n"
                         + "\n".join(bad))

    def test_empty_loader_gets_dropped_by_render(self):
        """空加载器必须真的被 render 删掉（机制存在才敢把它当修法）。"""
        from tools.p20 import render

        g = {
            "1": {"class_type": "LoadAudio",
                  "inputs": {"audio": ""}},
            "2": {"class_type": "CreateVideo",
                  "inputs": {"images": ["9", 0], "audio": ["1", 0], "fps": 16}},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0]}},
        }
        n = render.prune_empty_loaders(g)
        self.assertEqual(n, 1, "空 LoadAudio 没被删")
        self.assertNotIn("1", g)
        # 指向已删节点的输入也要清掉，否则 ComfyUI 报引用不存在的节点
        self.assertNotIn("audio", g["2"]["inputs"])


class TestSoundImageToVideoInputs(unittest.TestCase):
    """WanSoundImageToVideo 系列的真实必填输入。

    2026-11-14 实测：这两个节点缺 width/height/batch_size 时，
    ComfyUI 回 `required_input_missing`，作业白建一次机。
    """

    REQUIRED = ("width", "height", "batch_size")

    def test_s2v_graph_declares_size_inputs(self):
        g = _graph_of({"graph": "workflows/pack/video_wan2_2_14B_s2v.api.json"})
        self.assertTrue(g, "s2v 图读不到")
        found = False
        for nid, node in g.items():
            if not isinstance(node, dict):
                continue
            if str(node.get("class_type") or "") not in (
                    "WanSoundImageToVideo", "WanSoundImageToVideoExtend"):
                continue
            found = True
            ins = node.get("inputs") or {}
            missing = [k for k in self.REQUIRED if k not in ins]
            self.assertEqual(missing, [],
                             f"节点 {nid} {node['class_type']} 缺必填输入 {missing}")
        self.assertTrue(found, "图里没找到 WanSoundImageToVideo 节点")


if __name__ == "__main__":
    unittest.main()
