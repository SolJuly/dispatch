# P2.1 运行时镜像（Dockerfile → ACR）

把 `p2-comfy-media-v3` 的运行时部分做成可重建、可移植的 OCI 镜像。

## 里面有什么

| 组件 | 版本 / commit |
|---|---|
| 基础 | `python:3.10-slim-bookworm`（可 `--build-arg BASE=...` 换掉） |
| PyTorch | `2.10.0+cu130` / torchaudio 同版本 |
| ComfyUI | `3c80da7f87ee359b2d06f107cb3c0797079dfbbb`（2026-09-18） |
| 自定义节点 | 10 个 active 节点，commit 见 `install-nodes.sh` |
| Python 依赖 | `constraints.txt`（198 个锁定版本，来自 v3 实机 `pip freeze`） |
| xformers | **不装**（0.0.32 与 torch 2.10 ABI 不匹配，会拖垮 SeedVR2/SUPIR） |

**不含模型权重**。实机上权重来自只读 `/model` 软链；容器里请挂载 NAS/OSS/云存储到 `/model`，或在启动前下载到 `/root/ComfyUI/models`。

## 构建 / 推送

```bash
# 构建机需要有 docker、能访问 github.com 和 download.pytorch.org
ACR=registry.cn-hangzhou.aliyuncs.com NS=your-namespace TAG=v3 ./docker/build.sh

# 推送（先 docker login）
ACR=registry.cn-hangzhou.aliyuncs.com NS=your-namespace TAG=v3 PUSH=1 ./docker/build.sh
```

预计体积 **15–25GB**（对比 CompShare 全盘镜像 210GB），构建 20–40 分钟。

## 怎么跑

```bash
docker run --gpus all -p 8188:8188 \
  -v /path/to/weights:/model:ro \
  registry.cn-hangzhou.aliyuncs.com/your-namespace/comfyui-p21:v3
```

权重目录按 ComfyUI 习惯放：`/model/diffusion_models`、`/model/vae`、`/model/text_encoders` 等，再软链进 `/root/ComfyUI/models`（和实机的做法一致）。

## 重要限制：CompShare 不能直接拉 ACR

查过 CompShare 的 API：`CreateCompShareInstance` **只接受 `CompShareImageId`**，SDK 里没有 registry / import / docker 相关字段（`Registry`/`Docker`/`Import` 关键字在 schema 里 0 命中），也没有“从镜像仓库导入”的接口。平台镜像只能是他们自己的格式（实例 save 出来的）。

所以这个 ACR 镜像的定位是：

- ✅ **可移植产物**：换到支持 OCI 的平台（阿里云 PAI / ACK / ECI、RunPod、自建 K8s + GPU Operator）可以直接跑；
- ✅ **可重建的环境定义**：不再依赖“手工改一台实例再 save”，环境变更走 Dockerfile diff；
- ❌ **不能在 CompShare 上直接跑**。CompShare 仍然要用自制镜像；可以用这套 Dockerfile 作为“安装脚本”在新实例上复现，再 save 成平台镜像。

如果目标是“彻底摆脱 210GB 全盘镜像”，需要把运行时迁到支持 OCI 的平台，或者接受 CompShare 的镜像模式但把权重全部外置。

## 已知未验证项

- 本机没有 docker、Docker Hub 也不通，**这份 Dockerfile 没有实际构建过**；第一次构建大概率要按日志调几处依赖（LayerStyle / SUPIR / face_parsing 的 requirements 最容易冲突）。
- `constraints.txt` 里有 `sageattention` 等本地编译包被剔除了，相关节点若需要得在 Dockerfile 里补。
- 权重不在镜像里，`/object_info` 不会列出模型，需要先挂载/下载再验。
