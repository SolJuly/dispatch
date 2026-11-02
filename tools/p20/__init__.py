# -*- coding: utf-8 -*-
"""P2.0 工作流基础设施：Workflow Registry + Model Registry。

包边界（不依赖 tools/p1 的执行器，只依赖标准库）：
  config_schema.py  配置文件 JSON 契约校验（纯函数，可单测）
  registry.py       SQLite 登记库：导入 / 查询 / 兼容性 / 解析
  render.py         参数映射：参数 + 模型文件 → ComfyUI API 图
  assets.py         模型资产可见性检查（远端 object_info）
  p20ctl.py         统一 CLI：init / import / validate / resolve / new / compat

单一事实来源：
  registry/workflows/*.json   工作流配置（含参数绑定）
  registry/models/*.json      模型配置（含权重文件、默认值、约束）
  registry/compat.json        兼容性结论（人工确认的 verified / incompatible）
  reports/p2/registry.db      由上面三类文件派生出的索引库（可删可重建）
"""
__all__ = ["config_schema", "registry", "render", "assets"]
