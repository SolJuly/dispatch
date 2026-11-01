# P2 只读 API（UI 对接文档）

> 版本：`v0.3-p2-core` · 2026-11-01
> 全部 **GET / 只读 / JSON**，沿用文件服务的 token 鉴权；无写接口（提交/取消仍走 `batchctl`）。

---

## 0. 鉴权与基址

```
https://<host>/files/<TOKEN>/api/p2/...
```

`TOKEN` = 仓库根目录 `.share_token` 的值（当前 `23e00ec2`）。token 不对一律 `404`（不区分「无权限」和「不存在」）。

本机直连（调试用）：`http://127.0.0.1:8081/<TOKEN>/api/p2/...`

**CORS**：未开启。UI 需与本服务同源，或由 nginx 反代同域。

---

## 1. 接口清单

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/p2/queue` | 队列汇总 + 全部条目（轻量字段） |
| GET | `/api/p2/jobs` | 作业列表（含判定/复验/产物） |
| GET | `/api/p2/jobs/<job_id>` | 单作业详情 + 状态机事件 |
| GET | `/api/p2/audit?job=&since=&limit=` | 审计流水（id 游标，可增量轮询） |
| GET | `/api/p2/events?job=&since=` | 状态机事件（seq 游标，可增量轮询） |

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
