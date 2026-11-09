# 跨云部署：一份描述，三种落地

## 核心结论

**停机实例不能跨云，也不该跨云复制。**

各家计费规则差异极大（见下表），把智云的「停机实例」方案照搬到别家会踩坑，
甚至更贵。跨云真正能带走的是**环境描述**，不是**机器状态**。

## 三层结构

```
第 1 层  comfyui-wan-p21.env.json     环境描述（可带走，跨云唯一真相源）
第 2 层  provision.sh + instance_setup.sh   落地脚本（每家云各跑一遍）
第 3 层  加速器                       机器级缓存（每家云各搞，不可移植）
```

| 层 | 能否跨云 | 说明 |
|---|---|---|
| 1 环境描述 | ✅ 完全可带走 | 纯 JSON，写死 torch/ComfyUI/节点 commit |
| 2 落地脚本 | ✅ 通用 | 只要有外网 + Python 3.10 + CUDA 12.8 |
| 3 加速器 | ❌ 数据中心锁定 | 它是「这台机器」的状态，换云就没有 |

## 各家停机计费对照

| 平台 | 停机保留 | 停机后存储费 | 能否用停机实例当加速器 |
|---|---|---|---|
| **智云 CompShare** | ✅ | 系统盘 ≤100GB **免费** | ✅ 待 24h 实验验证（¥0 可能成立） |
| **AutoDL 普通容器实例** | ✅ | 关机结束计费，基础 30GB 系统盘不额外收 | ✅ 可以 |
| **AutoDL 容器实例 Pro** | ⚠️ | 系统盘 **0.10 元/日**，无论开关机 | ❌ 不能（天天收钱） |
| **RunPod** | ⚠️ | 磁盘约 $0.20/GB/月，**停机照收** | ❌ 不能（150GB ≈ $30/月） |
| **Google Cloud** | ⚠️ suspend | 60 天后自动 TERMINATED | ❌ 不能（状态会丢） |

数据来源：
- 智云：本项目 `README.md:66-68` + `price()` API（`disk=¥0/h @100GB`）
- AutoDL：[充值与计费](https://www.autodl.com/docs/price/)
- RunPod / GCP：[How to Stop Cloud GPU Billing When Idle](https://www.aquanode.io/blog/stop-cloud-gpu-billing-when-idle)（引 RunPod、GCP 官方文档）

### 关键反直觉点

**RunPod 停机的磁盘费比智云镜像贵 13 倍。**

```
RunPod 150GB 停机   ≈ $0.20×150 = $30/月 ≈ ¥216/月
智云   70GB 镜像    = ¥0.236×70 = ¥16.5/月
```

所以在 RunPod 上「留一台停机实例省冷启动」是**反向优化**。
那里要么用 network volume 共享权重 + 每次冷装，要么直接接受冷启动。

## 各家的正确打法

### 智云 CompShare

```
加速器：停机实例（若实验证明 ¥0）或 自制镜像（¥16.5/月）
落地  ：instance_setup.sh（镜像已含环境，只做配置）
限制  ：实例无外网 → provision.sh 不可用
```

### AutoDL 普通容器实例

```
加速器：关机保留（免费）
落地  ：provision.sh（有外网，能装）→ instance_setup.sh
注意  ：别用 Pro 类型（系统盘天天收钱）
```

### RunPod

```
加速器：不搞停机实例（太贵）
落地  ：provision.sh（有外网，能装）
优化  ：network volume 存权重（跨 pod 共享，$0.07/GB/月）
        或直接用 docker/Dockerfile 构建的 OCI 镜像
```

### 阿里云 PAI / 自建 K8s

```
加速器：用 docker/Dockerfile 构建 OCI 镜像推到 ACR
落地  ：镜像直接起容器，provison.sh 作为 fallback
```

## 命令

```bash
# 看环境描述
cat deploy/env/comfyui-wan-p21.env.json

# 在任意有外网的机器上装环境（智云上会被拒）
bash deploy/env/provision.sh --dry-run
bash deploy/env/provision.sh
bash deploy/env/provision.sh --verify-only

# 智云上配置 + 启动
bash tools/p1/instance_setup.sh <uhost-id>
```

## manifest 与 install-nodes.sh 的关系

`deploy/env/comfyui-wan-p21.env.json` 的 `custom_nodes` 与
`docker/install-nodes.sh` 的 10 个节点 commit **必须一致** ——
`tools/p2/tests/test_cross_cloud.py::test_node_commits_match` 强制校验。

改节点版本时两边都要改，否则测试失败。
