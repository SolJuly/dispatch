# 优云智算（CompShare）GPU 实例管理工具

用脚本管理 `api.compshare.cn` 上的 GPU 实例：**把「人盯着控制台刷新」变成「脚本自动等库存、自动等就绪、自动关机」**，同时把每小时成本压到最低。

```
智云视频工作流/
├── csctl.py                     # 唯一入口，全部子命令
├── compshare/
│   ├── credentials.env          # AccessKey（已写入，600 权限）
│   └── cs.py                    # API 薄封装：库存/价格/监控/SSH探测/等待/定时关机
├── scripts/
│   └── install_cron.sh          # 一键装「空闲自动关机」守护
└── .compshare/                  # 运行态（state.json / watch_state.json / stop_at.json / watch.log）
```

## 0. 自检

```bash
python3 csctl.py init      # 连通性 + 现货 + 余额
python3 csctl.py ls        # 实例列表（状态/单价/已跑时长/已花金额/定时关机）
```

当前账户：余额 ¥54.09，可用区 `cn-wlcb-01`（华北二A），Project `org-2aks1b`。

## 1. 常用命令

| 目的 | 命令 |
| --- | --- |
| 看所有卡型价格（挑最省） | `python3 csctl.py price --all` |
| 看现货库存 | `python3 csctl.py stock` |
| 找镜像 | `python3 csctl.py images --keyword ComfyUI` |
| **一键部署** | `python3 csctl.py up --gpu-type 4090 --hours 4` |
| 抢不到卡时挂机等货并自动下单 | `python3 csctl.py up --gpu-type 4090 --hours 4 --stock-timeout 7200 --poll 60` |
| 用更便宜的抢占式 | `python3 csctl.py up --gpu-type 4090 --charge Spot --hours 4` |
| 首选卡没货自动换卡 | `python3 csctl.py up --gpu-type 4090 --fallback 3090,V100S --hours 4` |
| 远程执行命令 | `python3 csctl.py ssh <ID> --exec "nvidia-smi"` |
| 关机（算力费归零） | `python3 csctl.py stop cs-mmddHHMM` |
| 开机 | `python3 csctl.py start <ID>` |
| 定时关机兜底 | `python3 csctl.py stop-at <ID> --in 3` / `--at 23:30` |
| 取消定时关机 | `python3 csctl.py cancel-stop <ID>` |
| 无卡模式（0.15 元/时挂机） | `python3 csctl.py cardless <ID> on` / `off` |
| 打包环境成镜像 | `python3 csctl.py save-image <ID> --name my-comfyui` |
| 空闲自动关机守护 | `python3 csctl.py watch --idle-minutes 15` |
| 彻底释放 | `python3 csctl.py rm <ID> --yes` |
| 花费总览 | `python3 csctl.py status` |

`up` 的默认动作：查价 → 等库存 → 下单 → **轮询到 Running** → 直接打印 SSH 命令 / 密码 / 应用访问地址 → **自动设定时关机**（`--hours`，默认 4 小时，忘关也不会烧钱）。

## 2. 成本模型（2026-10-26 实测，1 卡最小规格，系统盘 100G）

| 卡型 | 规格 | 按量 /h | 抢占 /h | 抢占省 | 包日 | 包月 |
| --- | --- | --- | --- | --- | --- | --- |
| 2080 | 8C/40G | ¥0.42 | 不支持 | - | ¥9.24 | ¥252 |
| P40 | 8C/64G | ¥0.40 | 不支持 | - | ¥8.97 | ¥244.84 |
| 3080Ti | 12C/32G | ¥0.74 | ¥0.52 | -30% | ¥16.30 | ¥444.72 |
| 3090 | 16C/64G | ¥1.19 | ¥0.83 | -30% | ¥26.38 | ¥719.64 |
| **4090** | 16C/64G | **¥2.15** | **¥1.38** | **-36%** | ¥47.47 | ¥1294.75 |
| 4090_48G | 16C/96G | ¥3.80 | 不支持 | - | ¥83.60 | ¥2280 |
| 5090 | 16C/96G | ¥3.99 | ¥2.21 | -45% | ¥87.91 | ¥2397.60 |
| V100S | 10C/64G | ¥1.41 | ¥0.98 | -30% | ¥31.17 | ¥850.20 |
| A800 | 16C/240G | ¥7.36 | ¥3.02 | -59% | ¥162.07 | ¥4420.32 |
| H20 | 16C/240G | ¥7.50 | 不支持 | - | ¥165 | ¥4500 |
| A100 | 16C/64G | ¥10.75 | ¥7.52 | -30% | ¥236.58 | ¥6452.40 |

**计费规则（决定省钱打法）**

- 按量后付费 = **秒级计费**，整点出账单；**关机后 CPU/GPU/内存停止收费**，只保留云盘。
- **系统盘 SSD ≤100GB 免费**（工具默认就是 100G，所以盘费 0 元），超出部分才收；数据盘全量收费。
- 抢占式：价格更低，**可能随时被回收**；运行 <1h 被回收**免单**，≥1h 按实际时长；回收后数据保留 7 天、可再启动；**不支持手动关机**，不用就删。
- 包日/包月只适合 7 天以上的常驻；短期任务用「按量 or 抢占」更便宜且随时可停。
- 无卡模式（关机后用 CPU 开机）：2C4G **¥0.15/h**、8C16G ¥0.60/h，A800 不支持。

## 3. 省钱七板斧（工具已内置）

1. **系统盘锁 100G** —— 盘费恒为 0；要存数据用 `--data-disk` 或对象存储，别把系统盘撑大。
2. **按量 + 用完即关** —— 关机后算力费为 0，1 分钟不跑就关，脚本一条命令。
3. **空闲守护 `watch`** —— 每轮读 GPU 利用率，连续 N 分钟低于阈值就关机（`--stop-action stop|cardless|terminate`）。等于雇了个不要工资的运维，杜绝「忘了关，跑一夜空转」。
4. **定时关机兜底** —— `up` 默认 `--hours 4`，即使脚本崩了、人跑了，也会在到期时间点被平台强制关机。
5. **抢占式 Spot** —— 同样 4090 从 ¥2.15 砍到 ¥1.38（-36%）；能断点续跑的训练/批处理优先用，配合 `--hours 1`+断点续存，被回收 <1h 还免单。
6. **无卡模式** —— 装依赖、下模型、跑数据处理这类不用 GPU 的活，用 0.15 元/时 的 CPU 机器干，比 4090 空转便宜 93%。
7. **自制镜像 + 删机** —— 环境调好后 `save-image`，之后删掉实例（不再收任何盘费），下次用镜像秒级重建；比「长期留停机实例」更省。

## 4. 省时间：脚本在替人做什么

- `stock --watch`/`up --stock-timeout`：**盯着库存自动下单**。热门卡（4090/5090）经常 0 现货，人工要反复刷新，脚本按 `--poll` 秒轮询，一有货立刻创建。
- `up` 内置 `Install → Running` 状态轮询，就绪瞬间打印 SSH / 密码 / ComfyUI 访问地址，不用守在控制台等。
- `ls` 一行看到「已跑时长 / 已花金额 / 定时关机时间」，不用翻账单。
- `watch --once` 可以塞进 crontab，每 5 分钟体检一次。

## 5. 装成常驻守护（推荐）

```bash
bash scripts/install_cron.sh          # 每 5 分钟跑一次空闲检查，闲置 15 分钟自动关机
crontab -l | tail -3                  # 确认
```

日志：`.compshare/watch.log`（`tail -f` 看）。

## 6. 已知的坑（踩过，已处理）

- 定时关机接口参数名是 **`SchedulerStopTime`**（不是 `StopTime`），且**强制要 `ProjectId`**（工具自动补），并要求 ≥ 当前+300 秒；Spot 实例不支持定时关机。
- `DescribeCompShareInstance` **不返回** `SchedulerStopTime`/`InstancePrice`，所以工具把定时关机时间记在本地 `.compshare/stop_at.json`，单价用价格接口按配置换算（带缓存）。
- `GetCompShareInstanceMonitor` 用表单编码会报 `Missing params [UHostIds]`，必须走 **JSON body**；工具内置了 JSON 签名通道。
- 无卡模式没有独立接口：走 `StartCompShareInstance` 加 **`WithoutGpu=true`**（已验证可用）；`WithoutGpuSpec` 字段为空，不能用来判断是否在无卡模式。
- 关机/开机瞬间重复操作会报 `8903 UHost is in the task`；工具会先 `settle` 等状态落定再动手。
- `DescribeCompShareImages` 单独传 `Limit` 会报 `Params [Limit] not available`；工具改成 `Limit+Offset` 分页。
- 价格接口返回字段是 `PriceDetails`（复数），用 `GetCompShareInstanceUserPrice`（含折扣）而不是 `GetCompShareInstancePrice`（返回空）。
- Spot 不支持手动关机，也不支持 `4090_48G / H20 / P40 / 2080`；`up --charge Spot` 会自动降级为按量。
- A800 实例不支持无卡开机。
- 账户等级限制实例数：未充值 1 台、已充值(Level1) 5 台、Level2 20 台……；有未支付订单时无法创建。
- 创建前建议先看 `stock`，现货为 0 时下单会失败/排队。

## 7. 实测记录（端到端已验证）

在本账户上跑过一轮完整生命周期（`uhost-1wxnmqheyazw`，V100S / Ubuntu-nvidia 22.04）：

| 动作 | 结果 |
| --- | --- |
| `up` 下单→Running | 约 40 秒内就绪，自动打印 `ssh ubuntu@<IP>` + 密码 |
| 远程执行 | `ssh <ID> --exec "nvidia-smi -L"` → `Tesla V100-SXM2-32GB` ✔ |
| `stop` | 1 秒进 `Stopping`，8 秒变 `Stopped`，算力费立即停 |
| `start` | 12 秒回到 `Running`，GPU 正常 |
| `cardless on` | 关机后 CPU 模式开机成功（`WithoutGpu=true`），0.15 元/时级别 |
| `cardless off` | 关机→GPU 开机，`nvidia-smi` 恢复 `Tesla V100S-PCIE-32GB` ✔ |
| `stop-at --in 0.5` | 定时关机设置成功（平台要求 ≥5 分钟后） |
| `watch --ssh --once` | 读到 `gpu=0% gpuprocs=0` 并累计 idle 计时 ✔ |
| `rm --yes` | 删除成功，实例列表清空 |
| 总花费 | 余额 54.09 → 53.99，**整轮 ¥0.10** |

## 8. 典型工作流（视频生成）

```bash
# ① 有卡就上，4 小时后自动关机；单价超过 3 元/时就中止
python3 csctl.py up --gpu-type 4090 --charge Spot --image ComfyUI --hours 4 --max-hourly 3
# ② 打开 ComfyUI 页面跑活（地址已在 ① 的输出里）
# ③ 干完立刻关，或交给守护
python3 csctl.py stop cs-xxxx
# ④ 环境满意 → 存镜像 → 删机（后续 0 盘费）
python3 csctl.py save-image cs-xxxx --name comfyui-wan22
python3 csctl.py rm cs-xxxx --yes
```

## 9. 安全

> `compshare/credentials.env` 里是可直接花的 AccessKey，已设 600 权限，别提交到 git、别贴群里。

---

## 10. 现在的状况（2026-10-26 13:05）

- 余额 **¥53.99**，无欠费、无遗留实例（冒烟测试实例已删除）。
- 可用区 `cn-wlcb-01` 现货：**V100S 6 张**、A100 0 张，**4090 / 5090 / 3090 目前 0 现货**（热门卡常态化缺货）。
- 因此现在直接上 4090 会失败。两个方案：
  1. 挂机等货自动下单：`python3 csctl.py up --gpu-type 4090 --hours 4 --stock-timeout 7200`（一有卡立刻创建，不用守着刷）；
  2. 先用有货的卡顶上：`python3 csctl.py up --gpu-type 4090 --fallback 3090,V100S --hours 4`（自动切到 V100S，¥1.41/h）。

按 ¥53.99 余额估算续航：4090 按量 ≈ 25 小时 / 4090 抢占 ≈ 39 小时 / V100S 按量 ≈ 38 小时 / 5090 抢占 ≈ 24 小时。

---

## 11. 平板用的文件窗口

地址（收藏即可）：**http://123.207.74.155/files/<TOKEN>/**

- 这里放的是本目录的只读副本（**不含 `credentials.env`**），点文件名可在浏览器里直接预览：
  - `.md` 按文档排版渲染（表格、代码块可读）
  - `.py` / `.sh` 按代码块显示
  - 图片直接内嵌
- 每行右侧「下载」按钮 = 直接存到平板；顶部「全部 zip」= 一次打包全部文件。
- 底部「上传文件给我」可以把你平板上的截图 / 日志传上来，落到 `share/_incoming/`，我这边立刻能读。
- 我这边改了文件后，页面点「↻ 刷新列表」即可看到最新版本（同步命令：`bash scripts/sync_share.sh`）。

服务与关闭：

```bash
systemctl status cs-files        # 运行状态（服务名 cs-files，监听 127.0.0.1:8081，经 nginx /files/ 反代）
systemctl stop cs-files          # 临时关掉这个窗口
cat .share_token                 # 访问口令；删掉该文件并重启服务会换新链接
```

---

## 12. 自动化基准实验室（cs-lab）

```bash
systemctl status cs-lab     # 等货中 / 正在跑
tail -f logs/lab.log        # 实时日志
cat lab/state.json          # 每台机器的结果与花费
touch lab/STOP && systemctl stop cs-lab   # 急停
```

- 等现货 → 建机 → 一台机器上连跑多个「模型 × 配置」→ 收图/视频 → **销毁** → 出报告
- 矩阵：4090（cn-wlcb-01 / sh2-02 / sh2-01 / bj2-03 四地域）、5090、3090、V100S、4090_48G ×
  SD1.5 / SDXL / FLUX（高端卡）/ Wan2.1 i2v（仅 4090/5090）× 不同步数与分辨率
- 指标：建机→Running、ComfyUI 就绪、冷启推理、预热推理、显存峰值、实扣金额
- 输出：`reports/lab/REPORT.md`（表）+ `reports/lab/gallery.html`（按模型分组的图片/视频，自动同步到平板窗口）
- 安全阀：总预算 ¥8、单机 35 分钟上限、建机即设定时关机兜底、异常必销毁、启动时清理孤儿 `lab-*` 实例


---

## 13. 修复记录 & 新命令（2026-10-26 17:20）

- `csctl cost` 花费明细（来自 `.compshare/ledger.jsonl`）
- `csctl jobs` 每次建机的 job 记录
- `up --balance-floor 5 --daily-budget 20` 预算熔断；`up` 自带 flock 与同名查重
- `rm <id> --yes --archive reports/archive` 销毁前 rsync 拉回产物
- `ssh <id> --exec "cmd" --detach --log /root/x.log` 后台执行，断连不停
- 空闲守护已装 crontab（每 5 分钟，跳过 `lab-*`）
- 画廊改用缩略图 + 指标表：`reports/lab/gallery.html`


---

## 14. 模型准备与传输通道（2026-10-26 晚）

- `csctl provision <ID> --ensure wan21_i2v,ltxv,hunyuan_i2v`
  → 自动写 `extra_model_paths.yaml` 挂载 `/model` 共享库（unet 可见数 2 → 156），重启 ComfyUI，核对 loader 可见性，输出缺失清单。
  `--download` 时缺文件会切**无卡模式（0.15~0.6 元/时）**下载，再切回 GPU。
- `csctl pull / push`：传输层 `tools/transfer.py`
  · 单文件断点续传（200MB 实测：34.6MB 断点 → 续传补齐，sha256 一致）
  · 目录默认 `scp -r`（80MB/40 文件 7.3s，11MB/s）
  · 大量小文件走 tar 单流（200 个小文件 4.2s，比 scp -r 快 4.4×）
  · 统一 `-c aes128-gcm@openssh.com`；本机/远端都有 rsync 时优先增量
- `rm --archive` 改用 Transfer，**不再依赖实例上的 rsync**（该镜像没有）。
- 当前视频模型可用性：**LTXV 2.3 / HunyuanVideo i2v 齐全**；Wan2.1 只缺 `wan_2.1_vae`（242MB）。


---

## 15. 出图/出片实测（2026-10-26 晚）

- **出图**（V100S · SDXL juggernaut · 1280×720 · 20 步 · seed 20261026）：冷跑两次 12.13s / 11.28s，像素 md5 一致（种子可复现）。
  注：ComfyUI 对**完全相同的图**会走节点缓存（第二次 1.01s），对比多次生成时需换 `filename_prefix` 或重启 ComfyUI。
- **出片**（4090 · SVD-XT 链式 i2v · 832×480 · 97 帧 @19.4fps）：shot1 124.2s / shot2 111.4s，各 5.000s，同实例同参数、期间未销毁。
- **Wan2.1 i2v 暂不可用**：fp8 权重 + int8 umt5 在 ComfyUI 0.3.40 上输出纯黑（试过 4/20 步、cfg 1/5、euler/uni_pc、加 ModelSamplingSD3 shift=8），需换 0.36.x 镜像再验。
- 成本：图片实例 ¥0.09（V100S 约 4 分钟），视频实例 ¥0.63（4090 17.9 分钟）。

## 16. 生命周期审计、检查点与风险监控（2026-10-26 夜，实机验证）

审计报告：`reports/audit-storage-2026-10-26.html`

### SSH 断开后任务是否继续（A/B/C/D 实测）

| 组 | 启动方式 | 断开后 | PPID |
|---|---|---|---|
| A | 前台裸跑（非交互 ssh，无 tty） | ✅ 继续 | 1 |
| B | `nohup … &` | ✅ 继续 | 1 |
| C | `setsid nohup … &`（`csctl ssh --detach`） | ✅ 继续 | 1 |
| D | 前台 + `ssh -tt` 强制 tty | ❌ 被杀 | — |

**决定生死的是 tty，不是 nohup**：sshd 只向已分配 tty 的会话发 SIGHUP。
ComfyUI（`bash /start.d/comfyui.sh`，PPID=1）、Jupyter、FileBrowser 都是 entrypoint 起的，与 SSH 无关。
长任务用 `csctl ssh <id> --detach --log /root/job.log`，或直接打 `:8188` HTTP。

### 存储生命周期矩阵

| 路径 | stop→start | reboot | save-image→新建 | terminate |
|---|---|---|---|---|
| `/root` `/tmp` `/var/log` `/etc` conda output workflows | 保留 | 保留 | 保留（镜像内） | **删除** |
| 数据盘 `/cloud/cloud-ssd1` | 保留 | 保留 | **丢失**（新盘） | 删除 |
| `/model`（UPFS 40T，**只读**） | 保留 | 保留 | 保留 | **保留** |

- 镜像 30GB，制作 2 分 23 秒；`compshareImage-1wyak4svn41r`
- **`save-image` 会让实例里新进程失去 NVML**（`nvidia-smi` 报 Unknown Error、`torch.cuda` False），但已持有 CUDA context 的 ComfyUI 不受影响；**stop→start 完全恢复**
- SSH 会话里 `torch.cuda.is_available()` 常为 False → 不要在 SSH 里直接跑 torch 推理，走 ComfyUI HTTP

### 检查点（CP0–CP7）

`tools/checkpoint.py --instance <id> [--video]`，脚本经 stdin 下发（`lab/checkpoint.sh`），不依赖实例预置文件。

CP0 开机就绪（NVML / 服务端 cuda / 8188 / 共享库）· CP1 环境（torch·ComfyUI·PyAV·PIL）· CP2 模型检查点（`juggernautXL`、`checkpoints/svd_xt.safetensors`）· CP3 输入（input/、workflows/）· CP4 运行（**启动链根 PPID=1**、日志无 Traceback）· CP5 输出 · CP6 资源（盘 <85%、数据盘）· CP7 可追溯。

### 风险监控（R01–R16）

`tools/riskmon.py` → `reports/risk/latest.json` + `alerts.md`；页面 `<token>/risk`；cron 每 10 分钟。
覆盖：余额、无定时关机、孤儿 lab、日预算、单价、无货、SSH 不就绪、服务掉线、cron 缺失、凭证权限、磁盘、基准失败（已过滤 226604/226628 等可重试）、黑帧（ffmpeg signalstats）、模型库、漏归档、`--deep` 实例内部健康。
