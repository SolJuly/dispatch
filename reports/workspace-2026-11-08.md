# 工作台清单 · 智云视频工作流

> 分支 `feature-/creative-engine` · HEAD `30b5a28 2026-11-07 13:46 fix(p1): 动作迁移跑 23 分钟的三件事——帧数循环、服务崩溃傻等、估价按 77 帧`
> 相对 main 75 个提交 · 工作区 31 改 / 38 新增（未提交）

## 一、这是什么

一个跑在智云（优云智算）GPU 上的 **AI 出图 / 出视频流水线**：
从建实例、挂模型、跑 ComfyUI、拉产物到校验销毁，全链路自动化，支持预算控制与并发去重。

代码规模：**127 个 Python 文件 / 30150 行**（不含 share 部署副本）。

## 二、顶层目录

| 目录/文件 | 作用 | 规模 |
|---|---|---|
| `csctl.py` | 优云智算 GPU 实例管理 CLI（建机/关机/成本/SSH/上传下载） | 1178 行 |
| `fileserver.py` | 控制台 + 文件分享服务（路由、上传、页面渲染） | 1430 行 |
| `webui/` | 控制台数据层与页面（创作台/任务/详情/图库/工作流） | 14 文件 188K |
| `tools/` | 全部业务模块（p1 执行器 / p2 批量 / p20 注册表 / p21 探测 / p22 试验） | 179 文件 2.0M |
| `registry/` | 工作流与模型注册表（54 工作流 / 49 模型）+ 派生 DB | 105 文件 1.5M |
| `workflows/` | ComfyUI API 图（扁平 {id:{class_type,inputs}}） | 55 文件 468K |
| `reports/` | 审计报告、真机日志、作业产物、架构图 | 1143 文件 362M |
| `scripts/` | 部署、发布、预算基线采集、文档生成 | 34 文件 336K |
| `deploy/` | 远端部署脚本与 systemd 单元 | 3 文件 28K |
| `docker/` | ComfyUI 运行时镜像（Dockerfile / 启动脚本 / 节点安装） | 6 文件 32K |
| `docs/` | API 文档与 OpenAPI 描述 | 3 文件 128K |
| `tests/` | 并发建机、快速销毁、状态转移测试 | 8 文件 112K |
| `lab/` | 早期实验脚本与状态快照 | 66 文件 444K |
| `share/` | 部署副本（gitignore，运行时同步） | 3623 文件 663M |

## 三、tools/ 模块分工

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `p1/` | 单任务执行器与状态机、产物校验、作业存储 | `run.py`(1323) `jobctl.py`(509) `verify.py` `state.py` `store.py` |
| `p2/` | 批量调度、预热池、冻结检测、拼接 | `jobqueue.py`(515) `scheduler.py` `pool.py` `freeze.py` `batchctl.py` |
| `p20/` | 工作流注册表与渲染（P2.0 核心） | `registry.py`(674) `p20ctl.py`(556) `render.py`(542) `config_schema.py`(534) |
| `p21/` | 真机探测（模型资产、分辨率、反演） | `probe_assets.py` `bench20.py` `inventory.py` |
| `p22/` | 工作流试验与回归 | `trial.py` `regress.py` `sweep.py` |
| `webui/` | 控制台测试 | `tests/` |

## 四、注册表内容

**54 个工作流**（`registry/workflows/`）：

| 前缀 | 数量 | 说明 |
|---|---|---|
| `pack_*` | 32 | 官方模板转换（视频/图片工具链） |
| `zimage_*` | 5 | Z-Image-Turbo 系列 |
| `sdxl_*` | 4 | SDXL 系列 |
| `qwen_image_*` | 4 | Qwen-Image 系列 |
| `seedvr2_*` | 2 | 超分 |
| `h3_*` | 2 | MiniMax H3 |
| 其他 | 5 | wan21 / ltxv / flux2 / upscale / detail_polish |

**49 个模型卡**（`registry/models/`），每个含权重文件、加载器槽位、约束、软链配置。

## 五、主要入口

```bash
python3 csctl.py init                 # 连通性/现货/余额自检
python3 csctl.py up --gpu-type 4090   # 一键建机
python3 csctl.py ls / status / cost   # 实例 / 账户 / 花费
python3 csctl.py ls | grep 运行中      # 看当前实例

python3 tools/p1/jobctl.py new --spec X.json   # 建作业（含查重）
python3 tools/p1/run.py <job_id>               # 执行（建机→出片→销毁）
python3 tools/p1/jobctl.py show <job_id>       # 状态链

python3 -m tools.p20.p20ctl validate            # 注册表体检
python3 -m tools.p20.p20ctl compat list         # 兼容性矩阵
python3 -m tools.p20.p20ctl import              # 重建 DB（改 JSON 后必跑）

python3 tools/p2/batchctl.py submit --specs '...'  # 批量

python3 fileserver.py                           # 控制台（默认 8081）
```

## 六、当前运行状态

| 项 | 值 |
|---|---|
| 账户余额 | ¥9.78  (冻结 ¥0.00) |
| 实例 | 0 台，运行中 0 台 |
| 算力 burn | ¥0.00/小时  ≈ ¥0.00/天 |
| 磁盘 burn | ¥0.00/小时（≤100G 系统盘免费，长期不用请删除实例） |
| 测试 | 260 条全绿（p20 104 / p1 51 / webui 58 / p2 47） |
| 注册表 | 54 工作流 / 49 模型 / 断链 0 |
| 服务 | fileserver 8081（127.0.0.1）· nginx 80 |

## 七、本会话产出的报告

| 文件 | 内容 |
|---|---|
| `reports/branch-audit-2026-11-08.html` | 分支审计报告（18 项发现 / 能力三档 / 自查表） |
| `reports/branch-audit-2026-11-08.md` | 同上 Markdown 版 |
| `reports/verify-video-engine-2026-11-07.md` | 视频引擎修复真机验证（含 4 次真机作业） |
| `reports/audit-video-engine-2026-11-07.md` | 视频引擎初版审计（13 项发现） |


## 八、目录树（两层，不含 share/reports 内部）

```
.
./.compshare
./compshare
./.compshare/ssh
./deploy
./deploy/remote
./deploy/systemd
./dist
./docker
./docs
./lab
./logs
./logs/dryrun
./registry
./registry/links
./registry/models
./registry/workflows
./reports
./scripts
./tests
./tools
./tools/p1
./tools/p2
./tools/p20
./tools/p21
./tools/p22
./tools/webui
./webui
./workflows
./workflows/pack
```
