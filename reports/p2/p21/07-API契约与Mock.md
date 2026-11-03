# P2.1 后端接口契约与 Mock（给前端并行开发）

> 契约：<https://123.207.74.155/pub/api/openapi.json>（OpenAPI 3.1，可直接导入 Swagger Editor / Postman / Apifox）
> 清单页：<https://123.207.74.155/pub/api/>
> 生成脚本：`python3 scripts/build_openapi.py`（改契约改脚本，重新生成）
> Mock：`python3 scripts/mock_api.py` → `http://127.0.0.1:8099/files/23e00ec2/`

## 1. 三个约定

1. **鉴权在路径里**：`/files/<token>/api/p2/...`，不是 Header/Cookie。前端把 `token` 当常量拼在 baseURL。
2. **`x-status` 标注**：`implemented` 可直接联调；`planned` 契约先定、先用 Mock，联调前会补实现（预计 P2.2）。
3. **产物地址是静态的**：`artifact.url` 形如 `/pub/jobs/<job_id>/<file>`，与后端进程无关；控制台页面（`/files/.../app/*`）依赖进程，产物不依赖。
4. **先探测版本**：前端启动时先打 `GET /api/p2/version`。返回 `api: "p2.1"` + `features` 才是新后端；**404 说明线上是旧进程**，别继续调其它接口（旧进程的 `/app/*` 也会 404，但 jobs 仍返回旧形状数据，很容易误判）。详见 `08-后端版本探测与恢复.md`。

## 2. Mock 怎么用

```bash
python3 scripts/mock_api.py            # 默认 127.0.0.1:8099
# 前端 baseURL = http://127.0.0.1:8099/files/23e00ec2
```

- GET 一律返回 `openapi.json` 里的示例；
- **有状态**：`POST /api/p2/jobs` 会真的建一条（返回新 `job_id`），`GET /api/p2/jobs/<id>` 能查到，`cancel` 会把它置 `ABORTED`；
- `POST /api/p2/uploads` 按内容哈希返回 `in-<sha8>.png`，和线上一致；
- `GET /api/p2/jobs/zip?ids=...` 返回一个真 zip（内含占位 PNG），下载交互可以真跑通；
- 产物 URL（`/pub/jobs/...`）返回 64×64 占位图。

## 3. 接口清单

### 3.1 作业（核心）

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| GET | `/api/p2/jobs` | ✅ | 列表；`limit`、`state`（`FAILED` 前缀匹配）、`workflow`、`q` |
| POST | `/api/p2/jobs` | ✅ | 建作业；`{workflow, params, run}`；`run=false` 不花钱 |
| GET | `/api/p2/jobs/{id}` | ✅ | 详情：`job` + `artifacts[]` + `terminal` |
| POST | `/api/p2/jobs/{id}/cancel` | ✅ | 仅 `CREATED` 可取消 → `ABORTED`；执行中返回 409 |
| POST | `/api/p2/jobs/{id}/rerun` | 🟡 | 同参重跑（P2.2），先用 Mock |
| GET | `/api/p2/jobs/zip?ids=a,b` | ✅ | 批量下载，`application/zip` |

### 3.2 上传 / 文件

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| POST | `/api/p2/uploads` | ✅ | multipart `file`；PNG/JPEG/WebP ≤20MB；返回 `in-<sha8>.<ext>` |
| GET | `/api/p2/jobs/{id}/artifacts/{name}` | 🟡 | 鉴权下载（P2.2）；现在请直接用 `artifact.url` |

### 3.3 Registry（表单数据源）

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| GET | `/api/p2/registry` | ✅ | 统计摘要 |
| GET | `/api/p2/registry/workflows` | ✅ | 工作流 + **参数契约**（type/default/min/max/values/bindings） |
| GET | `/api/p2/registry/workflows/{key}` | ✅ | 单个工作流 |
| GET | `/api/p2/registry/models` · `/{key}` | ✅ | 模型与约束 |
| GET | `/api/p2/registry/compat` | ✅ | 兼容矩阵 |
| GET | `/api/p2/registry/resolve` | ✅ | 预演：补默认值 + 渲染 + 期望值（不落盘） |

### 3.4 队列 / 状态（旧契约，时间线用）

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| GET | `/api/p2/queue` | ✅ | 队列统计 |
| GET | `/api/p2/queue/jobs` · `/{id}` | ✅ | **旧** `/api/p2/jobs` 契约，已挪到这里 |
| GET | `/api/p2/audit?since=` | ✅ | 审计事件增量 |
| GET | `/api/p2/events?since=` | ✅ | 事件流增量 |
| GET | `/api/p2/status` | ✅ | 系统状态 |

### 3.5 规划中（先用 Mock）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/p2/presets` | 参数预设（存 `share/presets.json`） |
| GET | `/api/p2/share/{job_id}?sig=` | 公开分享页数据（签名，不暴露主 token） |

## 4. 关键数据契约

**作业状态**：`CREATED → PROVISIONING → READY → SUBMITTED → RUNNING → FETCHING → VERIFYING → PERSISTED → RELEASING → DONE`；失败分支 `FAILED_PROVISION / FAILED_RUN / FAILED_FETCH / FAILED_VERIFY / FAILED`；`ABORTED`。
前端建议：`terminal = state in (DONE, FAILED*, ABORTED)`，用后端给的 `terminal` 字段即可。

**参数**：`params` 必须符合工作流 `params` 契约——**多传一个键会被 400 拒绝**（这是有意的，防止参数漂移）。表单请按 `type` 渲染：`string / int / float / bool / enum(values) / image(上传后填文件名)`。

**多图**：`batch_size>1` 时 `expect.images` 等于张数，`artifacts` 会有多张；张数不符后端直接判失败。

**上传后**：`params.image = "in-<sha8>.png"`（图生图底图）、`params.mask = "in-<sha8>.png"`（局部重绘蒙版，白=重绘）；预览用返回的 `url`（`/f/inputs/...`）。

**轮询**：建议 1.5–2s；`terminal=true` 停止。单张 SDXL 约 5s，Qwen 1024² 约 22s，含开机时整单 1–3 分钟。

## 5. 与现有页面的一致性

现有控制台（`/files/<token>/app/*`）已经按这套契约实现，可直接当参考实现看：

- 表单渲染：`webui/components.py`（按 `type` 出控件）
- 提交/轮询/结果：`webui/assets.py`（`POST /api/p2/jobs` → 轮询 → 渲染 `artifacts`）
- 批量下载、最近作业：`webui/pages.py`

## 6. 需要前端确认的三件事

1. **重跑/分享** 是否在 M4 就要（这两条目前是 `planned`，可以先按契约做 UI + Mock）。
2. 任务列表要不要**分页游标**（现在只有 `limit`，超过 50 条只能靠前端截断；需要的话我加 `cursor`）。
3. 产物下载要**鉴权接口**还是继续用静态 URL（现在静态 URL 无鉴权，知道路径即可访问）。

## 7. 契约自检

```bash
python3 -m unittest tools.p20.tests.test_openapi   # 8 条：$ref 可解析 / operationId 唯一 / 示例齐全 /
                                                   #   implemented 路径在 fileserver.py 真存在 / Mock 覆盖全部 GET
python3 scripts/build_openapi.py                   # 重新生成 docs/openapi.json
python3 scripts/publish_api_docs.py                # 发布到 /pub/api/
```
