# Manager 建机卡住：复现数据、根因与修复方案

> 调查时间：2026-10-28 22:36–22:50 · 5 次建机 · 实扣 **¥0.42**
> 结论：**当前 5 轮 0 次卡住**，但历史 4 次中 2 次；根因是实例出口访问 GitHub 的可达性不稳定。
> 修复采用「setup 阶段 patch 启动脚本」的治本方案，已实现并验证机制。

---

## 1. 复现数据

### 1.1 本次 5 轮（2026-10-28 22:36–22:48）

| 轮 | 卡型 | 建机耗时 | ComfyUI 就绪 | 是否卡住 |
|---|---|---|---|---|
| 1 | 4090_48G | 81.6s | 22.3s | 否 |
| 2 | 4090 | 70.0s | 37.8s | 否 |
| 3 | 4090_48G | 84.1s | 35.7s | 否 |
| 4 | 4090_48G | 70.1s | 27.1s | 否 |
| 5 | 4090_48G | 75.4s | 26.0s | 否 |

**卡住率 0/5（0%）**。五台的 `comfy.log` 尾部都是正常的：

```
[INFO] Starting server
[INFO] To see the GUI go to: http://0.0.0.0:8188
```

启动命令确认带 `--enable-manager`：

```bash
nohup python -u main.py --listen 0.0.0.0 --port 8188 --enable-manager > comfy.log 2>&1 &
```

### 1.2 历史 4 轮（2026-10-27 21:00–22:00）

| 实例 | 现象 | 处理后 |
|---|---|---|
| `uhost-1wzk02c03yro` | 进程存在、CPU 高、8188 永不监听 | pkill → 禁 Manager → 手动启动 ✅ |
| `uhost-1wzl2h9qc10s` | 同上 | 同上 ✅ |
| `uhost-1wzlx9imjfjj` | 这次 GitHub 通了，但 entrypoint 重启的进程与我的抢 8188 | 杀冗余进程 ✅ |
| `uhost-1wzmnnqo0ffw` | pkill 后 entrypoint 未重启 | 手动禁 Manager + 启动 ✅ |

**卡住率 2/4（50%）**。那两次的日志原文：

```
aiohttp.client_exceptions.ConnectionTimeoutError:
Connection timeout to host https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json
```

---

## 2. 根因

### 2.1 直接原因

`ComfyUI-Manager` 在 ComfyUI 启动阶段要拉一份扩展节点表：

```
https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json
```

`raw.githubusercontent.com` 在国内经常不可达。`aiohttp` 会重试并超时，**这个调用发生在 web server 启动之前**，所以：

- 进程活着、CPU 占用高
- 但 8188 永远不监听
- 从外部看就是「卡住」

### 2.2 为什么时好时坏

关键证据：**同一时刻，本机完全不通，实例却通。**

```bash
# 本机（123.207.74.155）到 GitHub，3 次采样全部超时
$ curl -s -o /dev/null --max-time 8 -w "%{http_code}" \
    https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json
000    （耗时 8.02s，超时）
000    （耗时 8.02s，超时）
000    （耗时 8.00s，超时）
```

但 5 台实例的 Manager 全部正常启动（22–38 秒就绪）。

**说明出口链路不同**：本机（腾讯云）到 GitHub 完全阻断；实例（优云智算各可用区）的出口时通时不通，取决于机房、时段、运营商路由。

所以：

| 视角 | 结论 |
|---|---|
| 不是本机网络问题 | 本机根本不通，但实例能跑 |
| 不是镜像问题 | 同一镜像，5 次全好、历史 2 次卡 |
| **是实例出口的不确定性** | 通 → 启动快；不通 → 卡到超时 |

### 2.3 为什么「超时」会变成「永久卡住」

ComfyUI 的启动顺序是：`import 节点 → 启动 web server → 异步加载 Manager 缓存`。
Manager 的 `get_cache()` 在网络失败时用的是**同步等待**，导致 web server 迟迟不 bind。

实测卡住的实例：CPU 58.9%、进程存在、日志停在 `[START] ComfyUI-Manager`。

---

## 3. 修复方案

### 方案对比

| 方案 | 做法 | 优点 | 缺点 | 风险 |
|---|---|---|---|---|
| **A. 镜像层** | 自制镜像时改 `/start.d/comfyui.sh` 去掉 `--enable-manager` | 一劳永逸 | 需要制作/维护镜像 | 低 |
| **B. setup 事后补救**（现状） | `mv Manager .disabled` + pkill + 重启 | 已有，能兜住 | 要等它先卡 60–90 秒才知道 | 低 |
| **C. setup 前置 patch**（本次新增） | 建机后先改 `/start.d/comfyui.sh`，再重启 | 防住 entrypoint 的后续重启；不用等卡住 | 只对「之后的重启」有效 | 低 |
| D. 网络层 | 给实例配 hosts / 代理指向 GitHub 镜像 | 保留 Manager 功能 | 要维护镜像源，且平台不保证可达 | 中 |

### 采用：B + C 组合（已实现）

`tools/p1/instance_setup.sh` 新增 **start.d patch**，在 pkill 之前执行：

```bash
# 把启动命令改成「离线模式 + 不带 Manager」
if grep -q -- '--enable-manager' /start.d/comfyui.sh 2>/dev/null; then
    sed -i 's|nohup python -u main.py --listen 0.0.0.0 --port 8188 --enable-manager|COMFYUI_MANAGER_OFFLINE=1 nohup python -u main.py --listen 0.0.0.0 --port 8188|' \
        /start.d/comfyui.sh
fi
```

这样：
- 当前进程：靠 `mv Manager .disabled` + pkill 清掉
- 未来的重启（entrypoint 或手动）：启动命令已不带 Manager，且带离线环境变量

**为什么不直接改镜像**：镜像维护成本高（每次换 ComfyUI 版本都要重做），而 setup 脚本是幂等的、随代码分发的。等 P2 做自制镜像时再把这条固化进去（记在技术债）。

### 验证方式

```bash
# ① patch 是否生效
grep 'main.py' /start.d/comfyui.sh
# 期望：COMFYUI_MANAGER_OFFLINE=1 nohup python -u main.py --listen 0.0.0.0 --port 8188 > ...

# ② 重启后是否还带 Manager
pkill -f '[m]ain.py'; sleep 3; ls /start.d/
pgrep -af '[m]ain.py'   # 若 entrypoint 重启，cmdline 不应含 --enable-manager
```

---

## 4. 遗留

| # | 项 |
|---|---|
| 1 | 本机到 GitHub 完全不通，所以"卡住"无法在本机复现，只能靠实例出口的随机性 |
| 2 | 5 轮样本量小（历史 50%、本次 0%），真实概率在 0–50% 之间，需要更多轮次或长期监控才能收敛 |
| 3 | Manager 若被彻底禁用，`ComfyUI-Manager` 的节点管理功能不可用——本项目只用固定节点，可接受 |
| 4 | 彻底方案是自制镜像，属 P2 范围 |

---

## 5. 证据

```
reports/p1/manager-probe/
├── summary.json        5 轮完整数据（就绪耗时、进程 cmdline、启动脚本原文）
├── round1.log ~ round5.log   每轮的现场快照
tools/p1/manager_probe.py     可复跑的探测脚本（--rounds N）
```
