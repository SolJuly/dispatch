# AutoDL Provider 接入 · 交接单

> 建立：2026-11-13 19:05
> 分支：`feat/l5-autodl`
> 工作树：`/root/zhiyun-l5-autodl`
> 目标：L5 多云第二家 —— AutoDL（算力云）

---

## 〇、为什么是 AutoDL，不是智星云

智星云那条线（`feat/l5-zhixingyun`，HEAD `8e1f05f`）已停在「交接单写完、尚未调研 API」的状态，**因阻碍较大暂缓**。分支保留，随时可回。

选 AutoDL 的理由（来自 `reports/audit/L4L5L6完成度审计-20261112.md`）：

| 维度 | AutoDL | 说明 |
|---|---|---|
| 停机免费 | ✅ | 最像智云，停机不收算力费 |
| API 文档 | ✅ 公开完整 | 不用猜 |
| 价格 | 便宜 | 适合做第二家 |
| 生态 | 国内主流 | 出问题有资料 |

---

## 一、起点状态

```
主树     /root/智云视频工作流       0c677e1 [main]
本工作树 /root/zhiyun-l5-autodl    0c677e1 [feat/l5-autodl]   ← 从这里开始
测试基线 1023 项全绿
```

**注意**：本分支从 main 切出，**没有**智星云的交接单（那是另一条线的内容）。两家可以并行推进，互不干扰。

---

## 二、要做什么

在 `tools/p2/providers/` 下新增 `autodl.py`，实现 `Provider` 契约，并在 `registry.py` 注册。

### 已有资产（照抄对象）

| 文件 | 行数 | 作用 |
|---|---|---|
| `providers/base.py` | 301 | **契约定义**，必须实现的方法都在这 |
| `providers/compshare.py` | 210 | **最像的参考实现**，优云智算适配器 |
| `providers/local.py` | 252 | 本地/假 provider，含 `FakeProvider` 供测试 |
| `providers/registry.py` | 121 | 工厂 + 能力矩阵，要在这里加 `autodl` |
| `tests/test_l5_providers.py` | — | 契约一致性测试，新 provider 必须过 |

### 必须实现的方法（12 个）

```python
class AutoDLProvider(Provider):
    name = "autodl"

    def capabilities(self) -> Capabilities: ...
    def create(self, *, image, gpu_type, gpu, cpu, memory_gb,
               disk_gb, data_disk_gb, name, charge_type,
               zone, password, remark) -> str: ...
    def get(self, uid) -> InstanceView: ...
    def list_instances(self, prefix="") -> list[InstanceView]: ...
    def start(self, uid) -> None: ...
    def stop(self, uid) -> None: ...
    def terminate(self, uid) -> None: ...
    def ssh_endpoint(self, uid) -> SSHEndpoint: ...
    def ssh_ready(self, uid, *, timeout=180) -> SSHEndpoint: ...
    def scheduled_stop(self, uid, at_epoch) -> None: ...
    def price(self, *, gpu_type, gpu, cpu, memory_gb,
              disk_gb, data_disk_gb, charge_type, image, zone) -> dict: ...
    def balance(self) -> float: ...
    def resolve_image(self, spec) -> dict: ...
    def images(self, keyword="") -> list[dict]: ...
    def stock(self, gpu_type, *, spot=False, zone=None) -> int: ...
```

`wait_state()` 有默认实现，不用重写。

---

## 三、第一步：调研 AutoDL API（**先不写代码**）

智星云的教训：不知道 API 长什么样就写适配器，等于猜。

### 已知信息（需要你去核实）

AutoDL 的 API 有几个特点，**必须先确认**：

| # | 要查什么 | 为什么重要 |
|---|---|---|
| 1 | **认证方式** | AutoDL 用 `token`（不是 AK/SK）。在哪拿？ |
| 2 | **API 基址** | `api.autodl.com`？还是别的 |
| 3 | **建机接口参数** | 能否指定 GPU 型号、镜像、磁盘？ |
| 4 | **实例状态枚举** | 映射到我们的 `running/stopped/...` |
| 5 | **计费接口** | 有没有余额查询、消费明细 |
| 6 | **SSH 方式** | 端口？密码？密钥？AutoDL 有个「自定义服务」概念要搞清 |
| 7 | **定时关机** | 是否支持？不支持就抛 UNSUPPORTED |
| 8 | **镜像体系** | 官方镜像？社区镜像？私有镜像？ |
| 9 | **停机是否收费** | ⚠️ 最贵的坑，必须实测 |
| 10 | **实例内有无外网** | ⚠️ 影响 provision.sh / pip / docker pull |

### 关键提醒

**AutoDL 与智云的语义差异**（这些会直接影响实现）：

| 概念 | 智云 | AutoDL（待确认） |
|---|---|---|
| 计费单位 | 按小时 | 可能按分钟/秒 |
| 关机 | GPU/CPU/内存免费，磁盘可能收费 | 需实测 |
| 磁盘 | 系统盘 ≤100G 免费 | 可能按容量收费 |
| 镜像 | 平台镜像 + 自定义 | 可能有「社区镜像」体系 |
| SSH | 端口 23，root + 密码 | ⚠️ 可能要先进「容器实例」 |

**如果发现某能力 AutoDL 没有**，正确做法是：

```python
def scheduled_stop(self, uid, at_epoch):
    raise ProviderError(
        "AutoDL 不支持定时关机，上层需自行用 cron 兜底",
        ProviderError.UNSUPPORTED)
```

**不要假装支持然后空实现。**

---

## 四、设计约束

### 1. 错误必须正确分类

`ProviderError` 分三类，写错会烧钱或漏重试：

| kind | 什么情况 | 上层行为 |
|---|---|---|
| `TRANSIENT` | 网络抖动、无货、正在同步 | **重试** |
| `PERMANENT` | 参数错、余额不足、镜像不存在 | **不重试** |
| `UNSUPPORTED` | 该云没这能力 | 走降级路径 |

参考 `compshare.py` 的 `_classify()` 和 base.py 的 `classify()`。

### 2. 能力声明要诚实

```python
Capabilities(
    name="autodl",
    can_stop_instance=True,           # 待实测
    bills_disk_when_stopped=None,     # ⚠️ 必须实测，猜错会算错钱
    instance_has_internet=None,       # ⚠️ 影响 provision.sh 能否跑
    can_pull_oci=False,
    has_shared_weight_mount=False,    # AutoDL 应该没有 /model 共享库
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

```python
get_provider("autodl", credentials={"token": "..."})
```

必须能接受外部凭证，不能只读全局配置文件。

### 4. 不要在注册表里硬编码

`registry.py` 加分支即可，**不要在上层写 `if provider == "autodl"`**。

---

## 五、建议动作顺序

```
① 查 AutoDL 官方 API 文档，整理成摘要写入 reports/l5/autodl-api-notes.md
② 注册账号，拿 token（⚠️ 不贴进对话，写进 credentials 文件）
③ curl 手工跑通：查余额 → 列镜像 → 建最便宜一台 → 查状态 → 停机 → 销毁
④ 把 ③ 的请求/响应整理成 fixtures（脱敏）
⑤ 照 compshare.py 写 autodl.py
⑥ 写离线测试（不联网、零费用）
⑦ 真机验证（建一台最便宜的，全流程走通）
⑧ 推分支、开 PR
```

**③ 之前不要写代码。**

---

## 六、测试要求

新 provider 必须让这个测试通过：

```bash
python3 -m unittest tools.p2.tests.test_l5_providers
```

再补 `test_autodl.py`，用离线 fixture 覆盖：

- `create()` 参数构造正确
- 状态映射表覆盖所有云侧状态
- 错误码分类
- 不支持的能力正确抛 `UNSUPPORTED`
- BYOK 凭证不泄漏到全局

**全程零费用、不联网**（照 `test_l5_providers.py` 的写法）。

---

## 七、验收标准

| # | 标准 | 怎么验 |
|---|---|---|
| 1 | `autodl.py` 实现全部 12 个方法 | 契约测试过 |
| 2 | `registry.get_provider("autodl")` 能拿到实例 | 一行 python 验证 |
| 3 | `l5l6 providers` 能力矩阵里能看到 AutoDL | `python3 tools/p2/l5l6.py providers` |
| 4 | 原有 1023 项测试仍全绿 | 全量跑一遍 |
| 5 | 真机走通一次：建机 → SSH → 销毁 | 留存日志与花费记录 |
| 6 | `capabilities()` 里没有未说明的 `None` | 人工检查或写进 notes |
| 7 | `reports/l5/autodl-api-notes.md` 存在 | 调研成果留档 |

---

## 八、不要做的事

| ❌ | 原因 |
|---|---|
| 在 `main` 上直接改 | AGENTS.md 铁律 1 |
| `git add -A` | AGENTS.md 铁律 2 |
| 改 `compshare.py` 现有逻辑 | 会影响已跑通的生产路径 |
| 猜 API 参数 | 猜错了调试成本远高于先查文档 |
| 伪造 capabilities 让测试过 | 上层按假数据决策，真机必翻车 |
| 硬编码 token | 安全红线 |
| 动 `feat/l5-zhixingyun` 那条线 | 那是独立分支，别混 |

---

## 九、参考资料

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

## 十、完成后

```bash
cd /root/zhiyun-l5-autodl
git add tools/p2/providers/autodl.py \
        tools/p2/providers/registry.py \
        tools/p2/tests/test_autodl.py \
        reports/l5/autodl-api-notes.md
git commit -m "feat(l5): 接入 AutoDL provider"
git push -u origin feat/l5-autodl
```

开 PR：https://github.com/SolJuly/dispatch/compare/main...feat/l5-autodl

---

## 十一、当前工作树全景（避免混淆）

```
/root/智云视频工作流    0c677e1 [main]                     ← 发布树，永远停 main
/root/zhiyun-l5-autodl  0c677e1 [feat/l5-autodl]           ← 本交接单，从现在开始
/root/zhiyun-l5-zx      8e1f05f [feat/l5-zhixingyun]       ← 智星云线，暂缓
/tmp/zxy-base           8e1f05f (detached)                 ← 未知来源，无改动，未处理
```

**只在本目录（`/root/zhiyun-l5-autodl`）工作，不要碰其他目录。**
