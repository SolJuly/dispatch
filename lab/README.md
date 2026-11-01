# 自动化基准实验室（cs-lab.service）

> ⚠️ **已退役（2026-11-01）**：P0 的卡型×模型选型任务已完成，选择结论已固化进 P1/P2。
> `cs-lab.service` 已 `disable`（重启不再自起，防无人值守烧钱）；历史数据保留在 `reports/lab/`。
> 需要复跑：`systemctl enable --now cs-lab`（预算 ¥8 / 12h 上限仍在 unit 里）。

等现货 → 建机 → 同机跑多模型多配置 → 收产物 → 销毁 → 出报告。等待期间零成本、零 token。

## 跑什么

| 维度 | 取值 |
|---|---|
| 卡型 | 4090（四个地域各排一台）、5090、3090、V100S、A100、4090_48G |
| 地域/可用区 | cn-wlcb-01、cn-sh2-02、cn-sh2-01、cn-bj2-03（同卡不同地域对比） |
| 模型 | SD1.5 `ghostmix_v12`、SDXL `juggernautXL`、FLUX.1-dev（仅高端卡）、Wan2.1 i2v（仅 4090/5090） |
| 配置 | 同模型不同步数/分辨率：SD1.5 @512 20步 vs 8步 vs @768 20步；SDXL @1024 20步 vs 8步；FLUX 20步 vs 10步 |
| 指标 | 建机→Running 秒、ComfyUI 就绪秒、冷启推理秒、预热推理秒、显存峰值、实扣金额 |
| 证据 | 每格产出的 PNG / WEBP 视频，落在 `reports/lab/<机器>/<负载>/` |

同一台机器上跑多个模型，保证环境一致；质量只在**同一模型内**横向比较（gallery 按模型分组）。

## 安全阀

- 总预算 `--budget 8`（元），到额自动停；
- 单机运行上限 35 分钟，建机即设平台定时关机兜底；
- 任何异常都在 `finally` 里 `terminate`；失败机重试 1 次；
- 急停：`touch lab/STOP`；彻底停：`systemctl stop cs-lab`
- 日志：`logs/lab.log`；进度：`reports/lab/REPORT.md`、`reports/lab/gallery.html`（都会自动同步到平板窗口）

## 常用命令

```bash
systemctl status cs-lab          # 是否在等货
tail -f logs/lab.log             # 实时日志
cat lab/state.json               # 每台机器的结果/花费
systemctl stop cs-lab && touch lab/STOP   # 立刻停
python3 lab_runner.py --dry-run  # 只看计划与现货，不建机
```
