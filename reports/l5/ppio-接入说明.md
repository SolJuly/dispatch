# PPIO 派欧云 Provider 接入 · 说明与验证

> 分支：`feat/l5-ppio` · 工作树 `/root/zhiyun-l5-ppio` · 建于 `main @ 0c677e1`
> 日期：2026-11-13
> 相关：`reports/l5/国内GPU平台选型-20261113.md`（为什么选这家）、
> `reports/l5/autodl-api-notes.md`（AutoDL 那条线为什么停）

---

## 一句话

`tools/p2/providers/ppio.py` 实现全部 16 个契约方法，`get_provider("ppio")` 可用，
`l5l6 providers` 能力矩阵里能看到，**测试 1070 项全绿（基线 1002 + 新增 68）**。
**全程零网络、零费用**：测试用注入 transport 复放 fixture，没有一次真实请求。

---

## 一、改了什么

| 文件 | 行数 | 性质 |
|---|---|---|
| `tools/p2/providers/ppio.py` | 852 | 新增：PPIO 适配器 |
| `tools/p2/tests/test_ppio.py` | 769 | 新增：68 项离线测试 |
| `tools/p2/providers/base.py` | +15 | 契约新增 `Provider.ready()` |
| `tools/p2/providers/registry.py` | +42/−11 | 注册 ppio；拆开 `available()` 与 `capability_matrix()` |
| `tools/p2/tests/test_l5_providers.py` | +6/−1 | ppio 加入共享契约测试 |

**没有动 `compshare.py`，没有动 `cs.py`，没有动任何上层**（`run.py` / `scheduler.py` / `reaper.py` 一行没改）。

---

## 二、三个设计决定（每个都在代码里有注释，这里说清为什么）

### 1. `capabilities()` 与 `available()` 被拆开了

接 PPIO 时撞到一个真问题：**能力声明跟凭证无关，能不能调才跟凭证有关**。

原来 `available()` 的判据是「构造不抛异常」。PPIO 的 API key 是必需项，于是出现两难：

- 构造期抛 → `capability_matrix()` 遍历 `available()`，**矩阵里整行消失**，
  文档里就看不到「PPIO 支持 OCI / 130GB 免费盘 / 竞价」，而这恰恰是选型最该看的
- 构造期不抛 → `available()` 把一家必然 401 的云列进去，`cost_snapshot`
  之类的巡检工具每次去查都撞墙

解法是在契约上多一个钩子：

```python
class Provider:
    def ready(self) -> None:
        """本机现在能不能真的调这家云。默认「能」。"""
        return None
```

```python
available()          → get_provider(n).ready() 不抛         # 「本机真能调」
capability_matrix()  → 遍历 _BUILTIN，额外标 ready: bool     # 「这家能做什么」
```

结果：`l5l6 providers` 现在四行都在，没配凭证时 PPIO 也显示，但矩阵 JSON 里
`ready: false` —— 一眼能分出「这家支持」和「这台机器配好了」。

### 2. 金额用响应里的精度，绝不硬编码 1000

PPIO 的价格是 `{"precision": 3, "final_price": 12600}` = ¥12.600/时。
`_money(raw, precision)` 从响应取精度。测试钉了 3 个精度档（2 / 3 / 4）都算对。

`balance()` 是另一套单位：`availableBalance` 是 **1/10000 元**（10000 = ¥1），
不是分也不是毫。写错就是 100 倍的账。

### 3. 磁盘按天，**不**折算成小时

```python
"hourly_instance": 1.98,     # 算力，元/小时（真值）
"hourly_disk": 0.0,          # 恒 0
"hourly_total": 1.98,        # 不含磁盘
"daily_disk": 0.0,           # 磁盘，元/天，超出 130GB 的部分
```

PPIO 系统盘是「超出 130GB 后 ¥0.01/GB/天，计费时长 = 创建→释放」。
把它除 24 折成 `hourly_disk` 会让跨日边界必错 —— 与 `base.py` 顶部
「不隐藏价格口径」是同一条理由。测试 `test_disk_as_daily_not_hourly` 钉住这条。

---

## 三、能力声明（逐项给依据）

```
ppio  停机=✓ 外网=✓ OCI=✓ 共享权重=✗ 定时关机=✗ 竞价=✓ 自定义镜像=✓ 免费系统盘=130GB
```

| 项 | 值 | 依据 |
|---|---|---|
| `can_stop_instance` | True | `PUT /gpus/v2/instances/{id}/stop` |
| `bills_disk_when_stopped` | **True** | 系统盘 130GB 内 0 元；超出 0.01 元/GB/天，**计费时长是「创建→释放」，停机也收** |
| `instance_has_internet` | True | 有；且 github/huggingface/pypi/docker 有加速 → `provision.sh` 能跑 |
| `can_pull_oci` | True | create 直接收 `image` 地址 + `registry_auth_id` |
| `has_shared_weight_mount` | False | 没有智云 `/model` 那种共享权重库 |
| `supports_scheduled_stop` | **False** | v2 全部 40 个 spec 里没有这个端点 |
| `supports_spot` | True | 5 折，1 小时保护期，回收前 1 小时通知，可读 `spot_reclaim_time` |
| `supports_custom_image` | True | `POST /gpus/v2/jobs/save-image` |
| `free_system_disk_gb` | 130 | 官方计费页 |
| `typical_boot_s` | **None** | 真机没测过，不猜。`notes["typical_boot_s"]` 写了理由 |

验收标准第 6 条「没有未说明的 None」由测试
`test_capabilities_has_no_unexplained_none` 强制：任何 None 必须在 `notes` 里有解释。

---

## 四、16 个方法怎么实现的

| 方法 | 接口 | 关键处理 |
|---|---|---|
| `capabilities` | — | 见上 |
| `create` | `POST /gpus/v2/instances` | 先拉 products 选 SKU；`data_disk_gb>0` 抛 UNSUPPORTED；`idempotent=False` |
| `get` | `GET /gpus/v2/instances/{id}` | `product_id` 翻成展示名；未知状态不抛 |
| `list_instances` | `GET /gpus/v2/instances` | 游标翻页（最多 50 页防死循环）；`prefix` 本地过滤 |
| `start` | `PUT .../start` | 失败统一 `retryable=True`（资源被抢占要能换机） |
| `stop` | `PUT .../stop` | — |
| `terminate` | `DELETE /gpus/v2/instances/{id}` | 不要求先关机（与 AutoDL 不同） |
| `ssh_endpoint` | `GET .../{id}` → `tools.ssh` | 解析 `ssh -p PORT user@host`；**每次重取不缓存** |
| `ssh_ready` | `ssh_endpoint` + TCP/ssh 探活 | 端口变了下轮重取；`running ≠ sshd 就绪` |
| `scheduled_stop` | — | 抛 UNSUPPORTED，报错里指明降到 `reaper` |
| `price` | `GET /gpus/v2/products` | 见上「磁盘按天」；该计费方式不可用抛 UNSUPPORTED |
| `balance` | `GET /openapi/v1/billing/balance/detail` | `/10000` |
| `resolve_image` | 地址原样过 / `GET /gpus/v2/images` | 三类镜像（base/community/private）逐个查 |
| `images` | `GET /gpus/v2/images` | 三类合并 **+ 按 image_id 去重** |
| `stock` | `GET /gpus/v2/products` | `resource_spec.gpu.available`；找不到型号抛 NOT_FOUND |

---

## 五、五处刻意「不猜」的地方

这些是写代码时最想偷懒、但偷懒就会出事的点：

### 1. `4090` 与 `4090D` 不能互相污染

`4090` 是 `4090D` 的子串。只做 `in` 判断会让「要 4090」变成「按 4090D 建机」——
两者都是 24G，但 4090D 算力约 4090 的 8 折，**报价和 OOM 升卡排序都会错**。

`_match()` 分三档：型号名精确 → id/name 精确 → 子串（取最短）。
测试 `test_4090_never_matches_4090d` 双向钉住。

### 2. `stock()` 找不到型号抛异常，不返回 0

返回 0 会被上层读成「售罄」→ 决策变成「换张卡」，而真相是「型号名写错了」。
这两种情况的正确动作完全不同（AutoDL 那份笔记里记过同类的坑：库存语义对不上比没有库存更糟）。

### 3. `price()` 遇到不支持的计费方式抛 UNSUPPORTED，不返回 0

3090 没有 Spot。返回 0 会被读成「免费」，那是灾难。

### 4. `create` / `start` 的重试语义

- `create` **不幂等**：重试一次可能租出两台卡 → 所有失败 `retryable=False`
  （连「没货」也是，上层该换卡而不是盲目再来一次）
- `start` **可以安全重试**：对已在运行的实例再开一次只会报错，不会多租卡
  → 失败统一 `retryable=True`

测试 `test_create_failure_is_not_retryable` /
`test_create_out_of_stock_also_not_retryable` / `test_start_failure_is_retryable` 三条钉住。

> 后者最初我写成了「必须归 TRANSIENT」，跑测试时发现实际归到 `OUT_OF_STOCK`。
> 停下来想了一下：**两条都对** —— 资源被抢占本来就是「没货」语义。
> 真正的不变量是「可重试」，不是某个具体 kind。所以改的是测试，断言改成
> `retryable is True`，并补了一条覆盖 generic unknown 的用例。

### 5. `ssh_endpoint()` 不缓存

PPIO 实例停止后可能被调度到另一台机器，端口和密码会变 —— **但不是每次变**。
「大多数时候还能用」正是最危险的 bug 形态：旧值能用的次数足够多，多到足以把 bug 藏起来。
测试 `test_endpoint_not_cached` 用两次不同端点的 fixture 证明每次都重取。

---

## 六、测试覆盖（68 项）

| 组 | 数量 | 钉住什么 |
|---|---|---|
| 纯函数（`_money` / `_norm` / `_parse_ssh` / `classify_ppio`） | 8 | 精度换算、状态码分类、kind 不越闭集合 |
| 契约 | 5 | 16 方法齐全、无未说明 None、`require()` 门禁 |
| 视图 | 6 | 字段映射、云字段不外泄、未知状态不抛、游标翻页 |
| 建机 | 11 | payload 形状、SKU 挑选、4090/4090D、rootfs 夹取、不幂等 |
| 报价 | 6 | 精度、磁盘日结不折算、GPU 数乘算、Spot 不可用抛错 |
| 库存 | 5 | 读 available、型号错抛 NOT_FOUND、spot 过滤 |
| 余额 / 镜像 | 7 | 1/10000 单位、地址透传、按名查找、去重 |
| SSH | 3 | command 解析、不缓存、缺 SSH 信息算 TRANSIENT |
| 生命周期 | 3 | start/stop/terminate 路径、开机可重试、定时关机 UNSUPPORTED |
| 凭证 | 4 | 构造不校验 / ready 校验、BYOK 不污染环境变量、优先级 |
| 注册表 | 5 | BYOK 取实例、矩阵常显 ppio、`ready` 标记、`available` 排除 |

---

## 七、验收对照

| # | 标准 | 结果 |
|---|---|---|
| 1 | 实现全部方法 | ✅ 16 个，含共享契约测试（ppio 已加入 `_instances()`） |
| 2 | `get_provider("ppio")` 能拿到实例 | ✅ `PPIO_API_KEY=... python3 -c "..."` → `name=ppio` |
| 3 | `l5l6 providers` 里能看到 | ✅ `ppio 停机=✓ 外网=✓ OCI=✓ ... 免费系统盘=130GB` |
| 4 | 原有测试仍全绿 | ✅ 1070 项 = 1002 基线 + 68 新增 |
| 5 | 真机走通一次 | ⏳ **未做**，需要 token（见第八节） |
| 6 | `capabilities()` 无未说明 None | ✅ 由测试强制 |
| 7 | 调研笔记存在 | ✅ 两份：`国内GPU平台选型-20261113.md`、`autodl-api-notes.md` |

---

## 八、真机联调前必须回答的 9 件事

代码里凡是「未实测」都标了 `⚠️ PENDING`，这里集中列。**拿到 token 后按顺序跑**：

| # | 问题 | 为什么必须实测 | 怎么测 |
|---|---|---|---|
| 1 | `products` 真实响应结构是否与文档一致 | 全部逻辑的地基；字段名错了整个适配器都不通 | `GET /gpus/v2/products?type=gpu&category=instance` |
| 2 | 按量真实单价（4090 / H20 / L40S） | 官网价格页 JS 渲染抓不到 | 同上，看 `pricing.postpaid.final_price` + `precision` |
| 3 | `status.status` 完整枚举 | 映射错 → reaper 误杀在跑的机器 | 建机全程每 2s 拉一次，收集所有出现过的值 |
| 4 | 计费起算点：创建即计时 vs 运行中才计时 | **官方两处说法矛盾**（计费页 vs FAQ）；70GB 镜像拉取十几分钟，差一天几块钱 | 建机记时间戳，对照账单 |
| 5 | 停止后重启的成功率 | 官方明说「可能无法正常启动」，直接决定「失败停机保留 + 唤醒」能不能用 | 建机 → stop → start，重复 5 次 |
| 6 | 130GB 免费系统盘的计费口径 | 决定 `bills_disk_when_stopped` 是否要改成 False | 停机 24h 看账单 |
| 7 | `tools.ssh` 端口/密码在 stop→start 后是否变 | 变了还缓存 = 连不上 | 两次 `get()` 对比 |
| 8 | `ports[].endpoint` 是固定还是每次重分配 | 决定 ComfyUI 地址要不要每次重查 | 同上 |
| 9 | 国内区实际有哪几个 | 别被调度到境外 | `products[].region_ids` 汇总 |

### 已知的产品面限制（不是 bug，是 PPIO 的现状）

- **没有 4090-48G**：我们主力有 4090 24G，但 `gpu_ladder` 里的 48G 升卡档没有对应 SKU，
  OOM 升卡阶梯需要按 PPIO 的实际卡型重排（4090 → H20 96G / L40S 48G）
- **没有定时关机**：`p2-reaper.timer`（15 分钟）+ 本地 `stop_at` 台账是唯一兜底
- **停止后可能开不回来**：会影响「失败停机保留 + 续跑唤醒」设计。
  可能需要改成「到期直接销毁重建」，而不是指望唤醒

---

## 九、怎么用

```python
from tools.p2.providers import get_provider

# BYOK：客户自己的 key
p = get_provider("ppio", credentials={"api_key": "sk_..."})
# 或读环境变量 PPIO_API_KEY

print(p.balance())                                    # 19.79
print(p.stock("4090"))                                # 6
print(p.price(gpu_type="4090", cpu=16, memory_gb=62,
              disk_gb=130, charge_type="Postpay"))
# {'hourly_instance': 1.98, 'hourly_disk': 0.0, 'hourly_total': 1.98,
#  'daily_disk': 0.0, 'currency': 'CNY', 'free_system_disk_gb': 130, ...}

uid = p.create(image="registry.cn-sh/x/comfy:v4", gpu_type="4090",
               cpu=16, memory_gb=62, disk_gb=130, name="job-001")
ep = p.ssh_ready(uid)          # 等 sshd 真能收命令
p.stop(uid)                    # 停止计费（数据保留 7 天）
p.terminate(uid)               # 彻底删除
```

CLI：

```bash
python3 -m tools.p2.l5l6 providers              # 四家能力对照
python3 -m tools.p2.l5l6 providers --matrix     # JSON（含 ready 标记）
python3 -m unittest tools.p2.tests.test_ppio    # 68 项，0.1 秒，零费用
```

---

## 十、顺手发现的环境问题（与本次改动无关，已用 HEAD 基线验证）

新建 worktree 跑测试会撞 4 条失败，**在 HEAD 基线（撤掉全部 ppio 改动）下原样复现**，
根因是 gitignore 掉的运行时文件没跟过来：

| 缺什么 | 症状 |
|---|---|
| `compshare/credentials.env` | `test_oom_wiring` 报 `缺少凭证: CS_PUBLIC_KEY...` |
| `.compshare/` | `test_lifecycle` 报 `缺账本：.compshare/ledger.jsonl` |
| `.share_token` | `test_cross_cloud` 部署机守卫检测不到标记，不拒绝执行 |
| `reports/p2/p21/inputs/*.mp4` | `test_budget` trim 测试拿不到帧数（379.7 不小于 379.7） |

补齐这四样后 1070 项全绿。**下次开新 worktree 记得带上**，否则会以为是自己的改动把测试跑挂了
（我第一次就差点这么以为）。
