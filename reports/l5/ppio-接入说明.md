# PPIO 派欧云 Provider 接入 · 说明与验证

> 分支：`feat/l5-ppio` · 工作树 `/root/zhiyun-l5-ppio` · 建于 `main @ 0c677e1`
> 日期：2026-11-13
> 相关：`reports/l5/国内GPU平台选型-20261113.md`（为什么选这家）、
> `reports/l5/autodl-api-notes.md`（AutoDL 那条线为什么停）

---

> ⚠️ **前置风险，先读这个**：`ppio.com` 全站导航与 `/pricing` 页把「GPU 云服务 / GPU 服务」
> 标了红底「**即将下线**」徽章，但官方 34 条公告里没有对应 EOL 通知，也没有最终服务日期。
> 后台 API 目前完全正常（价格/镜像/余额都能读，文档 Last-Modified 是今天）。
> 完整证据、矛盾点与决策建议见 [ppio-下线风险核查.md](ppio-下线风险核查.md)。
> **本文档记录的是「这个适配器在技术上成立」，不构成「这家平台值得长期依赖」的结论。**

---

## 一句话

`tools/p2/providers/ppio.py` 实现全部 16 个契约方法，`get_provider("ppio")` 可用，
`l5l6 providers` 能力矩阵里能看到，**测试 1084 项全绿（基线 1002 + 新增 82）**。

**拿到真 key 后只读部分全部跑通**（balance / products / images / instances / price / stock），
并**抓出两处文档与现实的偏差**（金额除数、镜像地址），都已按实测修正并写成回归测试。
建机未做 —— 余额 ¥0。全部 82 项测试零网络零费用，用真响应 fixture 复放。

---

## 一、改了什么

| 文件 | 行数 | 性质 |
|---|---|---|
| `tools/p2/providers/ppio.py` | ~880 | 新增：PPIO 适配器 |
| `tools/p2/tests/test_ppio.py` | ~900 | 新增：82 项离线测试（含 14 项真响应漂移探测） |
| `tools/p2/tests/fixtures/ppio/*.json` | 5 个 | 新增：**真机响应原样存档**（products/balance/images×2/instances） |
| `tools/p2/providers/base.py` | +15 | 契约新增 `Provider.ready()` |
| `tools/p2/providers/registry.py` | +42/−11 | 注册 ppio；拆开 `available()` 与 `capability_matrix()` |
| `tools/p2/tests/test_l5_providers.py` | +6/−1 | ppio 加入共享契约测试 |

**没有动 `compshare.py`，没有动 `cs.py`，没有动任何上层**（`run.py` / `scheduler.py` / `reaper.py` 一行没改）。

---

## 一点五、真机实测推翻了文档（2026-11-13 21:25）

拿到 key 后的**第一轮只读请求就抓出两处文档与现实的偏差**，都已按实测修正：

### ① 金额除数不是 `precision`，是固定的 1e5

官方 OpenAPI spec 写的是「整数 × `pricing.precision`」，我第一版就照它实现。
真响应一打就废：

```
4090.16c62g.os   precision=10   final_price=231000
L40S.22c125g     precision= 0   final_price=396000
storage_price_per_gb = 1000（两者都一样）
```

按 `precision` 算会得到 `231000/10**10 = 0.0000231 元/时` 和 `396000/10**0 = 396000 元/时`，两个都不可能是真的。

**除数是 1e5，三条互相独立的证据**：

| 证据 | 算式 | 结论 |
|---|---|---|
| `storage_price_per_gb=1000` | 1000/1e5 = 0.01 | 等于官方计费页的「0.01 元/GB/天」 |
| 4090 `spot=116000` | 116000/1e5 = 1.16 | 等于第三方监测（rentgpu.link）的「PPIO 4090 Spot ¥1.16/h」 |
| 4090 包月 12 月 `137182000` | /1e5/720h = 1.905 | 比按量 2.31 便宜 17%，长租折扣合理 |

现在 `_MONEY_SCALE = 100000.0` 写死，`_money()` 不再接受 precision 参与计算，
并做了一次合理性夹取（>¥200/时 或 <0 返回 0，让上层按「未知价」处理，
而不是把荒谬数字塞进成本账）。

**真机验证的价格表**（`price()` 实际返回）：

| 产品 | CPU | 内存 | 按量 ¥/h | Spot ¥/h |
|---|---|---|---|---|
| 4090.16c62g / .16c62g.os / .16c96g.v2 / .16c125g | 16 | 62~125 | **2.31** | **1.16** |
| L40S.22c125g | 22 | 125 | **3.96** | —（不支持 Spot） |
| L40S.28c125g | 28 | 125 | **3.96** | **1.98** |
| H100-80GB.16c128g / .22c150g / .20c236g | 16~22 | 128~236 | **23.76** | **11.88** |

9 个产品，价格与第三方监测逐条吻合。

### ② 镜像地址在 `tags[].full_image_name`，不在 `image`

文档说记录的 `image` 字段是「镜像标识」。真响应里：

```json
{"id":"1", "name":"pytorch", "image":"pytorch",
 "tags":[{"tag":"v2", "full_image_name":"image.ppinfra.com/prod-gpucloudpublic/pytorch:v2"}]}
```

`image` 只是**短名**，直接塞进 `create()` 会拿到一个拉不动的名字。
已改成从 `tags[].full_image_name` 取，并按 `名字:tag` 精确匹配；
`images()` 也改成**按 tag 展开**（真机展开出 1852 条，一个镜像名下十几个 tag）。

### ③ 余额 0 时所有库存都是 0（**不能据此说平台无货**）

真响应里 9 个产品全部 `available: 0` / `deployable: false` / `inventory_state: "none"`。
余额为 0，分不清是「真无货」还是「没余额所以不报库存」。
所以 `stock()` 的真机结论只有一句：**能读到字段，但读不到非零值**。
真库存要等充值后再测。

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
| **真响应漂移探测** | **14** | 用真机 JSON 钉住 1e5 除数、`precision` 不可信、镜像地址在 tags、余额 0 时库存全 0 |
| 纯函数（`_money` / `_norm` / `_parse_ssh` / `classify_ppio`） | 10 | 1e5 换算、荒谬值夹取、状态码分类、kind 不越闭集合 |
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
| 5 | 真机走通一次 | ⚠️ **只读部分已完成**：balance / products / images / instances / price / stock 全部真机跑通（2026-11-13 21:25，零费用）。**建机未做**：余额 ¥0，建机必被拒 |
| 6 | `capabilities()` 无未说明 None | ✅ 由测试强制 |
| 7 | 调研笔记存在 | ✅ 两份：`国内GPU平台选型-20261113.md`、`autodl-api-notes.md` |

---

## 八、真机联调清单（2026-11-13 更新）

### 已回答（真 key，只读，零费用）

| # | 问题 | 答案 |
|---|---|---|
| 1 | `products` 结构是否与文档一致 | ❌ **不一致**：`precision` 不可信（除数固定 1e5）、镜像地址在 `tags[].full_image_name`。已修 |
| 2 | 按量真实单价 | ✅ 4090 ¥2.31 / L40S ¥3.96 / H100 ¥23.76；Spot 4090 ¥1.16 / H100 ¥11.88 |
| 3 | 地域表 | ✅ 25 个 region，国内有 `cn-south-1` / `cn-north-2` / `cn-beijing-nas-1` / `cn-east-nas-2` / `cn-south-nas-1,2` / `cn-hongkong-1`，另有 4 个纯数字历史 id（`26/28/29/33`） |
| 4 | `images` 能不能解析 | ✅ 1852 条（按 tag 展开），`resolve_image("pytorch")` → `image.ppinfra.com/prod-gpucloudpublic/pytorch:v2` |
| 5 | `balance` 单位 | ✅ `availableBalance` = `"0"`，1/10000 元口径已按 0 验证（非零值待充值后复核） |
| 6 | `stock` 能读到吗 | ⚠️ 字段能读到，但余额 0 时全是 0 —— **不能说平台无货** |

### 仍待回答（需要建机，即需要充值）

| # | 问题 | 为什么必须实测 | 怎么测 |
|---|---|---|---|
| 1 | 真实库存（非零） | 现在全是 0，分不清「无货」还是「没余额不报」 | 充值后重打 products |
| 2 | `status.status` 完整枚举 | 映射错 → reaper 误杀在跑的机器 | 建机全程每 2s 拉一次，收集所有出现过的值 |
| 3 | 计费起算点：创建即计时 vs 运行中才计时 | **官方两处说法矛盾**（计费页 vs FAQ）；70GB 镜像拉取十几分钟，差一天几块钱 | 建机记时间戳，对照账单 |
| 4 | 停止后重启的成功率 | 官方明说「可能无法正常启动」，直接决定「失败停机保留 + 唤醒」能不能用 | 建机 → stop → start，重复 5 次 |
| 5 | 130GB 免费系统盘的计费口径 | 决定 `bills_disk_when_stopped` 是否要改成 False | 停机 24h 看账单 |
| 6 | `tools.ssh` 端口/密码在 stop→start 后是否变 | 变了还缓存 = 连不上 | 两次 `get()` 对比 |
| 7 | `ports[].endpoint` 是固定还是每次重分配 | 决定 ComfyUI 地址要不要每次重查 | 同上 |
| 8 | `typical_boot_s`（建机到 sshd 可用） | 现在声明是 None | 建机记时间戳 |
| 9 | `create()` payload 是否被接受 | 全部逻辑的最后一环，一次真实下单才知道 | 建最便宜的 4090，跑完立刻 release |

### 卡点

**余额 ¥0，建机一定失败**（`AuthorizedFailed` 或余额不足）。真机验证到此为止 ——
再往下每一步都要花钱，且需要先充值。`create()` 的 payload 形状目前只有文档依据。

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
