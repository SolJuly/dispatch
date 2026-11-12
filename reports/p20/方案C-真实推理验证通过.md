# v4 + 核心 0.43.0：真实推理验证通过

日期：2026-11-12
成本：约 **¥0.63**（09:33 起机 ~ 09:45 关机，12 分钟）
结论：**图片和视频两条链路都跑通，产物有效**

---

## 一句话

方案 C（v4 镜像只换核心到 0.43.0）**不只是接口兼容，真实推理也正常**。
图 768×768、视频 49 帧 1.96 秒，画面内容与提示词一致。

---

## 验证了什么

### ① 图片链路（SDXL 文生图）

用我们的 `sdxl_t2i` 工作流（7 个基础节点：`CheckpointLoaderSimple` /
`CLIPTextEncode` / `EmptyLatentImage` / `KSampler` / `VAEDecode` / `SaveImage`）。

```
提交: node_errors = False（提交前的节点校验全过）
结果: status = success, completed = True
产物: 772,013 bytes PNG
尺寸: 768×768
```

提示词 `a red apple on a wooden table, product photo`，产物是红苹果放木桌上，
光影、木纹、水珠都正常。

证据：`evidence/v4core43-image.png`

### ② 视频链路（LTXV 2B 文生视频）

用 `ltxv_2b_t2v`（12 个节点，含 `LTXVScheduler` / `SamplerCustom` /
`CreateVideo` / `SaveVideo`——正好覆盖视频链的关键节点）。

```
提交: node_errors = 无
结果: status = success, completed = True
产物: 275,032 bytes mp4
```

ffprobe 实测：

```
codec_name=h264
width=768  height=512
r_frame_rate=25/1
nb_frames=49          ← 与我设定的 49 完全一致
duration=1.960000
```

抽第 24 帧看画面：红苹果放木桌，与提示词一致。

证据：`evidence/v4core43-video.mp4`、`evidence/v4core43-video-frame.png`

---

## 关键结论

| 层 | 状态 |
|---|---|
| 核心启动 | ✅ 只有 embedded-docs 的无害 warning |
| 节点总数 | ✅ 1757 → 1818（净增 61，零丢失） |
| 接口契约 | ✅ 我们 161 个节点，158 个签名兼容，4 个纯增量 |
| 提交前校验 | ✅ `node_errors = False / 无` |
| **图片推理** | ✅ **success，768×768 有效 PNG** |
| **视频推理** | ✅ **success，h264 49帧 1.96s 有效 mp4** |
| 自定义节点 | ✅ 13 个包全部加载（36 个可枚举节点） |

**之前标为"未验证"的三项，现在两项已验（图片+视频推理），自定义节点"能否计算"仍未单独验**——
不过 SDXL/LTXV 用到的都是核心节点，自定义节点只是加载了没参与计算。

---

## 一个顺带证实的事

提交视频时我先漏填了 `{{SEED}}` 占位符，服务端返回：

```json
{"node_errors": {"ks": {"errors": [{
  "type": "invalid_input_type",
  "details": "noise_seed, {{SEED}}, invalid literal for int() with base 10: '{{SEED}}'"}]}}}
```

**报错精确到节点 id + 参数名 + 实际值。** 说明 0.43.0 的输入校验链路完整，
不是"能启动但校验失效"的半残状态。

---

## 现在的完整证据链

```
reports/p20/evidence/
  oi-0430.json              纯 0.43.0 底座，1060 节点
  oi-0450.json              0.45.0，1094 节点
  oi-v4-core43.json         v4+0.43核心，1818 节点   ← 本方案
  v4core43-image.png        SDXL 推理产物 768×768
  v4core43-video.mp4        LTXV 推理产物 49帧 h264
  v4core43-video-frame.png  视频第 24 帧
```

对照基线：`reports/p2/p21/evidence/objectinfo-v4.json`（原 v4 的 1757 节点）

---

## 落地步骤（已验证可行）

```bash
# 0. 从 v4 镜像起机（torch 2.8.0+cu128 保持不动）
# 1. 换核心目录（漏一个起不来，见下）
# 2. 装 4 个配套包（--no-deps）
# 3. 重启 ComfyUI
# 4. save 成 v5 镜像
```

**必须覆盖的目录**（实测漏 `app/` 报 `cannot import name 'governance'`）：

```
app/  api_server/  alembic_db/  blueprints/  comfy_api_nodes/
comfy/  comfy_api/  comfy_config/  comfy_execution/  comfy_extras/
comfy_aimdo/  comfy_kitchen/  middleware/  utils/
```

**必须一起升的 4 个包**：

```bash
/root/miniconda3/bin/pip install --no-deps \
  'comfyui-frontend-package==1.57.4' \
  'comfyui-workflow-templates==0.11.97' \
  'comfy-aimdo==0.6.0' \
  'comfy-kitchen==0.2.40'
```

**不动**：`custom_nodes/`、`models/`、`user/`、torch

### 落地脚本必须处理的环境坑

```
① GitHub 不通：https_proxy 指向 mihomo.kube-system.svc.cluster.local:7890，
   该 host 解析不了；去掉代理也 gnutls_handshake failed
   → 本机 git archive | gzip 打 13MB，scp 推过去

② pip 撞 PEP 668 → 用 /root/miniconda3/bin/pip 并 env -u http_proxy -u https_proxy

③ 实例会意外关机（定时关机设 10:09，实际 09:21 就关过；本次 09:33 起、09:45 手动关，
   期间正常）→ 脚本要能断点续跑
```

---

## 还没做的

1. **自定义节点能否计算**。13 个包都加载了，但这次跑的两个工作流只用核心节点。
   要验证得跑一个真正依赖 KJNodes / LayerStyle 的工作流（如 scail2 动作迁移）。
2. **qwen_image_2_1 能否真的生成**。节点能列出（`QwenImage21Cache` /
   `TextEncodeQwenImage21`），但没跑过。
3. **全量 54 个工作流回归**。目前只验了 2 个。

建议下一步：跑 1 个动作迁移（按依赖自定义节点推算）+ 1 个 qwen_image_2_1，
约 ¥2~3，就能覆盖全部未知项。

---

## 成本

| 项 | 金额 |
|---|---|
| 本次验证 | **¥0.63** |
| 前一轮接口验证 | ¥0.9 |
| 累计 | ¥1.53 |
| 余额 | ¥16.33 → **¥15.83** |

实例 `uhost-1xlp0lbuo6m6` 已关机，磁盘保留（环境已装好，下次开机直接可用）。
