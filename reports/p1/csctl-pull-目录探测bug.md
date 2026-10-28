# csctl pull 目录探测 bug：定位、根因与修复方案

> 调查：2026-10-28 23:16–23:30 · 纯静态分析 + 代码级复现 · **¥0**（未建机）
> 结论：**根因是 `Transfer.ssh()` 不捕获 `TimeoutExpired`，且 `probe()`/`_classify()` 都在 `pull()` 的 try 之外。**
> 修复范围小（约 10 行），但涉及主链 `tools/transfer.py`，**等批准后再改**。

---

## 1. 现象

实际发生过一次（2026-10-28 21:04，从矩阵实验拉产物时）：

```
subprocess.TimeoutExpired: Command '['sshpass', '-p', '...', 'ssh', '-p', '23',
  '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
  '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-c', 'aes128-gcm@openssh.com',
  'root@117.50.214.120',
  'if [ -d /root/matrix_out/C/C.mp4 ]; then echo DIR; else echo FILE; fi']'
  timed out after 30 seconds
```

表现：`csctl pull` 整体崩溃，没有降级、没有重试。
当时用 `scp` 直取绕过，P1 的 `step_fetch` 也改成了 scp。

---

## 2. 定位

### 2.1 出问题的命令

`tools/transfer.py:344`，在 `Transfer._classify()` 里：

```python
rc, out = self.ssh(f"if [ -d {remote} ]; then echo DIR; else echo FILE; fi", timeout=30)
if "DIR" in out:
    ...  # 按目录处理
return "file", [posixpath.basename(remote)], posixpath.dirname(remote)
```

这条命令本身极轻（一次 `test -d`），正常 <1 秒。

### 2.2 异常从哪冒出来

`tools/transfer.py:92`：

```python
def ssh(self, cmd: str, timeout: int = 120) -> tuple[int, str]:
    r = subprocess.run(self._ssh_base() + [cmd], capture_output=True, text=True,
                       timeout=timeout)          # ← 超时抛 TimeoutExpired，无 try
    return r.returncode, (r.stdout or "") + (("ERR:" + r.stderr) if r.returncode else "")
```

**没有 `try/except subprocess.TimeoutExpired`**。

### 2.3 调用链上有没有兜底

`Transfer.pull()`（第 154 行起）：

```python
def pull(self, remote, local_dir, mode="auto"):
    cap = self.probe()                              # 第 3 行，try 之外
    ...
    kind, items, base = self._classify(remote)      # 第 6 行，try 之外
    ...
        try:                                        # 第 21 行才开始 try
            ...
        except Exception:                           # 第 23 行
```

**`probe()` 和 `_classify()` 都在 try 之外**，所以两处都可能把 `TimeoutExpired` 抛到 `cmd_pull`，最后冒到顶层。

### 2.4 代码级复现（不连实例）

```python
# monkeypatch subprocess.run 让它抛 TimeoutExpired
rc, out = t.ssh("if [ -d /tmp/x ]; then echo DIR; else echo FILE; fi", timeout=30)
→ ✗ 未捕获，直接抛出 TimeoutExpired（timeout=30s）
```

同时检查了调用链：

```
Transfer._classify   含 except: False
Transfer.pull        含 except: True   （但只覆盖第 21 行之后）
```

---

## 3. 根因

三层叠加，缺一层都不会崩：

| 层 | 问题 | 后果 |
|---|---|---|
| **1. 触发** | ssh 命令 30 秒超时 | 实例满载（ComfyUI 推理时 CPU 100%）时，ssh 握手+执行可能超 30s |
| **2. 无重试** | `ssh()` 一次超时即向上抛 | 瞬时抖动也会导致永久失败 |
| **3. 无兜底** | `ssh()` 不捕异常；`probe()`/`_classify()` 不在 `pull()` 的 try 内 | 异常直冲顶层，整个 `pull` 崩溃 |

**为什么偏偏是 `_classify`**：它是 `pull()` 里第一条 ssh 调用，且发生在"实例正忙"时最容易被拉起来的时刻。`probe()`（`for c in rsync tar gzip…`）同理。

**注意不是"目录探测逻辑写错了"**——`if [ -d ... ]` 本身没问题，路径拼接也正确。是**容错缺失**，不是逻辑错误。

---

## 4. 修复方案

### 方案 A：只给 ssh() 加超时捕获（最小，但不够）

```python
def ssh(self, cmd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(self._ssh_base() + [cmd], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT({timeout}s)"
    return r.returncode, ...
```

**问题**：`_classify` 拿到 `"TIMEOUT(30s)"` 不含 `DIR`，会当成 **FILE** 处理。如果远端其实是目录，后续会按单文件拉，行为错但不会崩。

### 方案 B：A + `_classify` 超时降级（推荐）

```python
def _classify(self, remote: str):
    ...
    rc, out = self.ssh(f"if [ -d {remote} ]; then echo DIR; else echo FILE; fi", timeout=60)
    if rc == 124:                       # 超时：保守按单文件处理，并告知
        print(f"    ⚠ 目录探测超时，按单文件处理: {remote}")
        return "file", [posixpath.basename(remote)], posixpath.dirname(remote)
    if "DIR" in out:
        ...
```

同时把探测超时从 30s 提到 60s（给满载实例留余量）。

### 方案 C：A + B + 重试

在 `ssh()` 里对超时做一次重试（间隔 3s）。改动最大，但最稳。

### 对比

| 方案 | 改动行数 | 风险 | 效果 |
|---|---|---|---|
| A | ~4 | 低，但可能误判目录为文件 | 不崩，但行为可能错 |
| **B** | **~10** | **低** | **不崩 + 明确降级 + 超时放宽** |
| C | ~18 | 中（重试会拉长总耗时） | 最稳 |

---

## 5. 风险评估（如果要改）

| 项 | 评估 |
|---|---|
| 影响范围 | `Transfer.ssh()` 被 `probe`/`_classify`/`_remote_size`/`_same_size`/`publish` 等调用；行为从"抛异常"变"返回 124"，属于**更安全**的方向 |
| 兼容性 | 现有调用方都按 `(rc, out)` 使用，无人依赖异常（已逐个检查） |
| 是否影响 P1 | P1 的 `step_fetch` 用 scp 直取，不经过 `Transfer.pull`；改了不影响 P1 |
| 是否影响 csctl 其它命令 | `pull`/`push` 共用 `Transfer`，`push` 也会受益（不崩） |
| 回滚 | 单文件改动，`git checkout main -- tools/transfer.py` 即可 |
| 需要真机验证 | 是。至少跑一次"满载实例上 pull 目录 + pull 单文件" |

---

## 6. 建议

**推荐方案 B**，但**不在本轮改**——理由：

1. `csctl.py`/`transfer.py` 是主链文件，按约定先报你
2. P1 已用 scp 绕过，功能不受影响
3. 改完需要建机验证（满载场景难复现，可能得跑一次推理再 pull）

**如果要改**，建议单独开一个 commit：

```
fix(transfer): ssh 超时不崩溃，目录探测降级为单文件

- Transfer.ssh 捕获 TimeoutExpired，返回 (124, "TIMEOUT(Ns)")
- _classify 遇 124 时保守按单文件处理并打印警告
- 探测超时 30s → 60s（满载实例留余量）
- 验证：满载实例上 pull 目录 / pull 单文件各一次
```

---

## 7. 证据

```
reports/p1/logs/pull-bug-repro.txt    代码级复现输出（含调用链 except 检查）
tools/transfer.py:92                  ssh() 无异常捕获
tools/transfer.py:344                 _classify 的目录探测命令
tools/transfer.py:154-210             pull() 的 try 范围（第 21 行才开始）
```
