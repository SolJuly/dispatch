# 协作与提交规矩（AGENTS.md）

> 本文件是**所有在这个目录工作的会话/代理必须遵守的硬约束**。
> 建立于 2026-11-12，起因：同日 22:08 某会话在未开分支的情况下，直接往 `main` 提交了 7 笔 OOM 代码。
> 仓库：`git@github.com:SolJuly/dispatch.git`（**私有**）

---

## 一、铁律（不可协商）

### 1. 禁止直接推送 main

```bash
git push origin main          # ❌ 禁止
git push                      # ❌ 在 main 上禁止
```

**main 只能通过 GitHub 上的 Pull Request 合并进入。** PR 由用户（SolJuly）在网页或手机上点 Merge。

### 2. 禁止 `git add -A` / `git add .`

```bash
git add -A                    # ❌ 会把别人的半成品一起提交
git add .                     # ❌ 同上
```

**必须显式列出文件**：

```bash
git add path/to/file1.py path/to/file2.md    # ✅
```

> 2026-11-12 22:08 的事故就是这样发生的：某会话 `git add -A`，把另一个会话刚改的 `.gitignore` 一起提交了。

### 3. 禁止破坏性操作

```bash
git reset --hard              # ❌ 会抹掉别人未提交的改动
git checkout -- .             # ❌ 同上
git clean -fd                 # ❌ 删除所有未跟踪文件
git branch -D <别人的分支>     # ❌
git push --force              # ❌ 除非用户明确要求且确认过
```

### 4. 禁止擅自切换主树分支

主工作树 `/root/智云视频工作流` 必须**始终**停在 `main`。

```bash
cd /root/智云视频工作流 && git checkout feat/xxx    # ❌ 禁止
```

开发请用 worktree（见第三节）。

---

## 二、标准流程

### 新功能 / 新架构 / 重构

```bash
# ① 用户说「开 feat/xxx 分支」后，代理执行：
cd /root/智云视频工作流
git worktree add ../zhiyun-xxx -b feat/xxx

# ② 在 worktree 里开发
cd /root/zhiyun-xxx
# ... 改代码 ...
git add <具体文件>
git commit -m "feat(xxx): 做了什么"

# ③ 推送分支（不是 main）
git push -u origin feat/xxx

# ④ 告诉用户去 GitHub 开 PR
#    https://github.com/SolJuly/dispatch/compare/main...feat/xxx

# ⑤ 用户在 GitHub 点 Merge 后，代理同步本地：
cd /root/智云视频工作流
git pull
git worktree remove ../zhiyun-xxx
git branch -d feat/xxx
```

### 极小改动（错别字、注释、参数微调）

如果用户明确说「直接推」或「不用开分支」：

```bash
git add <具体文件>
git commit -m "docs: ..."
git push origin main           # 仅此情况允许，且必须有用户明确指令
```

**没有用户明确指令时，一律走分支 + PR。**

---

## 三、worktree 用法

### 为什么

同一个 `.git` 下多个目录，各自 checkout 不同分支，互不干扰。

单目录的问题：全局只有一个「当前分支」，A 会话切分支会让 B 会话的文件全变；A 的 `git add -A` 会吞掉 B 的改动。

### 常用命令

| 操作 | 命令 |
|---|---|
| 新建 | `git worktree add ../zhiyun-xxx -b feat/xxx` |
| 查看 | `git worktree list` |
| 删除 | `git worktree remove ../zhiyun-xxx` |
| 强制删除（有未提交改动时） | `git worktree remove ../zhiyun-xxx --force` |

### 注意

- 同一分支不能同时检出两次（这是特性，不是 bug）
- `worktree remove` 遇到未提交改动会拒绝，`--force` 会丢弃
- 删除 worktree **不会**删除分支，需要另外 `git branch -d`

---

## 四、署名

已在 `.git/config` 配置好，所有会话共享：

```
user.name  = SolJuly
user.email = 310061311+SolJuly@users.noreply.github.com
```

**不要修改。** 历史上的 `ops <ops@local>` 和 `agent <agent@local>` 是 2026-11-12 之前留下的，按用户决定保持原样不改写。

---

## 五、安全红线

### 绝不提交的文件

`.gitignore` 已覆盖，但再次明确：

| 文件 | 内容 |
|---|---|
| `compshare/credentials.env` | 优云智算 AK/SK |
| `.compshare/` | 账本、锁、运行态 |
| `.share_token` | 文件窗口访问 token |
| `logs/` | 运行日志 |
| `share/` | 同步副本 |
| `*.mp4` `*.png` `*.jpg` | 大体积媒体 |

### 提交前自查

```bash
git status                   # 确认没有意外文件
git diff --cached --stat     # 确认暂存内容
```

发现敏感文件被暂存：

```bash
git restore --staged <file>  # 取消暂存
```

---

## 六、推送前必做

```bash
# 1. 确认分支不是 main
git rev-parse --abbrev-ref HEAD

# 2. 确认工作区干净
git status --porcelain

# 3. 确认远程状态
git fetch origin && git log --oneline origin/main..HEAD
```

---

## 七、出问题时的处理

| 症状 | 原因 | 处理 |
|---|---|---|
| `! [rejected] main -> main (fetch first)` | 远程有新提交 | `git pull --rebase` 后重试 |
| `protected branch hook declined` | 分支保护生效 | 说明改走 PR，不要绕过 |
| 提交了不该提交的文件 | 手滑 | `git restore --staged <file>`，如果已 commit 则 `git reset --soft HEAD~1` |
| 误删了未提交改动 | `reset --hard` 等 | `git reflog` 找回（仅限已 commit 的） |

---

## 八、当前仓库状态（2026-11-12 23:50）

```
分支      main（= origin/main，98c2498）
          feat/l5-l6-completion（已推送，已合并过）
工作树    1 个（主树）
工作区    干净
测试      1023 项全绿
实例      0 台
余额      ¥19.79
```

---

## 九、待办（按优先级）

| 优先级 | 事项 | 说明 |
|---|---|---|
| P1 | 接第二家云（AutoDL） | L5 命门，契约/Fake/测试全就位，约 200 行 |
| P2 | 写 `tools/p2/placement.py` | 依赖 P1 |
| P3 | 决定镜像路线 | `reports/p20/镜像路线四选一-待决.md`，影响 ¥16.5/月 |
| P4 | 处理 U2（8903 重试） | 唯一会烧钱的风险，见 `reports/unknowns.md` |
| P5 | 清理 `p2-dispatch` 死配置 | 防误启用，见其 unit 文件顶部警告 |
| P6 | 给 P2.1/P2.2 补测试 | 目前 0 覆盖 |
