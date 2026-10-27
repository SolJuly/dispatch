# 测试 3：状态过渡期查询（state transitions）

> 生成时间：2026-10-27 09:04:15（Asia/Shanghai）  
> 测试：test_state_transitions  
> 卡型：V100S  
> 观察时长：30s  
> 采样间隔：2.0s  
> 模式：真实执行  

---

## 一、结论

| 检查项 | 结果 |
|---|---|
| 采样点数 | 11 |
| 观察到的状态 | `Initializing` |
| 状态跳转路径 | Initializing |
| 未文档化状态 | `Initializing` |
| 无残留实例 | ✅ |
| 脚本判定 | 通过 ✅ |
| 本测试花费 | ¥0.000 |
| 总耗时 | 52s |

## 二、状态序列（逐采样）

| 时刻(s) | State | 中文 | 是否文档化 |
|---|---|---|---|
| 4.8 | `Initializing` | 未文档化 | **未文档化** |
| 7.2 | `Initializing` | 未文档化 | **未文档化** |
| 9.5 | `Initializing` | 未文档化 | **未文档化** |
| 12.2 | `Initializing` | 未文档化 | **未文档化** |
| 14.7 | `Initializing` | 未文档化 | **未文档化** |
| 17.4 | `Initializing` | 未文档化 | **未文档化** |
| 19.9 | `Initializing` | 未文档化 | **未文档化** |
| 22.3 | `Initializing` | 未文档化 | **未文档化** |
| 25.1 | `Initializing` | 未文档化 | **未文档化** |
| 27.4 | `Initializing` | 未文档化 | **未文档化** |
| 29.8 | `Initializing` | 未文档化 | **未文档化** |

## 三、状态驻留时长

| State | 中文 | 累计驻留(s) |
|---|---|---|
| `Initializing` | 未文档化 | 47.0 |

## 四、与文档枚举的比对

`compshare/cs.py` 的 `STATE_CN` 定义了以下状态：

| State | 中文 |
|---|---|
| `Running` | 运行中 |
| `Stopped` | 已关机 |
| `Starting` | 启动中 |
| `Stopping` | 关机中 |
| `Install` | 安装/部署中 |
| `Rebooting` | 重启中 |
| `Interrupted` | 已中断 |
| `Terminating` | 删除中 |
| `Unknown` | 未知 |

**本次观察到的**：`Initializing`

**未文档化的状态**：`Initializing`

> 这些状态值未出现在 `STATE_CN` 里，`csctl ls` 会把它显示成原始英文。建议补进映射表，否则状态列会出现英文或 `None`。

## 五、清理与费用

`csctl ls` 中 `test-state-*` 残留：**0 台 ✅**

| 实例 | 名称 | 运行小时 | 花费 |
|---|---|---|---|
| `uhost-1wyuqafmju4o` | test-state-tx | 0.000 | ¥0.000 |

本测试合计 **¥0.000**

> 费用口径：以 `.compshare/ledger.jsonl` 的 terminate 记账为准。若实例存活时间极短（< 1 分钟）且 API 的 `StartTime` 尚未回填，`csctl rm` 会按 `run_h=0` 记账，此时显示 ¥0.00 属正常，实际扣费以账单为准（通常延迟数分钟）。

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
$ python3 tests/test_state_transitions.py --dry-run
==================================================================
test_state_transitions — dry-run 计划
==================================================================
1. 后台创建实例 test-state-tx
   python3 csctl.py up --gpu-type V100S --hours 1.0 --name test-state-tx

2. 抓到『实例已创建: uhost-xxx』后立即开始观察
   每 2.0s 查询一次状态，共 30s（约 15 个采样点）
   查询方式：只读调用 ComfyUI 无关的实例列表 API（不触碰实例内部）

3. 记录并分析
   · 状态序列（采样时刻 / State / 中文）
   · 各状态驻留时长
   · 状态跳转路径
   · 与 STATE_CN 的 9 个文档化状态比对，标记未文档化者

4. 收尾：rm --yes 删除 test-state-tx，校验无 test-state-* 残留

--dry-run：未调用任何云端接口，未创建实例。```

**退出码：0** ✅

### 3. 零触网证明（网络命名空间隔离）

用 `unshare -n` 剥离全部网络（无网卡、无 DNS、无路由）后重跑同一命令：

```
$ unshare -n python3 tests/test_state_transitions.py --dry-run
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
