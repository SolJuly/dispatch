# 创作控制台 SPA

`/files/<token>/ui/` 的前端。**纯原生 HTML + JS + CSS，无框架、无构建链**——
`dist/` 里的三个文件就是源码本身，改完刷新即可生效。

## 为什么放 dist/

`fileserver.py` 的 `/ui/` 路由读 `UI_ROOT`（默认 `web/dist`）。目录名沿用
"构建产物"的习惯，但这里没有构建步骤：`index.html` / `app.js` / `app.css`
直接手改。将来真要上 Vite 时，`npm run build` 输出到这个目录即可，路由不用改。

## 来源

来自分支 `feature/ui` 的 `reports/p2/ui/`（2026-11-01 原型，之后独立演进）。
搬过来时做了两处修复，见下节。

## 已修

1. **创作台永远显示「连接中…」**：`refresh()` 在 `view==='create'` 时直接 return，
   而 `markSync()` 只在函数末尾调用 → 状态机永远停在初始态。改为在该分支探活
   `/api/p2/version` 后调 `markSync()`；同时 `route()` 的 create 分支补一次 `tick()`，
   否则轮询调度根本不会启动。

## 后端依赖与降级

| 接口 | 主管道 | 缺失时表现 |
|---|---|---|
| `/api/p2/{version,jobs,registry,uploads,queue}` | ✅ 有 | — |
| `/api/p2/instances` | ❌ 无 | 运行环境页显示「接口不可用」说明，不报错 |
| `/api/p2/dispatch` | ❌ 无 | 调度条自动隐藏 |

两个缺失接口都有 try/catch 兜底，页面不会白屏。

## CSP

`fileserver.py` 的 `UI_CSP` 必须放行 `script-src 'self'`。
**注意：内联 `<script>` 会被拦截**（CSP 没有开 `'unsafe-inline'`），调试代码请放外部 .js。
