# 创作控制台 SPA

`/files/<token>/ui/` 的前端。**纯原生 HTML + JS + CSS，无框架、无构建链**——
`dist/` 里的三个文件就是源码本身，改完刷新即可生效。

---

## 为什么放 dist/

`fileserver.py` 的 `/ui/` 路由读 `UI_ROOT`（默认 `web/dist`）。目录名沿用
"构建产物"的习惯，但这里没有构建步骤：`index.html` / `app.js` / `app.css`
直接手改。将来真要上 Vite 时，`npm run build` 输出到这个目录即可，路由不用改。

> `.gitignore` 里根目录的 `dist/` 已锚定成 `/dist/`。**不要改回不带斜杠的 `dist/`**，
> 否则会连带忽略 `web/dist` —— 那是手写源码，不是构建产物。

---

## 开发流程（重要）

部署树 `/root/智云视频工作流` 同时是 **main 分支**和**线上服务的工作目录**。
`scripts/repo-guard.sh` 有三道硬性检查：必须是主工作树、必须在 main、工作区必须干净。
所以「边改边发布」是不可能的，按改动规模选路线：

### 小改：main 上直接改

适用：改颜色 / 字号 / 间距 / 文案、修单个函数、加一两个按钮。

```bash
cd /root/智云视频工作流          # 部署树 = main
vim web/dist/app.css

# 改完立刻验证，别让线上停在半成品
python3 -m unittest discover -s tools/webui/tests -t .
git commit -am "style(ui): ..."
bash scripts/deploy.sh
```

**注意**：`cs-files` 直接读 `web/dist/`，**文件一保存就会生效**（浏览器刷新即可见）。
所以改到一半时线上就是半成品。小改无所谓，改三分钟就完了。

### 大改：开分支 + worktree

适用：新增页面、重构布局、动数据流、换交互逻辑。

```bash
# 1. 建独立工作树（和部署树物理隔离，互不影响）
cd /root/智云视频工作流
git worktree add /root/wt-ui -b feature/ui-<主题>

# 2. 在 worktree 里改，随便改、随便提交
cd /root/wt-ui
vim web/dist/app.js
git commit -am "feat(ui): ..."

# 3. 预览：另起端口，完全不碰线上
cd /root/wt-ui
UI_ROOT=/root/wt-ui/web/dist \
SHARE_PORT=8082 SHARE_BIND=127.0.0.1 \
SHARE_ROOT=/root/智云视频工作流/share \
python3 fileserver.py
# 本机访问 http://127.0.0.1:8082/{TOKEN}/ui/

# 4. 满意了才合并发布
cd /root/智云视频工作流
git merge --no-ff feature/ui-<主题>
bash scripts/deploy.sh

# 5. 收尾
git worktree remove /root/wt-ui
git branch -d feature/ui-<主题>
```

**为什么用 worktree 而不是 `git checkout -b`**：部署树只有一个工作目录。
在上面切分支，线上服务会跟着切走（`repo-guard` 会拦发布，但服务本身读的是磁盘文件）。
worktree 让两条线各占一个目录，物理隔离。

### 平板预览临时改动

上面的预览端口只绑 `127.0.0.1`，平板访问不到。想用平板看，两个办法：

1. **小改直接上 main**（改完 deploy，刷新平板）；
2. 预览端口改成 `SHARE_BIND=0.0.0.0`，用 `http://<内网IP>:8082/...` 访问
   —— **只在受信网络下用**，它没有 nginx 那层 basic auth。

---

## 发布前必须跑的测试

```bash
# 只改前端
python3 -m unittest discover -s tools/webui/tests -t .

# 动了后端（webui/ 或 fileserver.py 或 tools/）
for d in tools/p20/tests tools/p1/tests tools/webui/tests tools/p2/tests; do
  printf "%-22s " "$d"
  python3 -m unittest discover -s $d -t . 2>&1 | grep -E "^Ran |^OK$|^FAILED" | tr '\n' ' '; echo
done
```

当前基线：**588 条**（p20 105 / p1 84 / webui 162 / p2 237）。

---

## 回滚

```bash
# 看有哪些回滚点
git tag -l

# 整体回到某个 tag
git reset --hard post-merge-audit-20261108
python3 -m tools.p20.p20ctl import      # registry.db 是派生索引，必须重导
bash scripts/deploy.sh

# 只回滚前端文件，不动其它
git checkout b32740c -- web/
systemctl restart cs-files
```

---

## 后端依赖与降级

| 接口 | 主管道 | 缺失时表现 |
|---|---|---|
| `/api/p2/{version,jobs,registry,uploads,queue,audit,events}` | ✅ 有 | — |
| `/api/p2/{status,presets,share/<id>}` | ❌ 契约里有、代码没有 | 相关 UI 不显示 |
| `/api/p2/instances` | ❌ 无 | 运行环境页显示「接口不可用」说明，不报错 |
| `/api/p2/dispatch` | ❌ 无 | 调度条自动隐藏 |

缺失接口都有 try/catch 兜底，页面不会白屏。

**实例页的完整实现曾存在于已删除的 `feature/ui` 分支**（`tools/p2/instances.py` 395 行
+ `submit.py` 569 行 + 368 行测试），2026-11-08 清理分支时已归档到
`/root/wt-ui-salvage-20261108/`。要用就把它们搬进 `tools/p2/` 并接线。

---

## CSP

`fileserver.py` 的 `UI_CSP` 必须放行 `script-src 'self'`。
**内联 `<script>` 会被拦截**（CSP 没开 `'unsafe-inline'`），调试代码一律放外部 .js。

`/f/*.html` 是另一套更严的策略（`default-src 'none'`），因为那是用户可写的共享目录。
两条路由的 CSP 不要混用 —— 混了要么 SPA 白屏，要么共享目录能跑脚本窃 token。

---

## 提示条

用 `toast(msg, kind)`，`kind` 取 `ok` / `warn` / `err`。节点 `#toast` 挂在 `body`
末尾，**任何页面都能用**。

> 历史教训：曾有代码在历史页写 `$('#submitMsg')`，而那个元素只在创作台存在 →
> `null.innerHTML` 抛 TypeError → 后续 `tick()` 和 `finally` 全被跳过，
> 删除实际成功了但界面毫无反应。
> **跨页面反馈一律走 toast，不要借某个具体页面里的元素。**

---

## 已知约定

- 工作流分组：视频在前、图片在后；组内 `compat.status === 'verified'` 在前，
  未开发的收进 `<details>` 折叠区。判据用 `compat.status`，**不要用名字里有没有「尚未说明」**。
- 产物 URL：本地存在的产物走 `/f/reports/p1/jobs/<id>/artifacts/<name>`，
  只有发布副本时才走 `/pub/jobs/<id>/<name>`。写死 `/pub/` 会 404（`<video>` 还会
  反复重试，表现为「与后端失联」）。
- 删除作业：默认进 `jobs/.trash/`（可恢复），`purge=True` 才彻底删。
  `CREATED`/`RUNNING` 等主链状态后端会拒绝，需 `force=True`。
  `FAILED_PROVISION` 等失败分支视为可删（它们不会自己继续了）。
