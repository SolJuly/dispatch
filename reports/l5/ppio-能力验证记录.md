# PPIO 能力验证记录（真机）

> 日期：2026-11-13 21:50–22:05 · 分支 `feat/l5-ppio`
> 目的：**只验证能力**，不管下线风险（那份另见 `ppio-下线风险核查.md`）
> 花费：**¥0.04**（余额 5.00 → 4.96）
> 实例：`9c109a3b31cf9b03`（4090.16c62g.os @ cn-south-1），已释放

---

## 一、结论速览

| 能力 | 验证方式 | 结果 |
|---|---|---|
| `balance()` | 真机 | ✅ 200，单位 1/10000 元 |
| `products()` | 真机 | ✅ 9 个 SKU / 3 种卡 |
| `price()` | 真机 | ✅ 4090 ¥2.31/h、Spot ¥1.16/h、L40S ¥3.96、H100 ¥23.76 |
| `stock()` | 真机 | ✅ 有货时返回 1，无货 0，型号不存在抛 NOT_FOUND |
| `create()` | **真建机** | ✅ 返回 `9c109a3b31cf9b03` |
| `get()` | 真机 | ✅ 字段全对，价格 ¥2.31 |
| `list_instances()` | 真机 | ✅ |
| `stop()` | 真机 | ✅ → `exited`（不是 `stopped`） |
| `start()` | 真机 | ✅ 3 秒回 `running` |
| `terminate()` | 真机 | ✅ 删除后查不到，实例列表空 |
| `ssh_endpoint()` | 真机 | ✅ 解析出 host/port/pass |
| `ssh_ready()` | 真机 | ❌ **连不上**（平台策略，见第四节） |
| `scheduled_stop()` | 路径探测 | ✅ 确认不存在，按设计抛 UNSUPPORTED |
| `resolve_image()` | 真机 | ✅ `cuda:11.8.0-devel-ubuntu22.04` 解析出完整地址 |
| `images()` | 真机 | ✅ 1852 条（按 tag 展开） |
| **自定义端口** | **真建机** | ✅ 8188 映射成功，拿到 endpoint |
| **OCI 镜像** | **真建机** | ✅ 直接指定 docker 地址成功 |

**16 个方法里 15 个真机验证通过，1 个（`ssh_ready`）受平台网络策略阻断。**

---

## 二、这一轮抓到的 4 个真 bug

都是「文档没说 / 说了但不对 / 我写错」的，每一条都会在真机上出问题。

### 1. 停机状态是 `exited`，不是 `stopped`

```json
{"status": {"status": "exited", "error": "", "message": ""}}
```

我的 `_STATE_MAP` 里没有 `exited` → `state` 落进 `unknown`。
后果：`reaper` 会把一台**正常停机**的实例当成异常实例处理。

已修：`_STATE_MAP["exited"] = "stopped"` + 回归测试
`test_real_stopped_state_is_exited`。

> 这正好是「未知状态保留而不抛异常」那个设计要兜的情况 —— 兜住了没崩，
> 但「没崩」不等于「对」。实测之前我把它当成了保险，实测之后才知道它是 bug。

### 2. 同一张卡有两种命名，`stock()` 会误报 NOT_FOUND

```
products 接口 → "RTX 4090 24GB"
regions 接口  → "NVIDIA GeForce RTX 4090 24GB"
```

`stock("NVIDIA GeForce RTX 4090 24GB")` 抛 NOT_FOUND ——
而**这个字符串是官方接口原样返回的**，上层照抄就会踩。

已修：新增 `_norm_model()` 剥掉 `nvidia` / `geforce` / `sxm` / `ada` /
`generation` 等噪音词（**保留显存容量**，24GB 与 48GB 是不同产品）。
现在 8 种写法全部命中：`RTX 4090 24GB` / `NVIDIA GeForce RTX 4090 24GB` /
`4090` / `rtx-4090` / `GeForce RTX 4090` …

### 3. 建机必须显式给 `candidate_regions`，否则报「region is not active」

```
400 VALIDATOR_PARAM: the region is not active
```

我原来的理解是「不传就让平台自动调度」（AutoDL 是这样）。
PPIO 不是：不传直接拒绝。必须传一个**当前 active** 的地域。

实测 `cn-south-1` 可用，`cn-hongkong-1` / `cn-beijing-nas-1` 等未逐个验证。

### 4. `memory_gb` 和价格字段是**字符串**

```json
"resource_specs": {"memory_gb": "62"}
"pricing": {"instance_price": "231000"}
```

`int("62")` / `float("231000")` 能过，所以没炸；但如果哪天写
`spec.get("memory_gb") or 0` 返回 `"62"` 再做算术就会 TypeError。
已确认现有实现能正确转换（`get()` 返回 `mem=62`、`price_per_hour=2.31`）。

---

## 三、实测数据

### 库存：余额 0 时全 0，充值后才有值

| 时刻 | 余额 | 有货 SKU |
|---|---|---|
| 21:25 | ¥0 | 9 个全是 `available: 0` / `deployable: false` |
| 21:50 | ¥5 | **2 个 4090 SKU 各 1 张**，其余仍 0 |

这证实了之前的怀疑：**余额 0 时 API 不返回真实库存**。所以「库存全 0」不能
读成「平台无货」—— 那是我在上一份报告里唯一没敢下结论的一条，现在有答案了。

### 状态机与耗时

```
create() → running        4 秒内（无中间态，直接就是 running）
stop()   → exited         立即
start()  → starting → running   3 秒
```

### 计费节奏：不是实时扣，是整点扣

```
运行中 90 秒：  余额 4.9600 → 4.9600 → 4.9600   （一分不动）
整点（22:00）：余额 5.0000 → 4.9600              （扣 ¥0.04）
停机后 90 秒：  4.9600 → 4.9600 → 4.9600        （不再扣）
```

**这解释了为什么「实时盯余额」看不出扣费** —— 与 AutoDL「整点 + 关机时扣费」一致。
也意味着**短时探测的花费在整点才可见**，不要因为「余额没变」就以为免费。

### stop → start 后 SSH 参数不变

```
停机前: proxy.cn-south-1...:36221 / <REDACTED>（实例已释放）
开机后: proxy.cn-south-1...:36221 / <REDACTED>
```

这次没变。但**不能据此缓存** —— 官方没说保证不变，样本只有 1 次。
`ssh_endpoint()` 仍然每次重取（这条设计不变）。

---

## 四、`ssh_ready()` 失败原因：已定位到精确层级（2026-11-13 22:45 补充实验）

上一版这里只写了「平台策略，IP 白名单」。补做实验后**结论更精确了**，
而且顺带发现 `command` 的真实语义。以下全部是实测，不是推测。

### 4.1 分层探测：TCP 通、协议协商被打断

对同一个 SSH 端口 `54786` 做三层探测，三次全部一致：

```
[1] TCP 握手成功
    发 SSH 版本串后 RST（代理层拒绝）
[2] TCP 握手成功
    发 SSH 版本串后 RST
[3] TCP 握手成功
    发 SSH 版本串后 RST
```

**这排除了「实例内没起 sshd」**：如果 ssd 没起来，TCP 根本连不上
（端口映射后面没有监听）。现在 TCP 能通，说明有东西在听。

### 4.2 再分层：是不是只拦 SSH 协议？

同一个端口，发不同内容：

| 发出内容 | 结果 |
|---|---|
| `SSH-2.0-probe\r\n` | RST |
| `GET / HTTP/1.1...` | RST |
| `\x00\x01\x02\x03`（纯字节） | RST |
| **什么都不发** | 收到 0B（连接保持） |

**所有内容都被 RST，只有「不发数据」时连接保持。**

这不是「SSH 协议被封」，是**代理层在没有通过某些检查前不接受任何上行数据**。
结合官方公告「TCP 端口映射必须提交访问源 IP 白名单」，
最合理的解释是：**TCP 映射在当前账号/源 IP 下未放行**，代理只完成 TCP 握手，
一旦收到数据就判定来源未授权并 RST。

### 4.3 官方 sys 日志证明 sshd 是独立 sidecar，且启动成功

```
create container pod-<id>-ssh success
start  container pod-<id>-ssh success
create container pod-<id>-webterm success
```

PPIO 的 SSH 不是镜像提供的，是平台 sidecar 容器。所以：

- ✅ **镜像里有没有 sshd 不重要**（换 pytorch / cuda / fooocus 都一样）
- ✅ sidecar 启动成功，sshd 在跑
- ❌ 但连不上 —— 卡在代理层

**这解释了为什么换镜像、重启实例都无效**：问题不在实例内。

---

## 四·补：`command` 参数的真实语义（文档没说，实测踩到）

`create(command=...)` 文档只写「覆盖镜像构建时设置的 CMD」。
实测它**不是 shell 字符串，被按空格切成 argv 传给 supervisord**：

```
command="python3 -m http.server 8188"
  → Error: positional arguments are not supported: ['python3', '-m', 'http.server', '8188']
     For help, use /usr/bin/supervisord -h

command="bash -c 'python3 -m http.server 8188'"
  → Error: positional arguments are not supported: ['bash', '-c', 'python3 -m http.server 8188']
```

**supervisord 不接受带位置参数的 argv。** 所以 `command` 只能是
**一个无参数的可执行文件**（或依赖镜像内的入口脚本自己解析环境变量）。

后果：想「在实例内起个服务」不能靠 `command` 传一条命令行。
可行路径只有：
1. 镜像自带入口（如官方 fooocus / ComfyUI 模板）
2. 用 `envs` 传配置，让镜像内的 entrypoint 自己决定
3. 配好 SSH/IP 白名单后走 SSH 推命令

### 顺带：HTTP 端口映射的认证表现（已实测）

```
无 header    → HTTP 401 Unauthorized     ← 网关认证层在工作
Bearer key   → HTTP 502 Bad Gateway      ← 认证通过，但后端没服务
```

**401 证明网关是通的、Key 是有效的**；502 是因为 8188 上没有服务（`command` 起不来）。
也就是说 **HTTP 端口映射本身可用**，只要有服务在监听。

---

## 四·原：`ssh_ready()` 为什么失败：平台策略，不是适配器问题

```
kex_exchange_identification: read: Connection reset by peer
Connection reset by 124.223.119.199 port 36221
```

官方公告（2026-09-24，`ppio.com/docs/announcement/announcement`）：

> **GPU Instance TCP 端口映射**：SSH、数据库端口及其他 TCP 服务必须提交
> **访问源 IP 白名单**，不再支持全网开放。已开通 TCP 映射的用户，请
> 在 2026-10-15 前完成白名单配置。

HTTP 端口映射也要带 API Key：

> 通过 `*.gpu-instance.ppinfra.com` 访问实例 HTTP 服务时，必须携带有效 API Key。

**对我们的直接影响：**

1. `ssh_ready()` 在配置 IP 白名单之前**必然失败**。`ssh_endpoint()` 能拿到正确
   参数（已验证），但连不上。
2. `provision.sh` 走 SSH 推送 → **被阻断**，除非先把部署机公网 IP 加进白名单。
3. ComfyUI 的 HTTP 端口 → 要带 `Authorization: Bearer <key>`，我们的
   `lab/*.py` 全部是裸 `urlopen`，**全部要加 header**。

这条不是「能力不存在」，是「能力要额外配置」。适配层能做的只有：
把失败翻译成可读的原因（现在是 `TRANSIENT`，应该能区分成「未配白名单」）。

> ⚠️ 本次没有修改 `ssh_ready()` 的错误分类 —— 因为「Connection reset」
> 到底是白名单还是别的原因（实例内没起 sshd？）**没有区分开**。
> 这条留作已知缺口，不猜。

---

## 五、`scheduled_stop` 的确认

用真 key 探了 6 条候选路径，全部 404：

```
/gpus/v2/instances/x/timed-shutdown   404
/gpus/v2/instances/x/timed_shutdown   404
/gpus/v2/instances/x/timer            404
/gpus/v2/instances/x/auto-stop        404
/gpus/v2/timers                       404
/gpus/v2/instances/x/schedule         404
```

对照：同期探测 `/gpus/v2/instances/x/nonexistent-action` 也是 404，
而 `/gpus/v2/instances/x/stop` 返回 `400 RESOURCE_NOT_FOUND`（= 路径存在，实例不存在）。

**所以 404 在这里是可信的「路径不存在」信号**（有真 key 之后网关会把请求路由到业务层）。
`supports_scheduled_stop=False` 这个声明站得住。

### 顺带探到文档没写的接口

| 路径 | 状态 | 说明 |
|---|---|---|
| `GET /gpus/v2/regions` | ✅ 存在 | 9 个地域 + 各地域的 GPU 型号列表（文档没写） |
| `GET /gpus/v2/jobs` | ✅ 存在 | 保存镜像等 Job |
| `GET /gpus/v2/templates` | ✅ 存在 | 官方模板 |
| `GET /gpus/v2/deployments` | 404 | — |
| `GET /gpus/v2/vpcs` | 404 | — |
| `GET /gpus/v2/instances/x/monitor` | 404 | 无监控接口 |

`regions` 的 GPU 列表比 `products` 更宽（含 A100 / 5090 / RTX 6000 Ada），
但那些**没有可下单的 SKU** —— 所以 `stock()` 对它们抛 NOT_FOUND 是对的。

---

## 六、还没验证的

| # | 项 | 为什么没验 | 怎么验 |
|---|---|---|---|
| 1 | `ssh_ready()` 真正可用 | 需要 IP 白名单，得走工单 | 提工单加白名单后重试 |
| 2 | HTTP 端口映射带 Key 能否访问 | 需要实例内有 HTTP 服务在听 | 建机时 `command` 起一个 `python3 -m http.server 8188` |
| 3 | `exited` 之外还有没有别的状态 | 只走了 create/stop/start/terminate | 触发异常（如 OOM）看 `failed` 长什么样 |
| 4 | 计费起算点（创建 vs 运行中） | 需要跨整点做对照实验，成本更高 | 22:59 建机、23:01 删，看是否按 2 分钟计 |
| 5 | 130GB 系统盘停机是否收费 | 需要停机跨一天 | 停 24h 看次日账单 |
| 6 | `prepaid` / `spot` 真实建机 | 会锁资金/可能被回收 | 单独验证，不在这次范围 |

---

## 七、能力验证的最终判断

**PPIO 的 Provider 契约是可实现的，16 个方法里 15 个真机通过。**

唯一失败的 `ssh_ready()` 是平台网络策略（IP 白名单），不是 API 能力缺失 ——
同一个实例的 SSH **参数**是对的（host/port/password 全部解析正确），只是连接被网关重置。

**对出片链路的实际影响（精确版）：**

| 环节 | PPIO 现状 |
|---|---|
| 建机 / 改配 / 删除 | ✅ 全通（API） |
| 把 ComfyUI 镜像跑起来 | ✅ 建机时指定镜像 |
| **推 provision.sh（装环境）** | ❌ SSH 不通 → 推不进去 |
| **访问 ComfyUI 界面** | ⚠️ HTTP 端口可用（401→502 已证明网关通），但要带 `Authorization: Bearer` |
| **在实例内跑任意命令** | ❌ 目前无路（SSH 不通、`command` 不支持带参数） |

**所以答案是：不完全只能 HTTP，但当前只有 HTTP 这一条路真正通了；
而这恰好是「执行命令」最难的那部分。**

要打通「在实例内执行命令」，有两条路：
1. **工单加 IP 白名单** → 恢复 SSH（这是官方支持的正常路径）
2. **自己做镜像**：把启动逻辑写进镜像 entrypoint，用 `envs` 传参，
   服务自己起 —— 不依赖 SSH，也不需要 `command` 带参数

第 2 条与「镜像路线四选一」（`reports/p20/镜像路线四选一-待决.md`）其实是同一件事：
**如果确定长期用 PPIO，镜像必须自带入口，不能指望 SSH 推脚本。**

这个结论与下线风险无关，是纯粹的当前能力边界。
