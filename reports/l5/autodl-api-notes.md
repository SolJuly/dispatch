# AutoDL API 调研笔记（第一步：只调研，不写代码）

> 建立：2026-11-13 19:20
> 分支：`feat/l5-autodl` · 工作树 `/root/zhiyun-l5-autodl`
> 结论一句话：**能接，但 AutoDL 只开放了「容器实例 Pro」这一条产品线，语义与我们现有的
> `Provider` 契约有 5 处硬缺口（CPU/内存不可选、无数据盘、无定时关机、无 Pro 库存、
> 无价格目录）**。这 5 处必须在 `capabilities()` 里诚实声明，不能假装支持。
>
> 调研手段：① 官方文档三页（common_api / instance_pro_api / price）+ 计费/SSH/网络/端口/
> 数据保留/省钱绝招 6 页；② 两份第三方实现（`Minato-Aqukin/AutoDL-cli` 的 TS 源码、
> `NannaOlympicBroadcast/autodl-agent-package` 的 Python 客户端）交叉验证；③ **对
> `https://api.autodl.com` 做了无凭证探测**（只发请求看返回信封，未使用任何 token）。
> 标「实测」的是我这一轮真跑过的；标「来源」的是第三方实现里的实测记录，需我们真机复核。

---

## 一、认证与传输层

| 项 | 结论 |
|---|---|
| Host | `https://api.autodl.com` |
| 鉴权头 | `Authorization: <开发者Token>`，**裸 token，没有 `Bearer` 前缀** |
| Token 位置 | 控制台 → 账号 → 设置 → 开发者 Token（`/console/center/settings/token`） |
| 前置条件 | **Pro API 要求账号已完成个人实名或企业认证**，未认证时接口不响应（第三方 README 标注「必须先实名认证，否则 API 根本不响应」） |
| 响应信封 | `{"code": "Success", "data": ..., "msg": "", "request_id": "..."}` |
| 成功判定 | `code == "Success"`（**不是** HTTP 200 —— 见下） |
| 单位 | 金额一律**千分之一元**（`assets / 1000 = 元`；`payg_price: 1970` = ¥1.97/h） |

### ⚠️ 关键事实（实测，2026-11-13 19:10）

```
POST /api/v1/dev/wallet/balance        无 token        → HTTP 200 + code=AuthorizeFailed
POST /api/v1/dev/wallet/balance        伪造 token      → HTTP 200 + code=AuthorizeFailed
POST /api/v1/dev/instance/pro/create   伪造 token      → HTTP 200 + code=AuthorizeFailed
POST /api/v1/dev/nonexistent_xyz       任意            → HTTP 404 + 纯文本 "404 page not found"
```

推论（直接决定实现方式）：

1. **业务失败永远是 HTTP 200**，靠 `code != "Success"` 判断。所以「服务不可用」只能从
   传输异常 / 5xx / `request_id` 缺失去判，不能靠状态码。
2. **401/403 这条路径大概率不会出现**（第三方实现里写了，但实测走到的是 200+code）。
   我们的 `_classify` 不能只映射 HTTP 状态。
3. **路径存在性可以在无 token 下探测**（200=存在但鉴权失败，404=路径不存在）。这一轮就是
   这样把「文档写了但不存在」的接口筛掉的，见第四节。
4. 鉴权失败文案是中文 `"登录超时，请重新登录"`，`code` 是 `AuthorizeFailed`。
   `base.classify()` 会因为字符串里含 `auth` 命中 `AUTH`（不会误落到 `TRANSIENT`，
   因为 auth 分支在 timeout 分支之前），**这点现有实现刚好正确**，但要有测试锁住。

### GET 带 body 的坑

`snapshot` / `status` 文档标 `GET` 却给了 JSON body。两种第三方实现做法不同：
`autodl-cli` 先发 query string（Go 后端能绑定），只有 msg 看起来像缺参数时才降级成
「真·GET 带 body」（fetch 不允许，要用 `node:https` 裸发）；`p101` 直接给 GET 塞 json body。

我实测两种都能到鉴权层（都是 `AuthorizeFailed`），**无法在无 token 时区分哪种被正确解析**。
→ 实现时按「query 优先，缺参数则降级 body」写，真机联调时二选一锁定。这条要进 fixtures。

---

## 二、接口清单（✅ 实测存在 / ❌ 实测 404 / 🚫 文档没有）

### 2.1 通用（`common_api`）

| 接口 | 方法 | 状态 | 说明 |
|---|---|---|---|
| `/api/v1/dev/wallet/balance` | POST | ✅ | `data: {assets, accumulate, voucher_balance}`，千分之一元 |
| `/api/v1/dev/exclusive_nfs/mount` | POST | ✅ | 切专用 NFS，`mountable: 1/-1`，与企业订阅绑定，**我们不该用** |
| 保存镜像（common_api 里那个空标题） | — | 🚫 | 文档有标题无接口。真正的保存在 Pro 段 |

### 2.2 容器实例 Pro（`instance_pro_api`）—— 我们唯一能用的产品线

| 接口 | 方法 | 状态 | 关键字段 |
|---|---|---|---|
| `/api/v1/dev/instance/pro/create` | POST | ✅ | `req_gpu_amount`(1-4 必填)、`expand_system_disk_by_gb`(0-500 必填)、`gpu_spec_uuid`(必填)、`image_uuid`(必填)、`cuda_v_from`(必填整数)、`data_center_list`、`instance_name`、`start_command`；`data` = `"pro-76419909953e"` |
| `/api/v1/dev/instance/pro/snapshot` | GET | ✅ | 单实例详情：`ssh_command`/`proxy_host`/`ssh_port`/`root_password`/`payg_price`/`origin_pay_price`/`snapshot_gpu_alias_name`/`region_sign`/`expand_system_disk_size`/`system_init_disk_size`/`jupyter_token`/`jupyter_domain`/`service_6006_domain`/`service_6008_domain`/`usage_info` |
| `/api/v1/dev/instance/pro/status` | GET | ✅ | `data` 是字符串，如 `"running"` |
| `/api/v1/dev/instance/pro/list` | POST | ✅ | `page_index`/`page_size`；`data.list[]` 含 `uuid`/`status`/`sub_status`/`region_sign`/`region_name`/`charge_type`/`start_mode`/`req_gpu_amount`/`gpu_spec_uuid`/`created_at`/`started_at`/`stopped_at`/`expired_at`/`timed_shutdown_at` |
| `/api/v1/dev/instance/pro/power_on` | POST | ✅ | `{instance_uuid, payload: "gpu", start_command?}` |
| `/api/v1/dev/instance/pro/power_off` | POST | ✅ | `{instance_uuid}` |
| `/api/v1/dev/instance/pro/release` | POST | ✅ | `{instance_uuid}`；**必须先关机再释放**，否则可能失败 |
| `/api/v1/dev/instance/pro/image/save` | POST | ✅ | `{instance_uuid, image_name}` → `{image_uuid}` |
| `/api/v1/dev/instance/pro/image/private/list` | POST | ✅ | `{page_index,page_size}` → `image_uuid`/`name`/`status`(finished)/`image_size`/`create_at` |

### 2.3 实测 404（**文档没写、也没实现，别想了**）

```
/api/v1/dev/instance/pro/update_config      ← 升降配置
/api/v1/dev/instance/pro/restart|reboot     ← 重启
/api/v1/dev/instance/pro/rename             ← 改名
/api/v1/dev/instance/pro/timed_shutdown
/api/v1/dev/instance/pro/shutdown_at
/api/v1/dev/instance/pro/set_timed_shutdown
/api/v1/dev/instance/pro/auto_shutdown
/api/v1/dev/instance/pro/command/exec       ← 无远程执行
/api/v1/dev/instance/pro/monitor
/api/v1/dev/instance/pro/image/delete|rename|share|public/list
/api/v1/dev/instance/pro/detail|gpu_spec/list|region/list
/api/v1/dev/instance/pro/price
/api/v1/dev/machine/list
/api/v1/dev/gpu/list
/api/v1/dev/wallet/transaction|consume/list
```

`timed_shutdown_at` **作为字段存在于 list 响应里**，但没有任何写入接口 —— 说明平台内部
有这个能力（网页控制台可设），开放 API 没给。这条是第五节 `scheduled_stop()` 判定的依据。

### 2.4 弹性部署（`esd_api_doc`）—— 企业认证才可用，**不接**

`/api/v1/dev/deployment*`、`/api/v1/dev/image/private/list`、
`/api/v1/dev/machine/region/gpu_stock` 均实测存在（200+AuthorizeFailed），
但要求**企业认证**，且是 ReplicaSet/Job 那套编排模型，与我们的「一台实例一个 job」语义
完全不同。列在这里只是为了后面解释「为什么库存接口不能用」。

---

## 三、建机参数的真实自由度（**最大的语义缺口**）

AutoDL 的租法是「选物理主机上的 N 张卡」，CPU/内存由平台**按卡数倍数分配**：

> 主机标注 `32GB/GPU、8核/GPU` → 租 1 卡 = 8 核 / 32GB；租 2 卡 = 16 核 / 64GB。

对照我们的 `create(*, image, gpu_type, gpu, cpu, memory_gb, disk_gb, data_disk_gb, name, charge_type, zone, password, remark)`：

| 我们的参数 | AutoDL 对应 | 处理方式 |
|---|---|---|
| `gpu_type` | `gpu_spec_uuid` | 需别名映射表（`4090`→`v-48g`、`3090`→`v-48g-350w`…） |
| `gpu` | `req_gpu_amount` | 1-4，越界是 `PERMANENT` |
| `cpu` / `memory_gb` | **无参数** | ❌ 平台分配。只能忽略 + 在 `notes` 写明「实际值见 snapshot.usage_info」 |
| `disk_gb` | `expand_system_disk_by_gb`(0-500) | 系统盘基准 30GB，这里传的是**扩容量**，不是总容量 → 要减 30 |
| `data_disk_gb` | **不存在**（Pro 无数据盘） | ⚠️ 传 >0 必须抛 `UNSUPPORTED`，静默忽略等于谎报买到了盘 |
| `charge_type` | **只有按量**（payg） | ⚠️ 传 `Prepay/包年包月` 必须抛 `UNSUPPORTED` |
| `password` | **无参数** | 平台生成 `root_password`，从 snapshot 取 |
| `zone` | `data_center_list`（列表！） | 且**只有 `westDC3` / `beijingDC2` 能建 Pro**（来源：autodl-cli 2026-08-23 全量探测 11 个地区），其余传了会 `RequestParameterIsWrong` |
| `image` | `image_uuid` | 公共 `base-image-xxx` 或私有 `image-xxx` |
| （契约里没有） | `cuda_v_from` **必填** | 公共镜像可从标签推（如 `cuda11.8-…`→118）；私有镜像**没有 CUDA 信息**，只能默认 118 |
| （契约里没有） | `start_command` | 开机后执行，失败不影响开机 —— **这是我们做定时关机的唯一挂载点** |

### GPU 规格表（要内置成静态表）

| 前台显示 | 规格 | `gpu_spec_uuid` | 显存 |
|---|---|---|---|
| H800-80G | 通用型 | `h800` | 80G |
| 4090-48G | 通用型 | `v-48g` | 48G |
| 4090D | 通用型 | `4090D` | 24G |
| 3090-48G | 通用型 | `v-48g-350w` | 48G |
| PRO6000-96G | 性能型 | `pro6000-p` | 96G |
| 4080(S)-32G | 性能型 | `v-32g-p` | 32G |
| 5090-32G | 性能型 | `5090-p` | 32G |

**官方 API 没有目录接口**（`region/list`、`gpu_spec/list`、`gpu/list` 全 404），
所以规格表、地区表、公共镜像表只能**烤进代码**，并标注「最后核对日期」，与
`reports/p20/镜像路线四选一-待决.md` 里「静态表会过期」是同一类债。

---

## 四、状态枚举与映射

文档只给了一个例子 `"running"`，没有枚举列表。三方实现（autodl-cli 的 zod schema）里的
候选集合，加我们的归一化映射（**未真机确认，标 PENDING**）：

| 云侧 status | → `InstanceView.state` | `billing` |
|---|---|---|
| `creating` | `starting` | False |
| `starting` | `starting` | False |
| `running` | `running` | True |
| `shutting_down` | `stopping` | True（关机动作期间仍计时到关机时刻） |
| `shutdown` | `stopped` | False |
| `releasing` | `terminating` | False |
| `released` | `terminated` | False |
| `failed` | `unknown` | False |
| 其它未知值 | `unknown`（**原样塞进 `raw`，不要抛异常**） | False |

`sub_status` 单独存在（如「已关机-意外宕机」），**不要**混进 `state`，进 `raw`。
未知状态保留而不抛异常是硬要求：AutoDL 明确会加状态，抛异常会让 reaper 把在跑的机器当异常。

---

## 五、逐条对齐 12 个契约方法

| # | 方法 | AutoDL 可行性 | 实现要点 |
|---|---|---|---|
| 1 | `capabilities()` | ✅ | 见第七节，5 个 `None` 必须落成真值 |
| 2 | `create()` | ✅ 有损 | 见第三节；`expand_system_disk_by_gb = max(0, disk_gb-30)`；`data_disk_gb>0`、非按量 → `UNSUPPORTED` |
| 3 | `get()` | ✅ 两跳 | **没有单实例 GET**：`status` 只给状态字符串，`snapshot` 给硬件/SSH；`gpu_type`/`region` 要靠 `list` 补 → 实现成 status+snapshot(+list 兜底) 合并 |
| 4 | `list_instances()` | ✅ 分页 | `page_size` 最大按 100 走，循环到 `max_page`；`prefix` 在本地过滤 `name` |
| 5 | `start()` | ✅ | `power_on` + `payload:"gpu"`。**无卡模式 API 不支持**（实测 `cpu`/`no_gpu`/`none` 全报「不支持的启动模式」，空串被接受但仍是有卡） |
| 6 | `stop()` | ✅ | `power_off`；对「正在关机中」的实例再关机会报错 → 归 `BUSY` 短退避，不归 `TRANSIENT` |
| 7 | `terminate()` | ✅ 两步 | 先 `power_off` 再 `release`，且要等关机真的完成；已关机时跳过 power_off |
| 8 | `ssh_endpoint()` | ✅ | `SSHEndpoint(host=proxy_host, port=str(ssh_port), password=root_password, user="root")`。**端口和密码开关机后可能变**（不保证每次变 —— 这正是缓存最危险的地方）→ **每次调用都重拉 snapshot，不缓存** |
| 9 | `ssh_ready()` | ✅ 必须自实现 | `running` ≠ sshd 就绪（新建实例会在还不收连接时就报 running）。探活要有退避间隔，别连着打 |
| 10 | `scheduled_stop()` | ❌ **不支持** | 抛 `UNSUPPORTED`。降级路径见第六节 |
| 11 | `price()` | ⚠️ 半残 | 无价格目录接口，文档原文「请以网站显示的现价为准」。唯一来源是**运行中实例** snapshot 的 `payg_price`（折扣后）/`origin_pay_price`，千分之一元。建机前估价 → 只能查静态表或 `UNSUPPORTED` |
| 12 | `balance()` | ✅ | `wallet/balance` 的 `assets/1000` |
| 13 | `resolve_image()` | ⚠️ 有损 | 公共镜像 UUID 表**不完整**（官方附录以「…更多新上线的镜像请联系客服」结尾）；私有镜像走 `image/private/list` 按名匹配；两者都不是「注册表式解析」 |
| 14 | `images()` | ⚠️ 只能列私有 | 没有公共镜像目录接口。`keyword` 只能过滤私有镜像名 + 内置静态表 |
| 15 | `stock()` | ❌ **不支持** | 唯一的库存接口 `machine/region/gpu_stock` 属于弹性部署线、要企业认证，**且实测与 Pro 可用量对不上**（2026-08-23：表显示 westDC3 有 140 张空闲 4090D，创建却返回「暂无库存」；同样请求去掉地区限制反而成功，落在 beijingDC2）→ 抛 `UNSUPPORTED`，无货靠 create 的错误文案分类 |

---

## 六、计费：**停机免费，但 Pro 的系统盘按日收费**

这是交接单里标 ⚠️「最贵的坑」的答案 —— 分产品线，别混：

### 容器实例（普通，API 租不到）

- 按量：开机开始计费、关机结束；**精确到秒，最低 ¥0.01**；整点扣一次 + 关机扣一次
- 系统盘 30GB + 数据盘 50GB **默认免费**
- **付费扩容的数据盘：无论是否开机，每天凌晨结算一次** ← 停机也烧钱
- 连续关机 **15 天**释放实例，数据清空

### 容器实例 Pro（**我们能租的只有这个**）

- 按量同上（秒级、¥0.01 最低、整点+关机扣费）
- **无数据盘**；系统盘 30GB 默认，**¥0.10/日，只要当天存在该实例就结算，无论开关机**
- 扩容部分（0-500GB）按网页单价、**同样按日、无论开关机**
- 扩容后**不支持缩容**（文件系统特性），只能克隆实例
- 连续关机 **60 天**释放实例

### 其它停机仍可能计费的项（用不用取决于我们）

| 项 | 价格 | 免费额度 | 结算 |
|---|---|---|---|
| 文件存储 `autodl-fs` | ¥0.01/GB/日 | 20GB | 按当日最大用量，次日凌晨扣 |
| 网盘 | ¥0.30/GB/月 | 20GB | 月付 |
| 镜像存储 | ¥0.01/GB/日 | 30GB | 按当日最大用量，次日凌晨扣 |

### 对 `price()` / `capabilities()` 的结论

- `bills_disk_when_stopped = **True**`（仅 Pro：系统盘 ¥0.1/日 ≈ ¥0.00417/h 等效，但**是日结不是时结**）
- `free_system_disk_gb = **0**`（Pro 30GB 也收费；普通实例才有 30GB 免费，但 API 租不到）
- ⚠️ **`hourly_disk` 的语义失真**：契约要求「每小时」磁盘费，AutoDL 是**按日结算**。
  折成小时会让 `cost_snapshot` 少算/多算（跨日边界必错）。建议 `hourly_disk=0.0` +
  `daily_disk` 字段 + `notes` 说明，别偷偷折算 —— 这与 `base.py` 顶部「不隐藏价格口径」的
  设计原则一致。
- 余额 ¥19.79 时：一台 4090D 跑 1 小时 + 1 天系统盘 ≪ ¥5，做真机验证资金充足。

---

## 七、能力声明（草案，逐项给依据）

```python
Capabilities(
    name="autodl",
    can_stop_instance=True,            # power_off 存在且实测路径清晰
    bills_disk_when_stopped=True,      # Pro 系统盘 ¥0.1/日，开关机都收（官方计费页原文）
    instance_has_internet=True,        # 地区共享带宽 3~10Gbps，不单独计费 → provision.sh 可用
    can_pull_oci=False,                # 实例内不允许跑 Docker（官方 env 文档）；无 registry 参数
    has_shared_weight_mount=False,     # 无 /model 共享库；只有只读 /root/autodl-pub 与可写 autodl-fs
    supports_scheduled_stop=False,     # 无写入接口（timed_shutdown_at 只读）→ 靠 start_command 埋 shutdown
    supports_spot=False,               # 无竞价/抢占产品
    supports_custom_image=True,        # image/save + image/private/list
    free_system_disk_gb=0,             # Pro 30GB 也按 ¥0.1/日 收
    typical_boot_s=None,               # ⚠️ 待实测；已知「running 时 sshd 未就绪」，不能猜
    notes={
        "cpu_memory": "由平台按卡数倍数分配，create() 的 cpu/memory_gb 不生效",
        "data_disk": "Pro 无数据盘；/root/autodl-tmp 是系统盘上的保留目录",
        "charge_type": "仅按量计费；包年包月无 API",
        "scheduled_stop": "start_command 里 (sleep N && /usr/bin/shutdown -h now) & 兜底",
        "stock": "唯一库存接口属弹性部署线且与 Pro 可用量不符，只能盲试",
        "bills_disk_when_stopped": "按日结算，不是按小时，勿折算进 hourly_disk",
    },
)
```

---

## 八、错误分类（AutoDL 未公开错误码，只能按文案）

`base.classify()` 现有的关键词命中情况，加 AutoDL 特有项：

| 云侧现象 | 分类 | 备注 |
|---|---|---|
| `code=AuthorizeFailed`，msg `登录超时，请重新登录` | `AUTH` | **实测**。含 `auth` → 现有 classify 已命中，需测试锁住 |
| 文案含 `实名` | `AUTH` | 提示去实名认证，不重试 |
| `RequestParameterIsWrong`（地区不在 Pro 白名单等） | `PERMANENT` | 新增关键词 |
| `ServerError | 不支持的启动模式` | `PERMANENT` | 新增关键词 |
| `暂无库存` / `库存不足` | `OUT_OF_STOCK` | 现有 classify 覆盖 `库存不足`，需补 `暂无库存` |
| 含 `余额` | `BILLING` | 已有 |
| 含 `不存在` / `not found` | `NOT_FOUND` | 已有（主机下架导致实例消失也走这里） |
| 关机中再关机 / 释放中再释放 | `BUSY` | 新增：`正在关机`/`shutting` |
| 408/429/5xx/连接异常 | `TRANSIENT` | 已有 |

**现有 `classify()` 实测验证（2026-11-13 真跑 `tools.p2.providers.base.classify`）**：

```
'AuthorizeFailed 登录超时，请重新登录'  -> ('auth', False)      ✓ 正确
'暂无库存'                            -> ('unknown', False)    ✗ 要补 → out_of_stock
'RequestParameterIsWrong 请求参数错误' -> ('unknown', False)    ✗ 要补 → permanent
'ServerError | 不支持的启动模式'       -> ('unknown', False)    ✗ 要补 → permanent
'余额不足'                            -> ('billing', False)    ✓
'实例不存在'                          -> ('not_found', False)  ✓
```

> 注意：`classify()` 的 kind 闭集合里**没有 `PERMANENT`**（只有 out_of_stock/quota/transient/
> not_found/auth/unsupported/billing/busy/unknown）。参数错这类「终态、不重试」只能落到
> `unknown` 或新增语义。实现时**不要在 autodl.py 里自造 kind**（会破坏上层分流），
> 按 base.py 的闭集合选最接近的一个并在 notes 里写明判据。

### ⚠️ 幂等性硬约束（会重复烧钱）

**`create` 和 `power_on` 都不是幂等的**。`OUT_OF_STOCK` 默认 `retryable=True`，
上层若对 create 做自动重试，可能一次需求租出两台卡。

→ 实现里必须：`create`/`power_on` 的 `ProviderError` 显式传 `retryable=False`，
或者只在「请求未到达服务端」的传输异常上才允许重试（`autodl-cli` 的做法是
`maxRetries: 0`）。这条要写进 `test_autodl.py`。

---

## 九、网络 / SSH / 端口（影响 provision.sh 与出片）

| 项 | 结论 |
|---|---|
| 公网 IP | **没有独立公网 IP**；SSH 走平台反代 `connect.xxx.autodl.com:<高端口>` |
| SSH 用户 | `root`，密码由平台生成（`root_password`），支持控制台配公钥 |
| 端口变化 | **开关机后 `ssh_port` / `root_password` 可能变**（可能被调度到别的机器），也不是每次都变 |
| 外网 | **有**。同地区共享带宽 3~10Gbps，上下行对等，不单独计费 |
| GitHub/HF 加速 | `source /etc/network_turbo`（仅 github/huggingface 域，官方声明不保证稳定） |
| Docker | **实例内禁止跑 Docker**（要裸金属，整机包月起租）→ 我们的 OCI 路线在此不可用 |
| 端口映射 | 只有 **6006 / 6008** 被映射到公网 `https://xxx.seetacloud.com:8443`，协议可选 TCP/HTTP；开放任意端口需**企业认证** |

### 对我们的两个直接后果

1. **ComfyUI 8188 拿不到公网地址**。要么走 SSH 隧道（`ssh -L 8188:127.0.0.1:8188`），
   要么把 ComfyUI 起在 6006。`lab/*.py` 全部硬编码 `http://127.0.0.1:8188`，
   现成脚本在 AutoDL 上必须靠隧道才能跑通 —— 这是接完 provider 之后的第二道功课。
2. **`provision.sh` 终于能跑了**：`instance_has_internet=True`，pip/git 可用，
   这正好补上智云「DNS 解析失败」的死穴（`deploy/env/provision.sh` 头部注释写明该脚本
   在智云上不可用）。但**不能 `docker pull`**，所以镜像路线（P3）如果选 OCI 方案，
   AutoDL 这条线用不了。

---

## 十、下一步（等 YG 拍板后才动手）

按交接单第五节走，③ 之前不写代码。目前 ①（本文档）已完成，②③ 需要 token：

1. **YG 确认两件事**：账号是否已实名认证（未认证 API 完全不响应）；是否给一个 token
   （只写 `compshare/credentials.env` 或新的 `autodl/credentials.env`，**不进对话、不进 git**）
2. 有 token 后按顺序手工 curl：`balance` → `image/private/list` → `create`（最便宜规格，
   不带 `data_center_list` 让平台自己调度，实测成功率最高）→ `status` → `snapshot`（拿 SSH）
   → `power_off` → `release`
3. 把 ③ 的请求/响应脱敏成 fixtures（`tools/p2/tests/fixtures/autodl/*.json`），
   包含：成功信封、`AuthorizeFailed`、`暂无库存`、`RequestParameterIsWrong`、
   snapshot 全字段（密码替换成 `<REDACTED>`）
4. 再写 `autodl.py`

### 待实测清单（真机必须回答，不许猜）

| # | 问题 | 为什么必须实测 |
|---|---|---|
| 1 | `snapshot`/`status` 到底是 query 还是 body | 决定请求构造，猜错=全接口不通 |
| 2 | 状态枚举全集（`creating/starting/shutting_down/releasing/failed` 是否真的出现） | 映射表写错 → reaper 误杀在跑的机器 |
| 3 | `typical_boot_s`（create → sshd 可用） | 影响超时与估价 |
| 4 | 开机后 `ssh_port`/`root_password` 是否变 | 变了还缓存 = 连不上；不变就缓存 = 埋雷 |
| 5 | `expand_system_disk_by_gb=0` 时 snapshot 里 `expand_system_disk_size` 是多少 | 确认「30GB 基准 + 扩容」的口径 |
| 6 | 无货时 `code`/`msg` 的确切文案 | 分类关键词要精确，不能靠猜 |
| 7 | 单实例详情能否只靠 `status`+`snapshot` 拼出 `gpu_type`/`region` | 否则 `get()` 每次都要扫全表 |
| 8 | Pro 实例关机后是否真的一分钱不扣（除 ¥0.1/日系统盘） | 交接单里最贵的坑，必须看账单验证 |

---

## 附：参考来源

- 官方：[API 文档（通用）](https://api.autodl.com/docs/common_api/)、[容器实例 Pro API](https://www.autodl.com/docs/instance_pro_api/)、[充值与计费](https://www.autodl.com/docs/price/)、[容器实例 Pro 概要](https://www.autodl.com/docs/instance_pro/)、[SSH](https://www.autodl.com/docs/ssh/)、[开放端口](https://www.autodl.com/docs/port/)、[网络](https://www.autodl.com/docs/network/)、[学术资源加速](https://www.autodl.com/docs/network_turbo/)、[Pro 数据](https://www.autodl.com/docs/instance_pro_data/)、[省钱绝招](https://www.autodl.com/docs/save_money/)、[维护与故障](https://www.autodl.com/docs/maintenance/)
- 第三方实现（交叉验证，非官方）：
  [Minato-Aqukin/AutoDL-cli](https://github.com/Minato-Aqukin/AutoDL-cli)（TS，含 2026-08 真机实测记录与错误分类）、
  [NannaOlympicBroadcast/autodl-agent-package](https://github.com/NannaOlympicBroadcast/autodl-agent-package)（Python 客户端）、
  [wuzihuang/AUTODL-PLUGIN](https://github.com/wuzihuang/AUTODL-PLUGIN)（MCP 工具目录）
- 本轮无凭证探测：2026-11-13 19:10–19:15，仅 `POST/GET` 空体或伪造 token，未使用真实凭证、未创建任何资源、零费用。
