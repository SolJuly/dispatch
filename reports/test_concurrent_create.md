# 测试 1：并发创建（concurrent create）

> 生成时间：2026-10-27 09:11:56（Asia/Shanghai）  
> 测试：test_concurrent_create  
> 卡型：V100S  
> 并发数：3  
> 自动关机兜底：1.0h  
> 模式：真实执行  

---

## 一、结论

| 检查项 | 结果 |
|---|---|
| 首轮并发成功数 | 1/3 |
| 被 up.lock 挡住的 | 2 |
| 重试后全部创建 | ✅ |
| 全部进入 Running | ✅ |
| 名称与 ID 一一对应 | ✅ |
| 清理完成 | ✅ |
| 本测试花费 | ¥0.142 |
| 总耗时 | 326s |

## 二、首轮并发明细（同时发起）

| 实例名 | 启动 | 结束 | 耗时 | rc | UID | 结论 |
|---|---|---|---|---|---|---|
| test-concurrent-a | 09:06:30 | 09:06:30 | 0.3s | 1 | `-` | 被 up.lock 挡住 |
| test-concurrent-b | 09:06:30 | 09:07:46 | 76.0s | 0 | `uhost-1wyuub32uq7g` | 成功 |
| test-concurrent-c | 09:06:30 | 09:06:30 | 0.3s | 1 | `-` | 被 up.lock 挡住 |

### 并发锁的实际行为

```text
csctl up 通过 fcntl.flock(.compshare/up.lock, LOCK_EX|LOCK_NB) 做排他，
第二个并发的 up 会立刻失败并提示『另一个 up 正在运行』。
因此「并发创建」在 csctl 层是串行化的 —— 这是防重复下单的设计，不是 bug。
本机实测：3 个并发里 2 个被挡，1 个成功。
```

## 三、被挡实例的串行重试

| 实例名 | 重试 rc | UID | 耗时 | 结论 |
|---|---|---|---|---|
| test-concurrent-a | 0 | `uhost-1wyuvwjurbqk` | 145.0s | 成功 |
| test-concurrent-c | 0 | `uhost-1wyuz1l5c31r` | 77.6s | 成功 |

## 四、最终状态

| 实例名 | UID | 最终状态 | 达到 Running 耗时 |
|---|---|---|---|
| test-concurrent-b | `uhost-1wyuub32uq7g` | Running | 1.1s |
| test-concurrent-a | `uhost-1wyuvwjurbqk` | Running | 1.4s |
| test-concurrent-c | `uhost-1wyuz1l5c31r` | Running | 1.6s |

## 五、清理

| 实例名 | UID | 动作 | 结果 |
|---|---|---|---|
| test-concurrent-a | `uhost-1wyuvwjurbqk` | rm | rc=0 已消失=True |
| test-concurrent-b | `uhost-1wyuub32uq7g` | rm | rc=0 已消失=True |
| test-concurrent-c | `uhost-1wyuz1l5c31r` | rm | rc=0 已消失=True |

残留检查：`csctl ls` 中 `test-concurrent-*` 实例 **0 台 ✅**

## 六、费用

| 实例 | 名称 | 运行小时 | 花费 |
|---|---|---|---|
| `uhost-1wyuvwjurbqk` | test-concurrent-a | 0.027 | ¥0.038 |
| `uhost-1wyuub32uq7g` | test-concurrent-b | 0.067 | ¥0.094 |
| `uhost-1wyuz1l5c31r` | test-concurrent-c | 0.007 | ¥0.010 |

本测试合计 **¥0.142**

---

## 验收记录：--dry-run 验证

> 验证时间：2026-10-27 09:43:05（Asia/Shanghai）
> 验证目的：确认 dry-run 只打印操作计划、不调用任何云端接口、不产生任何副作用

### 1. 分支实现检查

| 检查项 | 结果 | 依据 |
|---|---|---|
| `--dry-run` 参数注册 | ✅ | `tests/_common.py` `parse_args()` 统一注册 |
| `main()` 提前返回 | ✅ | `if args.dry_run: return plan(...)`，位于所有 csctl 调用之前 |
| `plan()` 是否纯打印 | ✅ | 逐行审查：只 `print`，不构造 CS 对象、不读 API |
| `_common.csctl(dry_run=True)` | ✅ | 本地短路直接返回 `Result(skipped=True)`，不起子进程（双保险） |

### 2. 执行记录

```
$ python3 tests/test_concurrent_create.py --dry-run
==================================================================
test_concurrent_create — dry-run 计划
==================================================================
将并发发起以下命令（互不等待）：
  python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-concurrent-a
  python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-concurrent-b
  python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-concurrent-c

预期行为：
  · 第 1 个拿到 .compshare/up.lock，正常创建
  · 其余立即返回『另一个 up 正在运行』（rc≠0）
  · 脚本会对被挡住的实例串行重试，直至全部创建成功

随后将执行：
  · 逐个等待 Running（超时 15 分钟/台）
  · 校验名称与 ID 一一对应，无串号
  · 清理 3 台（csctl rm <id> --yes）并验证无残留
  · 费用写入 reports/test_cost_summary.md

--dry-run：未调用任何云端接口，未创建实例。```

**退出码：0** ✅

### 3. 零触网证明（网络命名空间隔离）

用 `unshare -n` 剥离全部网络（无网卡、无 DNS、无路由）后重跑同一命令：

```
$ unshare -n python3 tests/test_concurrent_create.py --dry-run
rc=0
$ diff <联网输出> <断网输出>   → 无差异（字节级完全一致）
```

**结论**：断网环境下输出与联网环境**字节级完全一致**且 rc=0，证明该路径**不存在任何网络调用**。

### 4. 零副作用（三点不变式）

| 指标 | 运行前 | 运行后 | 结论 |
|---|---|---|---|
| `.compshare/ledger.jsonl` 行数 | 985 | 985 | ✅ 无新增记账 |
| 实例数（`csctl ls`） | 0 | 0 | ✅ 无新实例创建 |
| 账户余额 | ¥50.82 | ¥50.82 | ✅ 无扣费 |

### 5. 小结

`--dry-run` **已实现、已验证、可安全执行**：打印完整操作计划、退出码 0、零触网、零副作用。
