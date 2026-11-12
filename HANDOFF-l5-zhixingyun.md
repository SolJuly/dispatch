# 智星云 Provider 接入 · 交接单

> 建立：2026-11-13 00:25
> 分支：`feat/l5-zhixingyun`
> 工作树：`/root/zhiyun-l5-zx`
> 目标：L5 多云第二家 —— 智星云（替代原计划的 AutoDL）

---

## 一、起点状态

```
main       0c677e1（AGENTS.md 已合并，PR #1）
分支       feat/l5-zhixingyun，从 main 0c677e1 切出
工作树     /root/zhiyun-l5-zx（39M，.git 共享主仓）
测试基线   1023 项全绿
```

---

## 二、要做什么

在 `tools/p2/providers/` 下新增 `zhixingyun.py`，实现 `Provider` 契约，并在 `registry.py` 注册。

### 已有资产（照抄对象）

| 文件 | 行数 | 作用 |
|---|---|---|
| `providers/base.py` | 301 | **契约定义**，必须实现的方法都在这 |
| `providers/compshare.py` | 210 | **最像的参考实现**，优云智算适配器 |
| `providers/local.py` | 252 | 本地/假 provider，含 `FakeProvider` 供测试 |
| `providers/registry.py` | 121 | 工厂 + 能力矩阵，要在这里加 `zhixingyun` 分支 |
| `tests/test_l5_providers.py` | — | 契约一致性测试，新 provider 必须过 |

### 必须实现的方法（12 个）

从 `base.py` 抄签名，一个都不能漏：

```python
class ZhiXingYunProvider(Provider):
    name = "zhixingyun"

    def capabilities(self) -> Capabilities: ...        # 先填声明值，实测后改
    def create(self, *, image, gpu_type, gpu, cpu, memory_gb,
               disk_gb, data_disk_gb, name, charge_type,
               zone, password, remark) -> str: ...     # 返回云侧实例 ID
    def get(self, uid) -> InstanceView: ...
    def list_instances(self, prefix="") -> list[InstanceView]: ...
    def start(self, uid) -> None: ...
    def stop(self, uid) -> None: ...
    def terminate(self, uid) -> None: ...
    def ssh_endpoint(self, uid) -> SSHEndpoint: ...
    def ssh_ready(self, uid, *, timeout=180) -> SSHEndpoint: ...
    def scheduled_stop(self, uid, at_epoch) -> None: ...  # 不支持就抛 UNSUPPORTED
    def price(self, *, gpu_type, gpu, cpu, memory_gb,
              disk_gb, data_disk_gb, charge_type, image, zone) -> dict: ...
    def balance(self) -> float: ...
    def resolve_image(self, spec) -> dict: ...
    def images(self, keyword="") -> list[dict]: ...
    def stock(self, gpu_type, *, spot=False, zone=None) -> int: ...
```

`wait_state()` 有默认实现，不用重写。

---

## 三、关键设计约束

### 1. 错误必须正确分类

`ProviderError` 分三类，写错会烧钱或漏重试：

| kind | 什么情况 | 上层行为 |
|---|---|---|
| `TRANSIENT` | 网络抖动、无货、正在同步 | **重试** |
| `PERMANENT` | 参数错、余额不足、镜像不存在 | **不重试** |
| `UNSUPPORTED` | 该云没这能力 | 走降级路径 |

参考 `compshare.py` 里的 `_classify()` 和 base.py 的 `classify()`。

### 2. 能力声明要诚实

`Capabilities` 里每个字段都影响上层决策。**先按「未知」填，实测后再改**：

```python
Capabilities(
    name="zhixingyun",
    can_stop_instance=True,           # 待实测
    bills_disk_when_stopped=None,     # ⚠️ 必须实测，猜错会算错钱
    instance_has_internet=None,       # ⚠️ 影响 provision.sh 能否跑
    can_pull_oci=False,
    has_shared_weight_mount=False,    # 智星云应该没有 /model
    supports_scheduled_stop=None,
    supports_spot=None,
    supports_custom_image=None,
    free_system_disk_gb=0,
    typical_boot_s=None,
    notes={},
)
```

`None` = 未知，上层会走保守路径。**不要为了「看起来完整」乱填 True。**

### 3. BYOK 凭证隔离

`registry.get_provider("zhixingyun", credentials={...})` 要能接受外部凭证，不能只读全局配置文件。

参考 `compshare.py` 怎么处理 `credentials` 参数。

### 4. 不要在注册表里硬编码

`registry.py` 加分支即可，**不要在上层写 `if provider == "zhixingyun"`**。

---

## 四、第一步：先搞清智星云的 API

**当前代码里零痕迹**（`grep -i 智星` 无结果），所以第一步是调研。

### 需要确认的信息

| # | 要查什么 | 为什么 |
|---|---|---|
| 1 | API 基址、认证方式（AK/SK？Token？） | 决定 `__init__` 怎么写 |
| 2 | 建机接口的参数集 | 决定 `create()` 能否满足 |
| 3 | 实例状态枚举 | 映射到 `InstanceView.state`（running/stopped/...） |
| 4 | 计费粒度与出账接口 | `price()` / `balance()` |
| 5 | SSH 连接方式（端口？密码？密钥？） | `ssh_endpoint()` |
| 6 | 是否有定时关机/自动释放 | `scheduled_stop()`，没有就抛 UNSUPPORTED |
| 7 | 镜像体系（预置镜像？自定义？OCI？） | `resolve_image()` / `capabilities.can_pull_oci` |
| 8 | **停机是否收磁盘费** | 最贵的坑，必须实测 |

### 建议动作顺序

```
① 查智星云官网/文档，拿到 API 说明
② 注册账号，拿到测试凭证（⚠️ 不要贴进对话，写进 credentials 文件）
③ 用 curl 手工跑通：查余额 → 列举镜像 → 建最便宜的一台 → 查状态 → 销毁
④ 把 ③ 的请求/响应整理成 fixtures，喂给 FakeProvider 写测试
⑤ 照 compshare.py 写 zhixingyun.py
⑥ 真机验证（建一台最便宜的，全流程走通）
```

**③ 之前不要写代码。** API 长什么样都不知道就写适配器，等于猜。

---

## 五、测试要求

新 provider 必须让这个测试通过：

```bash
python3 -m unittest tools.p2.tests.test_l5_providers
```

它会检查：
- 契约方法是否齐全
- `capabilities()` 返回类型正确
- 错误分类是否符合预期
- BYOK 凭证是否隔离

另外补一个 `test_zhixingyun.py`，用离线 fixture 覆盖：
- `create()` 参数构造正确
- 状态映射表覆盖所有云侧状态
- 错误码分类
- 不支持的能力正确抛 `UNSUPPORTED`

**全程零费用、不联网**（照 `test_l5_providers.py` 的写法）。

---

## 六、验收标准

| # | 标准 | 怎么验 |
|---|---|---|
| 1 | `zhixingyun.py` 实现全部 12 个方法 | `unittest` 契约测试过 |
| 2 | `registry.get_provider("zhixingyun")` 能拿到实例 | 一行 python 验证 |
| 3 | `l5l6 providers` 能力矩阵里能看到智星云 | `python3 tools/p2/l5l6.py providers` |
| 4 | 原有 1023 项测试仍全绿 | 全量跑一遍 |
| 5 | 真机走通一次：建机 → SSH → 销毁 | 留存日志与花费记录 |
| 6 | `capabilities()` 里没有 `None` 未填项（或明确标注「未知，保守处理」） | 人工检查 |

---

## 七、不要做的事

| ❌ | 原因 |
|---|---|
| 在 `main` 上直接改 | AGENTS.md 铁律 1 |
| `git add -A` | AGENTS.md 铁律 2 |
| 改 `compshare.py` 的现有逻辑 | 会影响已跑通的生产路径 |
| 猜 API 参数 | 猜错了调试成本远高于先查文档 |
| 为了让测试过而伪造 capabilities | 上层会按假数据决策，真机必翻车 |
| 在代码里硬编码凭证 | 安全红线 |

---

## 八、参考资料位置

| 内容 | 路径 |
|---|---|
| Provider 契约 | `tools/p2/providers/base.py` |
| 参考实现 | `tools/p2/providers/compshare.py` |
| 假 provider | `tools/p2/providers/local.py` 的 `FakeProvider` |
| 工厂注册 | `tools/p2/providers/registry.py` |
| 契约测试 | `tools/p2/tests/test_l5_providers.py` |
| 能力矩阵 CLI | `python3 tools/p2/l5l6.py providers` |
| L5 完成度审计 | `reports/audit/L4L5L6完成度审计-20261112.md` |
| 协作规矩 | `AGENTS.md` |

---

## 九、完成后

```bash
cd /root/zhiyun-l5-zx
git add tools/p2/providers/zhixingyun.py \
        tools/p2/providers/registry.py \
        tools/p2/tests/test_zhixingyun.py
git commit -m "feat(l5): 接入智星云 provider"
git push -u origin feat/l5-zhixingyun
```

然后开 PR：https://github.com/SolJuly/dispatch/compare/main...feat/l5-zhixingyun
