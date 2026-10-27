# 路径生命周期速查表

> 生成时间：2026-10-27 00:56:31  
> 数据来源：最新报告 `reports/checkpoint/phase0-cp-adaptive.json`  
> 路径总数：**13**  
> 探针定义：`lab/probe_storage.sh` · 实测依据：`reports/audit-storage-2026-10-26.md`

## 速查表

| 路径名 | 介质类型 | stop-start | reboot | save-image新建 | terminate |
|---|---|---|---|---|---|
| `/root`（脚本、配置） | overlay | 保留 | 保留 | 保留（镜像内） | **丢失** |
| `/root/ComfyUI/output`（出图/出片产物） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/models`（本地权重） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/custom_nodes`（自定义节点） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/user/default/workflows`（工作流 JSON） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/miniconda3`（Python 环境） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/tmp`（临时文件） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/var/log`（系统日志） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/etc`（系统配置） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/model`（共享模型库，唯一跨实例存活） | 网络存储（UPFS 40T，只读） | 保留 | 保留 | 保留 | 保留 |
| `/cloud/cloud-ssd1`（只跟本实例，存镜像带不走） | 独立数据盘（20G） | 保留 | 保留 | **丢失** | **丢失** |
| `/root/ComfyUI/comfyui_8188.log`（ComfyUI 运行日志） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/output`（产物文件（与 comfy_output 同目录，探针标签不同）） | overlay | 保留 | 保留 | 保留 | **丢失** |

## 结论

**一句话：stop / reboot / 存镜像都不丢系统盘，只有 terminate 会全删；数据盘只跟着这台实例活；唯一跨实例永久存在的只有 `/model`。**

### 三类介质

- **overlay**：11 条路径
- **独立数据盘（20G）**：1 条路径
- **网络存储（UPFS 40T，只读）**：1 条路径

### 两条例外（最容易踩坑）

- **存镜像会丢**：`/cloud/cloud-ssd1` —— 新建实例后只剩 lost+found，数据盘内容必须 `csctl pull` 或转存到 `/model` 才能带走。
- **terminate 后仍存活**：`/model` —— 唯一跨实例的持久层，但整盘只读，只能读不能写。

### 落盘策略

```
要跨实例保留的数据  →  先 pull 到本地，或写进 /model（只读，实际不可写）
单次任务产物        →  /root/ComfyUI/output 够用，terminate 前记得 pull
20G 数据盘          →  只当临时暂存，不要当归档，存镜像带不走
```
