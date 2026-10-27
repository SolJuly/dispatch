# 端到端视频测试记录（2026-10-26 19:59–20:08）

**目标**：建机 → 生成 10 秒视频 → 取回文件 → 删除实例，验证全链路。

## 结果

| 项 | 值 |
|---|---|
| 视频 | `video-10s.mp4` · 1024×576 · H.264 · 7.3 fps · 73 帧 · **10.000 s** · 1.9 MB |
| 实例 | `uhost-1wy2rurd6wkv` · RTX 4090 24G · 16C/64G · ComfyUI 0.3.40（CUDA 12.4） |
| 建机耗时 | 52 秒（Initializing → Running） |
| 总占用 | **7.3 分钟** |
| 实扣 | **¥0.26**（¥2.15/h；余额 52.94 → 52.68） |
| 实例状态 | 已删除（`terminate` 成功，列表 0 台） |

## 流水线（实例内实测耗时）

| 步骤 | 内容 | 耗时 |
|---|---|---|
| 1 | SDXL（juggernautXL）出首帧 1024×576 / 20 步 | 16.5 s |
| 2 | SVD-XT 图生视频 第 1 段 25 帧 / 20 步 | 54.1 s |
| 3 | 第 2 段（接上一段末帧） | 42.6 s |
| 4 | 第 3 段 | 42.5 s |
| 5 | PyAV 编码 H.264（73 帧 → 10.0s，7.3fps） | ~8 s |
| — | ComfyUI 就绪 → 全流程 | 155.9 s |

## 关键技术点（含踩坑）

1. **实例里没有 ffmpeg**，但有 `av`（PyAV 14.4）、`cv2`、`imageio`、`PIL` → 用 PyAV 直接编 H.264。
2. **Wan2.1 跑不了**：`/model` 里有 Wan fp8 权重，但实例的 `models/diffusion_models`、`vae` 是空的（只给 checkpoints 做了软链），且**没有 Wan 的 VAE**；`UNETLoader` 看不到 wan/umt5。
3. 因此改用 **SVD-XT 链式 i2v**：`svd_xt.safetensors` 在 checkpoints 里可见，节点 `ImageOnlyCheckpointLoader → SVD_img2vid_Conditioning → VideoLinearCFGGuidance → KSampler → VAEDecode`。
4. **PyAV 的 `rate` 必须是 Fraction**：传 float 会报 `AttributeError: 'float' object has no attribute 'numerator'`；改用 `Fraction(73, 10)` 后正好 10.000s。
5. 链式拼接：每段取 25 帧，后续段去掉重复首帧 → 25+24+24 = 73 帧。
6. `rm --archive` 依赖实例上的 rsync，该镜像**没有 rsync** → 归档失败（视频已单独 scp 取回，不受影响）。后续应改为「ssh tar 打包 + scp 回传」。

## 复现命令

```bash
python3 csctl.py up --gpu-type 4090 --image ComfyUI_v0.3.40 --name vid-test --hours 1
python3 - <<'PY'   # 上传并执行 lab/video_e2e.py（内含 3 段 SVD + PyAV 编码）
...
python3 csctl.py rm <ID> --yes
```
