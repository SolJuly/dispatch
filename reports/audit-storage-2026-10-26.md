# 存储与任务生命周期审计报告

> 日期：2026-10-26 23:25 – 23:50（Asia/Shanghai）
> 环境：优云智算 V100S 实机 · 两台实例（`audit-store` / `from-image`）· 全部已销毁
> 本轮花费：**¥0.39**（余额 51.80 → 51.41）
> HTML 版：`reports/audit-storage-2026-10-26.html`

---

## 结论速览

| 项目 | 结果 |
|---|---|
| 任务完成 | **4/4** |
| SSH 断开后任务存活 | **3/4 组存活**（含 tty 的那组被杀） |
| 存储路径 stop/start 保留 | **11/13**（`/model` 只读写不进、数据盘见下） |
| 检查点通过（镜像新建实例） | **29/32**（3 项为镜像已知缺失，不判失败） |
| 风险自动监控规则 | **15 条 + 1 条深度探测** |
| 本轮花费 | **¥0.39** |
| 当前实例数 | **0 台** |

### 三个最值得记住的结论

1. **决定任务生死的是「有没有 tty」，不是有没有 nohup。** sshd 只向已分配 tty 的会话发 SIGHUP。
2. **`save-image` 会让实例里新起的进程失去 GPU**（`nvidia-smi` 报 Unknown Error、`torch.cuda` False），已持有 CUDA context 的 ComfyUI 不受影响；**stop→start 完全恢复**。
3. **SSH 会话里 `torch.cuda.is_available()` 常为 False**，不要用 SSH 里直接跑 torch 做推理——一律走 ComfyUI `:8188` HTTP 接口。

---

## ① SSH 断开后，远程任务还会继续吗

### 方法

远端放一个「1 秒写一行的计数循环」脚本（`/root/t1_loop.sh`），本地用 ssh 起任务，**4 秒后直接 kill 掉本地 ssh 进程**（等价于平板关页面、网络断、笔记本合盖），过 25 秒用全新连接数行数。**行数还在涨 = 任务还活着。**

### 结果

| 组 | 启动方式 | 断开时行数 | 25 秒后 | 进程 PPID | 结论 |
|---|---|---|---|---|---|
| A | 前台裸跑（非交互 ssh，**不分配 tty**） | 73 | 94 | 1 | ✅ 继续跑 |
| B | `nohup bash … &` | 42 | 62 | 1 | ✅ 继续跑 |
| C | `setsid nohup bash … &`（`csctl ssh --detach` 的写法） | 11 | 31 | 1 | ✅ 继续跑 |
| D | 前台裸跑 + `ssh -tt` **强制分配 tty** | 5 | 5 | — | ❌ 被杀 |

### 机理

断开 SSH 时，sshd 只会向 **已分配 tty 的会话**发 SIGHUP：

- 非交互 `ssh host "命令"` 根本不建 tty → 前台裸跑也能活（A 组）
- `ssh -t` / 交互登录里起的前台进程 → 被 SIGHUP 打死（D 组）
- `nohup`、`setsid` 的作用是「即使有 tty 也不怕被杀」→ 多层冗余是有意义的（B、C 组）

### 最硬的证据：ComfyUI 本身不挂在 SSH 上

```
bash /start.d/comfyui.sh     PID 35,  PPID 1
└── python main.py --listen 0.0.0.0 --port 8188   PID 41, PPID 35
```

这是容器 entrypoint 启的，跟 SSH 会话毫无关系。同理 Jupyter(:8888)、FileBrowser(:8889) 也都是 PID 1 的孩子。**所以 SSH 断了、平板关了，出图/出片照跑。**

### 实操建议

- 长任务一律走 `csctl ssh <id> --detach --log /root/job.log`
- 或干脆直接打 ComfyUI 的 HTTP 接口（所有生产脚本都是这么干的）
- 绝不要 `ssh -t` 登进去手动跑前台长任务，然后关页面

---

## ② 存储生命周期：每种操作之后，哪些路径还在

### 环境事实

实例是**容器化**的：

| 挂载点 | 介质 | 容量 |
|---|---|---|
| `/` | overlay（vda2） | 79G |
| `/cloud/cloud-ssd1` | 独立数据盘（vdb） | 20G |
| `/model` | UPFS 网络存储 | 40T（只读） |

### 完整矩阵（13 个探针标记逐一比对内容）

| 路径 | 介质 | stop → start | reboot | save-image → 新建 | terminate |
|---|---|---|---|---|---|
| `/root`（脚本、配置） | overlay | 保留 | 保留 | **保留（镜像内）** | **删除** |
| `/root/ComfyUI/output` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/root/ComfyUI/models`（本地权重） | overlay | 保留 | 保留 | 保留 | **删除** |
| `/root/ComfyUI/custom_nodes` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/root/ComfyUI/user/…/workflows` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/root/miniconda3`（Python 环境） | overlay | 保留 | 保留 | 保留 | **删除** |
| `/tmp` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/var/log` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/etc` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/root/ComfyUI/comfyui_8188.log` | overlay | 保留 | 保留 | 保留 | **删除** |
| `/cloud/cloud-ssd1`（20G 数据盘） | 独立盘 | 保留 | 保留 | **丢失**（新盘只剩 lost+found） | **删除** |
| `/model`（UPFS 40T 共享库） | 网络存储 | 保留 | 保留 | 保留 | **保留** |
| `/model` 可写性 | — | 整盘**只读挂载**（777 的子目录也写不进） | | | |

### 一句话记住

> stop / reboot / 存镜像都**不丢系统盘**，只有 `terminate` 会全删；
> **数据盘只跟着「这台实例」活**，存了镜像也带不走；
> **唯一跨实例永久存在的只有 `/model` 共享库**，但它整盘只读。

### 自制镜像

| 项目 | 值 |
|---|---|
| 镜像 ID | `compshareImage-1wyak4svn41r` |
| 名称 | `audit-store-20261026` |
| 制作耗时 | **2 分 23 秒**（23:38:26 → 23:40:49） |
| 体积 | 30,720 MB（约 30GB，只打包实际使用量，不是 100G 系统盘全量） |
| 用镜像新建的实例 | `uhost-1wyasl8g5t50`，检查点 **29/32 通过** |
| 镜像带走了什么 | 系统盘内容全带走（含 23:31 传上去的探针脚本，镜像做于 23:38） |
| 镜像带不走什么 | 数据盘、`/model` 挂载内容（靠平台重新挂） |

### ⚠️ 坑一：`save-image` 会让实例里新进程失去 GPU

时间线：

```
23:38:26  提交 CreateCompShareCustomImage
23:40:49  镜像 Available（30GB，2 分 23 秒）
23:40:53  checkpoint 首次采集：nvidia-smi 正常，报 Tesla V100S-PCIE-32GB
23:42:02  nvidia-smi → Failed to initialize NVML: Unknown Error
          torch.cuda.is_available() → False
          但 ComfyUI /system_stats 仍报 cuda:0 Tesla V100S-PCIE-32GB, vram_free 31.4GB
23:43     stop → start
23:46     完全恢复：nvidia-smi 正常、torch.cuda True
```

**判断**：GPU 没掉，是「新进程无法初始化 NVML」，**已持有 CUDA context 的老进程不受影响**。

**恢复方式：stop → start。** reboot 不够干净，直接 stop/start 最可靠。

**运维含义**：存完镜像如果要继续干活，先 stop/start 一次，否则后续新起的进程全部看不到 GPU。

### ⚠️ 坑二：SSH 会话里 torch 看不到 GPU

```
$ nvidia-smi            → Failed to initialize NVML: Unknown Error
$ python -c "import torch; torch.cuda.init()"
RuntimeError: No CUDA GPUs are available
```

但：
- `/dev/nvidia0`、`/dev/nvidiactl`、`/dev/nvidia-uvm` 全在
- `ctypes.CDLL('libcuda.so.1')` 和 `libnvidia-ml.so.1` 都能成功加载
- `LD_LIBRARY_PATH` 加 conda/lib 或系统 nvidia 目录都无效

**含义：不要用 SSH 里直接跑 torch 脚本来做推理，会静默掉到 CPU。** 一律打 ComfyUI 的 `:8188` HTTP 接口（`/prompt`、`/system_stats`），那是服务端视角，永远准。

---

## ③ 检查点：把「路径清楚」变成「能判定」

模型 / 输入 / 输出 / 日志四条路径已经查清，但「知道在哪」不等于「能判断好坏」。所以落成 **8 组检查点 CP0–CP7**，每条都有判据、期望值、实测值、结论。

**工具**：`tools/checkpoint.py` + `lab/checkpoint.sh`（走 stdin 送脚本，不依赖实例上预置文件）

```bash
python3 tools/checkpoint.py --instance <id>          # 基础检查
python3 tools/checkpoint.py --instance <id> --video  # 额外要求 SVD 模型
python3 tools/checkpoint.py --instance <id> --json   # 机器可读
```

### 检查点定义

| 检查点 | 查什么 | 关键判据 |
|---|---|---|
| **CP0** 开机就绪 | 能不能干活 | nvidia-smi 出卡名 · 驱动版本 · CUDA 版本 · **NVML 可初始化** · **ComfyUI 服务端看得到 cuda:0** · 8188 端口 up · `/model` 已挂载 |
| **CP1** 环境 | 跑得起来 | torch 2.4.0+cu124 · ComfyUI 0.3.40 · PyAV 14.4.0 · PIL 10.2.0 · ffmpeg/rsync（镜像已知不带，记 ⚠️ 不判失败） |
| **CP2** 模型检查点 | 模型在不在 | 共享库 57 类可见 · `juggernautXL` 命中 · `checkpoints/svd_xt.safetensors` 命中（实测**不在** `diffusion_models`） |
| **CP3** 输入 | 输入区就位 | `input/` 存在 · `user/default/workflows/` 存在 |
| **CP4** 运行 | 服务活着且**脱离 SSH** | ComfyUI 进程在 · **启动链根 PPID=1** · 日志近 200 行无 Traceback/OOM · 有 `Prompt executed` |
| **CP5** 输出 | 产物存在 | `output/` 存在且非空 |
| **CP6** 归档/资源 | 盘不满、数据盘在 | 系统盘 <85% · 日志文件在 · 数据盘已挂 `/cloud/cloud-ssd1` |
| **CP7** 收尾 | 可追溯 | 采集时间戳 · 系统运行秒数 |

### CP4 的 PPID 检查是「防傻瓜」设计

如果你在 SSH 里手动 `nohup python main.py` 起了服务，PPID 就挂在 sshd 下面；这台实例一旦断连重启，服务就没了。检查点直接把这个错误暴露出来，而不是等你半小时后发现页面打不开。

> 注：旧实例上第一次跑时误判成 ❌，因为查的是 `main.py` 自己的 PPID=35；已修正为查**启动链根** `bash /start.d/comfyui.sh` 的 PPID。

### 实测结果

| 实例 | 场景 | 硬性通过 | 说明 |
|---|---|---|---|
| `audit-store` | 原实例（跑过 stop/start/reboot/save-image） | 首跑 26/32 | 当时 ComfyUI 已启动但 NVML 已失效（save-image 副作用），CP0 两项挂；修正判据后正常 |
| `from-image` | 用自制镜像新建 | **29/32 全通过** | 3 个 ⚠️ 是镜像已知不带 ffmpeg/rsync，不判失败 |

---

## ④ 风险清单自动化监控

把之前散落在架构图 F7 和踩坑记录里的风险，全部落成 **15 条可执行规则 + 1 条深度探测**。

**工具**：`tools/riskmon.py`
**调度**：cron 每 10 分钟（`*/10 * * * * python3 tools/riskmon.py`）
**输出**：`reports/risk/latest.json` + `reports/risk/alerts.md`
**页面**：`<token>/risk`（首页有「🛡️ 风险监控」按钮，60 秒自动重载）

### 规则清单

| ID | 风险 | 判据 | 自动处置建议 |
|---|---|---|---|
| R01 | 余额不足 | <¥10 高危，<¥20 关注 | 立即充值 |
| R02 | 运行中实例无定时关机 | Running 且 stop_at 为空 | `csctl stop-at` |
| R03 | 孤儿 lab 实例 | 名字 `lab-*` 但 cs-lab 服务不在跑 | `csctl rm --yes` |
| R04 | 当日花费超预算 | >¥20 高危，>¥12 关注 | 收敛实验规模 |
| R05 | 单价越过上限 | 在跑实例 >¥3/h | 换低价卡型 |
| R06 | 主力卡无货 | 4090 现货 = 0 | `--fallback 3090,V100S` |
| R07 | Running 但 SSH 不通 | 逐个实例探 `echo ok` | 等 20–40 秒，或 reboot |
| R08 | 常驻服务掉线 | cs-files / cs-lab 非 active | `systemctl restart` |
| R09 | 定时任务缺失 | crontab 里缺 watch / arch / cost | `scripts/install_cron.sh` |
| R10 | 凭证权限过宽 | `credentials.env` 不是 600 | `chmod 600` |
| R11 | 控制机磁盘将满 | >80% 关注，>90% 高危 | 清 logs/ 与旧 reports |
| R12 | 基准组失败残留 | lab 组 status=failed，**已过滤 226604/226628/无货等可重试原因** | 看 reports/lab/ |
| R13 | 视频黑帧 | 最近 3 个 mp4 首帧亮度 <2（ffmpeg signalstats） | 换模型/镜像组合 |
| R14 | 模型库不可见 | UPFS 是否挂载 | `csctl provision` |
| R15 | 销毁前漏归档 | terminate 但账本无 pull 记录 | 先 `csctl pull` 再 rm |
| R16 | 实例内部健康 `--deep` | 8188 监听 · 共享库可见 · 输出计数 · GPU 利用率 | 重启 ComfyUI |

### 当前状态

**高危 0 / 关注 1 / 提示 1 / 正常 13**

- 唯一的「关注」是 R12：lab 基准里还有 8 组历史失败（都是 SSH 连接不同步、跨区无货这类可重试原因，已排除在外的 33 组正常）
- R15 降为「提示」是因为 `pull` 从本轮起才写账本，历史 6 台无法追溯

### 去噪设计

`KNOWN_RETRYABLE` 白名单：`226604`（GPU 无货）、`226628`（镜像同步中）、`out of resources`、`镜像正在同步`、`8333`（CPU 内存比不合规）、`cpu memory ratio`、`Connection to`（SSH 未就绪）。

命中这些的失败**不计入报警**，否则基准实验室的历史失败会让风险页永久飘红。

---

## ⑤ 这轮踩到并已修掉的东西

1. **`csctl images()` 看不到「制作中」的镜像** —— 只保留 Status=Available，镜像刚提交就查不到，会误判「创建失败」。改为可直接查 `DescribeCompShareCustomImages` 原始状态。
2. **`csctl pull` 不记账** —— 导致「销毁前有没有归档」无从判断。已补 `ledger("pull", …)`。
3. **SSH 里 `pkill -f t1_loop.sh` 把自己杀了** —— 远端命令行本身含这个字符串，pkill 匹配到自己。改成 `pkill -f '^bash /root/t1_loop.sh'`。
4. **断开测试第一版双重转义失败** —— `ssh host "bash -c \"…\$i…\""` 里的 `$i` 被本地再展开一次，远端语法错误。改成「脚本先 push 上去，ssh 只传参数」。
5. **检查点把 `580.159.04` 判成非法数字** —— 用 `float()` 判版本号。改成正则 `^\d+(\.\d+)+`。
6. **检查点把「容器刚起还在加载 ComfyUI」判成失败** —— 增加 uptime <300s 时降级为 ⚠️ 的宽容逻辑。

---

## ⑥ 成本

| 实例 | 用途 | 规格 | 时长 | 花费 |
|---|---|---|---|---|
| `uhost-1wya3h0lcmsv` | audit-store：SSH 断开测试 + stop/reboot/存镜像 + 数据盘 | V100S 10C/64G · 100G+20G | 约 19 分钟（关机时段不计费） | ¥0.34 |
| `uhost-1wyasl8g5t50` | from-image：用自制镜像新建，验证镜像内容 | V100S · 100G+20G | 约 3 分钟 | ¥0.05 |
| **合计** | 余额 51.80 → **51.41** | | | **¥0.39** |

镜像 `compshareImage-1wyak4svn41r`（30GB）保留着，后续可直接用它秒起环境；不需要了可以删掉省镜像存储费。

---

## 附：复现命令

```bash
# ① SSH 断开后任务是否继续
python3 scripts/audit_ssh_detach.py <实例名>      # 输出 reports/audit-ssh-detach.json

# ② 存储生命周期
python3 csctl.py push <id> lab/probe_storage.sh /root/probe_storage.sh
python3 csctl.py ssh <id> --exec "bash /root/probe_storage.sh init AUD-xxx"
python3 csctl.py stop <id> && python3 csctl.py start <id>
python3 csctl.py ssh <id> --exec "bash /root/probe_storage.sh check AUD-xxx"
python3 csctl.py reboot <id>                       # 再 check
python3 csctl.py save-image <id> --name my-image   # 再 check
python3 csctl.py up --image <镜像ID> --gpu-type V100S --name from-image
python3 csctl.py rm <id> --yes                     # 数据盘一并删除

# ③ 检查点
python3 tools/checkpoint.py --instance <id> --video

# ④ 风险监控
python3 tools/riskmon.py            # 跑一次
python3 tools/riskmon.py --deep     # 含实例内部探测
python3 tools/riskmon.py --json     # 机器可读
```

---

## 相关文件

| 文件 | 内容 |
|---|---|
| `reports/audit-storage-2026-10-26.md` | 本报告 |
| `reports/audit-storage-2026-10-26.html` | HTML 版（平板友好） |
| `reports/audit-ssh-detach.json` | SSH 断开测试原始数据 |
| `reports/checkpoint/audit-store.json` | 原实例检查点结果 |
| `reports/checkpoint/from-image.json` | 镜像新建实例检查点结果 |
| `reports/risk/latest.json` | 最近一次风险扫描 |
| `reports/risk/alerts.md` | 告警明细 |
| `scripts/audit_ssh_detach.py` | 断开测试脚本 |
| `lab/probe_storage.sh` | 存储探针 |
| `lab/checkpoint.sh` | 检查点采集（远端） |
| `tools/checkpoint.py` | 检查点判定（本地） |
| `tools/riskmon.py` | 风险规则引擎 |
