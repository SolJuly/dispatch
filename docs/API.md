# P2 只读 API（UI 对接文档）

> **机器可读契约（推荐）**：`docs/openapi.json`（OpenAPI 3.1，由 `scripts/build_openapi.py` 生成）
> 在线：https://123.207.74.155/pub/api/openapi.json · 清单页：https://123.207.74.155/pub/api/
> 前端联调可用 `python3 scripts/mock_api.py` 起本地 Mock（有状态：建作业/取消/上传）。
> 本文档保留为人读说明；接口以 openapi.json 为准。


> 版本：`v0.4-p20-registry` · 2026-11-01
> 全部 **GET / 只读 / JSON**，沿用文件服务的 token 鉴权；无写接口（提交/取消仍走 `batchctl`，Registry 导入仍走 `p20ctl`）。

---

## 0. 鉴权与基址

```
https://<host>/files/<TOKEN>/api/p2/...
```

`TOKEN` = 仓库根目录 `.share_token` 的值（当前 `{TOKEN}`）。token 不对一律 `404`（不区分「无权限」和「不存在」）。

本机直连（调试用）：`http://127.0.0.1:8081/<TOKEN>/api/p2/...`

**CORS**：未开启。UI 需与本服务同源，或由 nginx 反代同域。

---

## 0.1 先探测版本：`GET /api/p2/version`

前端启动时**先打这条**。它返回构建标识与能力表；旧后端（P2.1 之前）没有这条路由，
直接 404 —— 这时不要继续调其它接口，提示「线上后端是旧版本，请用 Mock 联调或等待部署」。

```bash
curl -s "https://123.207.74.155/files/<TOKEN>/api/p2/version"
```

```json
{
  "service": "dsh-share", "ok": true, "api": "p2.1", "contract": "openapi-3.1",
  "build": {"commit": "3180f15", "branch": "feature-/creative-engine", "dirty": true, "dirty_files": 4},
  "started_at": "2026-11-03T13:34:34+08:00", "uptime_s": 42, "pid": 8123,
  "bind": "127.0.0.1:8081", "base": "/files",
  "capabilities": ["console", "registry", "jobs", "uploads", "zip", "version"],
  "features": {"console": true, "registry": true, "jobs": true, "uploads": true, "zip": true, "version": true},
  "registry": {"workflows": 13, "models": 6},
  "docs": {"openapi": "/pub/api/openapi.json", "handoff": "/pub/p21/07-API契约与Mock.html"}
}
```

前端判定建议：

```js
const v = await fetch(`${BASE}/api/p2/version`).then(r => r.ok ? r.json() : null);
if (!v) throw new Error("线上后端是旧版本（无 /api/p2/version），请改用 Mock");
if (!v.features.console) throw new Error("后端缺控制台页面，请重新部署");
```

`uptime_s` 很小、`build.commit` 与预期不符时，说明进程刚被重启或跑的还是旧代码。

---

## 1. 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/p2/version` | 后端版本/能力探测（前端启动先打） |
| GET | `/api/p2/queue` | 队列汇总 + 全部条目（轻量字段） |
| GET | `/api/p2/jobs` | 作业列表（含判定/复验/产物） |
| GET | `/api/p2/jobs/<job_id>` | 单作业详情 + 状态机事件 |
| GET | `/api/p2/audit?job=&since=&limit=` | 审计流水（id 游标，可增量轮询） |
| GET | `/api/p2/events?job=&since=` | 状态机事件（seq 游标，可增量轮询） |
| GET | `/api/p2/registry` | P2.0 Registry 统计 + 目录摘要 |
| GET | `/api/p2/registry/workflows` | 工作流列表（含兼容性状态） |
| GET | `/api/p2/registry/workflows/<key>` | 工作流详情（参数/绑定/输出/兼容性） |
| GET | `/api/p2/registry/models` | 模型列表（角色 → 权重文件） |
| GET | `/api/p2/registry/models/<key>` | 模型详情（默认值/约束/setup） |
| GET | `/api/p2/registry/compat` | 兼容性矩阵 |
| GET | `/api/p2/registry/resolve?workflow=&model=&params=` | 参数解析 + 渲染预览（不落盘） |

---

## 2. 接口详情

### 2.1 `GET /api/p2/queue`

```json
{
  "stats": {"total": 6, "by_state": {
      "done": {"n": 2, "cost": 0.097},
      "cancelled": {"n": 4, "cost": 0.057}}},
  "items": [
    {"job_id": "p1-20261101T1458-2e7352", "state": "done", "model_key": "ltxv_2b",
     "attempts": 1, "defers": 0, "cost": 0.0502, "exit_code": 0,
     "enqueued_at": "2026-11-01 14:58:47"}
  ]
}
```

`state` 枚举：`queued` / `claimed` / `running` / `done` / `failed` / `terminal_failed` / `cancelled` / `skipped` / `reclaimed`

### 2.2 `GET /api/p2/jobs`

```json
{"total": 6, "jobs": [ <job_view>, ... ]}
```

`<job_view>` 字段（下同）：

| 字段 | 说明 |
|---|---|
| `job_id` | 作业 ID（`p1-YYYYMMDDTHHMM-6hex`） |
| `queue_state` | 队列态（见上枚举） |
| `job_state` | 状态机态（`CREATED/PROVISIONING/…/DONE/FAILED/ABORTED`） |
| `model` / `kind` | 模型 key / `video`\|`image` |
| `attempts` / `defers` | 已尝试次数 / 无货延迟次数 |
| `instance_id` | GPU 实例 ID（未建机为 `null`） |
| `exit_code` | worker 退出码（终态才有） |
| `cost` / `gpu_minutes` | 累计成本（元）/ GPU 分钟（重试会累加） |
| `verdict` | `PASS` / `FAIL` / `null` |
| `checks` | 七项复验：`decode/geometry/frames/size/black/freeze/params` |
| `artifacts` | 产物相对路径列表 |
| `enqueued_at/started_at/ended_at` | 时间戳 |
| `last_error` | 最近失败原因（截断 300 字） |

### 2.3 `GET /api/p2/jobs/<job_id>`

```json
{
  "job": { "job_id": "...", "queue_state": "done", "job_state": "DONE",
           "verdict": "PASS", "checks": {"freeze": true, "...": true},
           "cost": 0.0502, "gpu_minutes": 1.4, "artifacts": ["artifacts/01-....mp4"] },
  "events": [
    {"seq": 1, "at": "2026-11-01T14:58:47+08:00", "from": null,
     "to": "CREATED", "reason": "新建作业（P1-T11）"}
  ]
}
```

`events` 是 job 状态机的全量事件（`from → to`），可直接画时间线。

**404**：作业不存在。

### 2.4 `GET /api/p2/audit?job=<id>&since=<id>&limit=<n>`

| 参数 | 默认 | 说明 |
|---|---|---|
| `job` | 全部 | 只看某个作业 |
| `since` | `0` | 只返回 `id > since` 的行（游标） |
| `limit` | `200` | 上限 1000 |

```json
{"rows": [{"id": 12, "ts": 1793516327, "job_id": "p1-...",
           "instance_id": "uhost-1x6...", "from_state": "running",
           "to_state": "done", "exit_code": 0, "gpu_minutes": 1.4,
           "detail": ""}],
 "next": 12}
```

`next` 下次轮询时作为 `since` 传入。`ts` 是 Unix 秒。

### 2.5 `GET /api/p2/events?job=<id>&since=<seq>`

只返回 `seq > since` 的事件，用于作业进行中的细粒度轮询。

```json
{"events": [{"seq": 7, "to": "RUNNING", "reason": "步骤 wait"}], "next": 7}
```

**400** 缺 `job`；**404** 作业不存在。

---

## 3. 轮询建议

| 场景 | 接口 | 间隔 | 游标 |
|---|---|---|---|
| 作业列表/看板 | `/api/p2/jobs` | 3–5s | — |
| 单作业进度条 | `/api/p2/events?job=&since=` | 2s | 上次 `next` |
| 审计/成本流水 | `/api/p2/audit?since=` | 10s | 上次 `next` |
| 队列水位 | `/api/p2/queue` | 5s | — |

`status2.json`（每 10 分钟由 cron 刷新）也可直接读：
`https://<host>/files/<TOKEN>/f/reports/p2/status2.json`，适合做首屏快照。

---

## 4. 错误约定

| 码 | 含义 |
|---|---|
| 404 | token 不对 / 作业不存在 / 路径不存在（统一 HTML 错误页） |
| 400 | 参数缺失（如 `events` 缺 `job`） |
| 500 | 后端模块不可用（如 `tools/p2` 导入失败） |

---

## 5. 现状限制

- **只读**：提交、取消、重试仍走 CLI（`batchctl submit/run/cancel`）。要加写接口需先做鉴权升级与幂等令牌，留到 UI 二期。
- 数据源是 `queue.db` + `jobs/<id>/{job,manifest,events}`，`status2.json` 只作快照。
- 无分页（列表按 `enqueued_at` 全量返回）；作业量过百再加 `offset/limit`。
- 无 WebSocket / SSE；轮询足够（作业级更新频率 ~1–5s）。

---

## 6. P2.0 Registry API（新增，只读）

> 设计文档：`docs/P2.0-Workflow-Model-Registry-设计.md`
> 数据源：`registry/registry.db`（由 `registry/*.json` 派生，`p20ctl import` 重建）。

### 6.1 `GET /api/p2/registry`

```json
{"stats": {"workflows": 7, "models": 4,
           "compat": {"verified": 4, "equivalent": 2, "experimental": 1},
           "fingerprint": "8dc8...", "imported_at": "2026-11-01T21:26:00+0800",
           "schema_version": "1"},
 "workflows": 7, "models": 4}
```

### 6.2 `GET /api/p2/registry/workflows`

```json
{"total": 7, "workflows": [
  {"key": "qwen_image_t2i", "name": "Qwen-Image 文生图 1024×1024", "version": 1,
   "kind": "image", "model_key": "qwen_image",
   "graph_sha256": "16fd3e...",
   "compat": {"status": "verified", "basis": "real-run", "evidence": ["p1-..."]},
   "params": ["cfg", "height", "neg_prompt", "prompt", "seed", "steps", "width"],
   "nodes": 9}
]}
```

`compat.status`：`verified` / `equivalent` / `experimental` / `incompatible`。

### 6.3 `GET /api/p2/registry/workflows/<key>`

返回 `{"workflow": {...}, "compat": {...}}`。`workflow.params` 是完整参数契约（类型/默认/范围/绑定），`workflow.output` 是输出契约，`workflow.nodes` 是节点数。**404** = key 不存在。

### 6.4 `GET /api/p2/registry/models`

```json
{"total": 4, "models": [
  {"key": "qwen_image", "name": "...", "version": 1, "kind": "image", "family": "qwen",
   "files": {"unet": "qwen/qwen_image_fp8_e4m3fn_scaled.safetensors",
             "clip": "qwen25vl7b_std.safetensors", "vae": "qwen_std_vae.safetensors"}}
]}
```

### 6.5 `GET /api/p2/registry/models/<key>`

返回 `{"model": {...}}`：`files`（含 loader/input/match/extra）、`defaults`、`constraints`、`setup`、`primary`。

### 6.6 `GET /api/p2/registry/compat`

```json
{"matrix": [
  {"workflow": "qwen_image_t2i", "model": "qwen_image", "status": "verified",
   "basis": "real-run", "evidence": ["p1-..."], "checked_at": "2026-11-01", "notes": "..."},
  {"workflow": "h3_i2v_121f", "model": "h3", "status": "experimental",
   "basis": "unverified", "evidence": [], "notes": "..."}
]}
```

### 6.7 `GET /api/p2/registry/resolve`

| 参数 | 必填 | 说明 |
|---|---|---|
| `workflow` | ✅ | 工作流 key |
| `model` | — | 默认取工作流配置的 `model.key` |
| `params` | — | URL-encoded JSON，如 `{"prompt":"a cat","seed":7}` |
| `allow_experimental` | — | `1/true` 放行 experimental |
| `graph` | — | `1/true` 返回完整渲染图（默认只返回摘要） |

响应含 `params`（补全默认值后）、`defaults_applied`、`expect`、`render`（graph_sha256/nodes/prefix）；`400` = 参数/兼容性/渲染错误。

### 6.8 轮询建议

Registry 配置只在 `p20ctl import` 后变化，UI 首屏拉一次即可；`workflows/models` 可 30–60s 刷新一次。作业进度仍用 `/api/p2/jobs` + `/api/p2/events`。

### 6.9 限制

- **只读**：导入/兼容性写入仍走 `p20ctl import` / `p20ctl compat verify`；写 API 需要幂等令牌与权限升级，未实现。
- `resolve` 不落盘、不建作业；要提交请走 CLI（`p20ctl new`）。
- 首次访问若 `registry.db` 不存在或配置指纹变化，会自动 `import_all()`（毫秒级；7 工作流/4 模型）。

---

## 7. 图片控制台（P2.1 · M1）

页面与服务端渲染（`webui/`），token 与文件服务一致；路径前缀 `/{token}`。

| 页面 | 说明 |
|---|---|
| `/{token}/app`、`/{token}/app/create?workflow=<key>` | 创作台：工作流/参数表单、提交、轮询、结果网格 |
| `/{token}/app/jobs?state=&q=` | 任务列表（状态/关键词筛选） |
| `/{token}/app/jobs/<job_id>` | 任务详情：状态时间线、产物、参数快照 |
| `/{token}/static/app.css`、`/static/app.js` | 控制台静态资源 |
| `/{token}/app/gallery`、`/app/registry` | M3/M4 交付，当前为占位页 |

### 7.1 `GET /api/p2/jobs`

| 参数 | 说明 |
|---|---|
| `limit` | 默认 20，上限 200 |
| `state` | 精确状态，或 `FAILED` 前缀匹配 `FAILED_*` |
| `workflow` | 工作流 key |
| `q` | job_id / prompt 关键词 |

响应：`{items:[{job_id,state,workflow,workflow_version,model,created_at,artifacts}], count}`。

### 7.2 `POST /api/p2/jobs`（唯一写接口）

```json
{"workflow": "qwen_image_t2i",
 "params": {"prompt": "a red apple", "seed": 7, "batch_size": 2},
 "run": false}
```

- 服务端用 `registry.resolve` 补默认值并校验（含兼容性）；`experimental` 组合返回 **409**。
- 生成 spec → `jobctl new` → 返回 `201 {job_id,state,workflow_version,params_hash,params,expect}`。
- `run:true` 时后台起 `run.py <job_id> --no-keep`（日志写 `reports/p1/jobs/<id>/run.log`）；默认 `false`，不花钱。
- `batch_size=2` → `expect.images=2`，fetch/verify 按张数校验。

### 7.3 `GET /api/p2/jobs/<job_id>`

响应 `{job, artifacts:[{name,bytes,url,published}], workflow, terminal}`；`url` 指向 nginx 静态路径 `/pub/jobs/<job_id>/<file>`（作业终态后由 `scripts/publish_artifacts.py` 复制，页面轮询会触发），**不依赖 fileserver 进程是否存活**。`404` 作业不存在，`400` job_id 非法。

### 7.4 `POST /api/p2/jobs/<job_id>/cancel`

只允许取消 `CREATED`（改为 `ABORTED`，`state_seq` +1）；已在执行返回 **409**，用 `run.py --abort`。

### 7.5 限制

- 写接口只做"建作业/取消"，**不暴露**改作业、删产物、改 registry。
- 上传（图生图/蒙版）在 M2/M3 提供 `POST /api/p2/uploads`。
- 页面无构建链：改 `webui/assets.py` 即生效，无需打包。

### 7.6 `POST /api/p2/uploads`（M2）

multipart，字段名 `file`。校验 magic number（PNG/JPEG/WebP）、上限 20MB，按内容哈希命名
`in-<sha8>.<ext>`，同时写 `reports/p2/p21/inputs/`（作业用）与 `share/inputs/`（`/f/inputs/...` 预览）。
返回 `201 {name, bytes, url}`；非图片 415，超限 413。

### 7.7 `GET /api/p2/jobs/zip?ids=a,b,c`

把选中作业的产物打成 zip（内存中，优先取 `/pub/jobs/` 已发布副本），返回 `application/zip`。
非法/不存在 id 直接跳过；一个都没有时 404。

### 7.8 图生图（M2）

创作台切换 `?workflow=i2i`：上传原图 → `POST /api/p2/uploads` → 提交时 `params.image=in-<sha8>.png`；
`denoise` 0.05–1.0；结果区出现「原图 / 结果」对比滑杆（原图走 `/f/inputs/<name>`）。
