# 模型准备（provision）+ 文件传输通道 实测记录

测试实例：`uhost-1wy3ufkstppr`（V100S，ComfyUI 0.3.40），**已删除**。耗时 6.8 分钟，实扣 **¥0.16**。

## 一、provision：无卡也能把模型“装好”

```
python3 csctl.py provision <ID> --ensure wan21_i2v,ltxv,hunyuan_i2v [--download]
```

它做四件事：
1. 探测 `/model`（40T UPFS 共享模型库）是否挂载；
2. 写 `ComfyUI/extra_model_paths.yaml`，把共享库目录接进 ComfyUI 的模型搜索路径；
3. 重启 ComfyUI（镜像无 systemd，走 `/start.d/comfyui.sh`）；
4. 查 `/object_info`，核对每类 loader 能否看到目标权重，输出缺失清单。

### 实测效果（同一台机器，加 yaml 前后）

| loader | 加之前 | 加之后 |
|---|---|---|
| UNETLoader（diffusion_models） | 2 个（仅 flux） | **156 个** |
| VAELoader | 3 个 | **22 个** |
| CLIPLoader（text_encoders） | 3 个 | **32 个** |

### 三种视频模型的可用性（本账户共享库）

| 模型 | unet | vae | text encoder | 结论 |
|---|---|---|---|---|
| **Wan2.1 i2v** | ✅ `Wan/Wan2.1_i2v_720p_scaled_fp8_e4m3_lightx2v` | ❌ 缺 `wan_2.1_vae` | ✅ `umt5_xxl_int8_convrot` | 只差一个 VAE（约 242MB） |
| **LTXV 2.3 22B** | ✅ `Ltx/ltx-2.3-22b-distilled-1.1_transformer_only-int4_convrot` | ✅ `ltx-2.3-22b-distilled_video_vae` | ✅ `t5xxl_fp16` | **齐了，可直接跑** |
| **HunyuanVideo i2v** | ✅ `hunyuan/hunyuan_video_image_to_video_720p_bf16` | ✅ `hunyuan_video_vae_bf16` | ✅ `llava_llama3_fp16` | **齐了，可直接跑** |

> 结论：上次视频测试“只能退回 SVD”的根因是 **extra_model_paths.yaml 没配**，不是文件缺失。
> Wan 只缺 VAE，用 `--download` 在无卡模式（0.15~0.6 元/时）下 242MB 几十秒即可补齐。

## 二、传输通道：实测对比（同一批数据）

数据：40 个 2MB 文件（80MB）、200 个 20B 文本、1 个 200MB 大文件。链路约 10–16 MB/s。

| 方案 | 40×2MB（80MB） | 说明 |
|---|---|---|
| `scp -r`（原方案） | 7.3s（11.0 MB/s） | 单连接，吞吐最好 |
| tar 单流 | 14.1s（5.7 MB/s） | 管道开销大 |
| tar 4 并发 | 9.4s（8.6 MB/s） | 没有明显收益 |
| 每文件并发 scp（4/8 路） | 16.1s / 11.7s | 每次握手 0.3–0.5s，文件小反而更慢 |
| **200 个文本 `scp -r`** | **18.6s** | 逐文件往返，最差 |
| **200 个文本 tar** | **4.2s** | **快 4.4×** |

### 最终策略（tools/transfer.py，自动选）

1. **单文件** → scp + **断点续传**（远端 `tail -c +N` 追加）；
2. **目录、文件数 < 50 且平均 > 256KB** → `scp -r`（吞吐最好）+ 自动拍平嵌套目录；
3. **大量小文件 / 小于 256KB 平均** → `tar -xf -` 单流（省往返）；
4. 统一 `-c aes128-gcm@openssh.com`；本机与远端都有 rsync 时优先 rsync（增量）；
5. 下载后按远端 `du -sb` 校验大小，可 `verify()` 取 sha256。

### 断点续传实测（200MB）

- 先拉 3 秒强杀 → 本地 **34.6MB**
- 再执行 `pull_file(resume=True)` → 16.1s 补齐 **200.0MB**
- 远端/本地 sha256 完全一致：`8fbdfec76ab841af77ff37437d7c5f0fa3bb39e329fdd697b34f408dc7d731b9`

## 三、新增命令

```bash
python3 csctl.py provision <ID> --ensure ltxv,hunyuan_i2v   # 挂共享库 + 校验可见性
python3 csctl.py provision <ID> --ensure wan21_i2v --download # 缺 VAE 时切无卡模式下载
python3 csctl.py pull <ID> /root/ComfyUI/output reports/pull  # 目录（自动策略）
python3 csctl.py pull <ID> "/root/*.mp4" reports/video        # 通配符
python3 csctl.py push <ID> local/file.npy /root/ComfyUI/input # 上传
python3 csctl.py rm <ID> --yes --archive reports/archive      # 归档已改用 Transfer（不再依赖实例上的 rsync）
```

`lab_runner.py` 也切换到了 Transfer：bench 上传与产物回拉都走这套（tar/scp 自适应 + 校验）。

## 四、遗留

1. `--download` 走无卡模式时会**关机→开机**，可能遇到“无卡”：需要重新抢卡（实测最长等过 5.5 分钟）。
2. Wan 的 VAE 只差一个文件，但它是 **fp8 模型**，仍需 4090/5090 才能算；A100/V100S 不支持 fp8。
3. 大文件上传（>1GB）目前仍是单连接 scp，未做分片并行；需要时再加。
