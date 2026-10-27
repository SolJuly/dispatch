# 测试 2：快速销毁（rapid destroy）

> 生成时间：2026-10-27 09:06:23（Asia/Shanghai）  
> 测试：test_rapid_destroy  
> 卡型：V100S  
> 自动关机兜底：1.0h  
> 模式：真实执行  

---

## 一、结论

| 检查项 | 结果 |
|---|---|
| A. 创建后立即 stop | 被拒/失败（rc=1） |
| B. 创建后立即 rm | 成功（rc=0） |
| 无残留实例 | ✅ |
| 本测试花费 | ¥0.017 |
| 总耗时 | 121s |

## 二、场景 A：创建后立即关机

| 项 | 值 |
|---|---|
| 实例 | `uhost-1wyurjqu7srz`（test-rapid-stop） |
| 动手瞬间的状态 | Initializing |
| stop rc | 1 |
| stop 耗时 | 3.7s |
| up 进程退出码 | 0 |
| 关机后状态 | Running |

**stop 输出**

```text
关机 uhost-1wyurjqu7srz …（关机后 GPU/CPU/内存停收，仅保留磁盘）
2026-10-27 09:04:34,439 [ WARNING] [3156bec7-3f68-4fef-8897-a8d8d0286bff] StopCompShareInstance - 8903: UHost is in the task, please try again later (_client.py:87 ucloud)
错误: [3156bec7-3f68-4fef-8897-a8d8d0286bff] StopCompShareInstance - 8903: UHost is in the task, please try again later
```

## 三、场景 B：创建后立即删除

| 项 | 值 |
|---|---|
| 实例 | `uhost-1wyutc7jvptb`（test-rapid-rm） |
| 动手瞬间的状态 | Initializing |
| 尝试次数 | 2 |
| 最终结果 | 成功 |
| 实例是否消失 | True（等了 0.4s） |
| up 进程退出码 | 1 |

**每次 rm 的返回**

| 第几次 | rc | 耗时 | 输出摘要 |
|---|---|---|---|
| 1 | 1 | 2.5s | 2026-10-27 09:05:58,931 [ WARNING] [873dd66e-6689-424e-8675-ada9fb5521a4] TerminateCompShareInstance - 8903: uhost is in the task, please try again later (_clie |
| 2 | 0 | 6.2s | 已提交删除 uhost-1wyutc7jvptb（本周期约 ¥0.00 已记账） |

## 四、残留检查

`csctl ls` 中 `test-rapid-*` 实例：**0 台 ✅**

## 五、费用

| 实例 | 名称 | 运行小时 | 花费 |
|---|---|---|---|
| `uhost-1wyutc7jvptb` | test-rapid-rm | 0.000 | ¥0.000 |
| `uhost-1wyurjqu7srz` | test-rapid-stop | 0.012 | ¥0.017 |

本测试合计 **¥0.017**

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
$ python3 tests/test_rapid_destroy.py --dry-run
==================================================================
test_rapid_destroy — dry-run 计划
==================================================================
场景 A：创建后立即关机
  python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-rapid-stop   (后台启动)
  # 一旦 stdout 出现『实例已创建: uhost-xxx』，立刻：
  python3 csctl.py stop <uid>
  # 预期：可能被拒（实例仍在 Install），记录其 stderr 原文

场景 B：创建后立即删除
  python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-rapid-rm     (后台启动)
  python3 csctl.py rm <uid> --yes
  # 预期：可能撞『UHost is in the task』，脚本会重试 3 次（间隔 10 秒）

收尾：
  · 两个场景的实例一律 rm --yes 兜底清理
  · csctl ls 校验无 test-rapid-* 残留
  · 费用写入 reports/test_cost_summary.md

--dry-run：未调用任何云端接口，未创建实例。```

**退出码：0** ✅

### 3. 零触网证明（网络命名空间隔离）

用 `unshare -n` 剥离全部网络（无网卡、无 DNS、无路由）后重跑同一命令：

```
$ unshare -n python3 tests/test_rapid_destroy.py --dry-run
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
