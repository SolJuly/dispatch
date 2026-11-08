# 视频引擎分支审计 · feature-/creative-engine

- 审计时间：2026-11-07 15:00–17:30（Asia/Shanghai）
- 审计对象：分支 `feature-/creative-engine`（HEAD `30b5a28`）相对 `main` 的全部改动
- 审计方式：逐提交 diff 审读 + 绑定一致性脚本 + 163 个单测 + 本地复现实验
- 结论一句话：**主干能跑、注册表零断链、测试全绿；但最近三次"视频引擎修复"里有两个 P0 在主力模板上失效，且 27 个视频工作流里有 21 个的质量闸门是空的。**

---

## 0. 范围与方法

| 项 | 数值 |
|---|---|
| 分支独有提交 | 75 |
| 变更文件 | 366（+60004 / −100） |
| 注册表工作流 | 54（video 27 / image 27），参数 282 个 |
| 模型条目 | registry/models 若干（含 softlink 定义） |
| 单测 | 163 条全绿（p20 104 / p1 4 / webui 55） |
| 编译检查 | `python3 -m compileall fileserver.py webui tools/p1 tools/p20` 退出码 0 |

视频引擎边界（本次审计范围）：`tools/p1/run.py`（作业状态机与实例编排）、`tools/p1/verify.py`、`tools/p1/state.py`、`tools/p20/{p20ctl,render,config_schema,comfy,assets}.py`、`webui/{api,budget,assets,pages}.py`、`fileserver.py`、`registry/workflows|models`。

---

## 1. 结论摘要

| # | 级别 | 问题 | 影响 | 证据 |
|---|---|---|---|---|
| F1 | **P0** | 崩溃检测 `pgrep -af 'ComfyUI.*main.py'` 命中的是 ssh 的 shell 包装进程，不是 ComfyUI | 检测恒为"活着"，崩溃后仍白等 40 分钟；极端情况下反向误杀 | `tools/p1/run.py:571-578` |
| F2 | **P0** | `trim_seconds` 与"按真实帧数估价"只认参数名 `video` | 蒸馏版动作迁移（参数名 `image_2`）两项收益全部失效，仍是 563 帧 / 7 轮 / 20+ 分钟 | `tools/p20/p20ctl.py:280`、`webui/budget.py:61`、`registry/workflows/pack_video_wan_animate2_distilled.json:120` |
| F3 | P1 | 27 个视频工作流里 21 个没有 `duration_s` 期望 | 时长/帧数不符不判失败，只查 decode/体积/黑帧/冻结 | `tools/p1/verify.py:204-210` |
| F4 | P1 | 主产物 = "文件名排序第一"，`kind` 也由首路后缀决定 | 多路输出顺序一变，封面 png 会让整单走图片校验（冻结/黑帧全跳过） | `tools/p1/verify.py:302,314,321`；`tools/p1/run.py:590-597` |
| F5 | P1 | `MEASURED` 只有 13 条基线，9 个视频工作流回落 `DEFAULT_REF` | LTX/VACE/SCAIL/VOID 的估价误差最大可到数倍 | `webui/budget.py:19-34,78` |
| F6 | P2 | 8× 上限 × 默认 300s = 40min，恰好等于 `max_minutes`，没有一致性校验 | 帧数再多一点就是"超时处死"而不是"超预算拦截" | `webui/budget.py:78`、`registry/workflows/*.json:limits` |
| F7 | P2 | 上传一次性 `read(length)` 进内存（上限 128MB），无 socket 超时 | 并发上传顶内存；慢连接可长期占线程 | `fileserver.py:657-663` |
| F8 | P2 | 上传写两份（`reports/p2/p21/inputs` + `share/inputs`），且 `share/` 是全仓副本 | 128MB 视频双份落盘；部署面存在重复源 | `webui/api.py:150-152` |
| F9 | P2 | `_probe_frames` 用 `-count_frames` 全量解码 | 1080p 长片每次估价都要真解码一遍，阻塞式 | `webui/budget.py:43-57` |
| F10 | P2 | abort 后 `step_release` 抛异常只打一行 `⚠ 销毁异常`，紧接着仍打印"已放弃并清理" | 实例漏释放且日志显示成功，持续计费 | `tools/p1/run.py:1045-1052` |
| F11 | P3 | 19 个视频工作流 `fps=None`，`_recalibrate_duration` 直接 return | 即便补了 `duration_s` 也不会随 frames 自适应 | `tools/p20/render.py:245-247` |
| F12 | P3 | `_trim_video` / `_comfy_alive` / `_scale` 帧数探测 / 多产物判定 **零单测** | 上述 P0 没有任何一条测试兜住 | `tools/*/tests` 内无命中 |
| F13 | P3 | 18 个视频工作流只暴露 `prompt/seed/图片`，没有尺寸/时长旋钮 | 与"视频引擎升级：可调分辨率与时长"的目标不一致 | 注册表参数表 |
| F14 | P1 | 3 个工作流的 `frames` 默认值（81/77）违反自己的 `multiple: 4` 校验 | 不显式传 frames 时建作业直接被拒；传什么都得先绕过校验 | `registry.py:533`、`pack_video_wan2_2_14b_flf2v|s2v`、`pack_video_wan_animate2_distilled` |

---

## 2. P0 详情

### F1 · 崩溃检测整段失效（复现过）

`tools/p1/run.py:571-578`：

```python
def _comfy_alive(host, port, pwd) -> bool | None:
    rc, out = _ssh_run(host, port, pwd,
                       "pgrep -af 'ComfyUI.*main.py' | head -3; echo '---'; "
                       "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader",
                       timeout=60)
    if rc != 0 and "pgrep" not in (out or ""):
        return None
    if "main.py" in (out or ""):
        return True
    return False
```

三个叠加缺陷：

1. **自匹配**：`_ssh_run` 走 `ssh root@host <cmd>`（`run.py:159-165`），远端由 `bash -c` 执行，该 shell 的 cmdline 里就含有字面量 `ComfyUI.*main.py`，`pgrep -af` 会把它列出来（本地已复现：`bash -c pgrep -af 'ComfyUI.*main.py' ...` 自己被匹配）。于是 `"main.py" in out` 恒真 → **永远返回 True**。
2. **正则在真实进程上命不中**：ComfyUI 实际启动命令是 `python -u main.py --listen 0.0.0.0 --port 8188`（`docker/start-comfyui.sh:10`，`lab/state.json` 里也是同形），cmdline 里没有 `ComfyUI` 这段路径前缀。
3. `| head -3` 会把真实进程挤出前三行，进一步保证"看不到真进程"。

后果：提交作业跑到第 7 轮 OOM 崩掉后，`step_wait` 仍然认为服务活着，一路等到 `max_minutes`（40 分钟）才超时——**这正是 30b5a28 想修的那个问题，修完仍然存在**。

修复（三行）：

```python
"pgrep -af '[m]ain\\.py --listen' | head -5; echo '---'; "
"curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/system_stats"
```

判定逻辑改成：`[m]` 防自匹配；端口探活返回 200 才算活；两者都失败才判死，只失败一项返回 `None`（继续等）。

### F2 · `trim_seconds` 与真实帧数估价只认 `video` 键

- `p20ctl._trim_video`（`:280`）的键名正则：`re.fullmatch(r"video(?:_\d+)?", k)`
- `budget._scale`（`:61`）的探测条件：`if not frames and params.get("video")`

而注册表里 9 个"视频输入"工作流，参数名分布是：

| 参数名 | 工作流 |
|---|---|
| `video` | `pack_video_wan_animate2`、`seedvr2_3b_upscale_video` |
| `image_2` | `pack_video_wan_animate2_distilled`、`pack_video_wan_vace_14b_v2v`、`pack_video_wan_vace_inpainting`、`pack_utility_void_video_inpainting` |
| `image` | `pack_video_ltx2_3_ic_lora`、`pack_video_wan21_scail2_character_replacement(_int8)` |

后果：

- 蒸馏版动作迁移（描述写的是"快"，实际是主推）**trim 静默失效**，`trim_seconds` 写进 job.json 却不截取，仍是 563 帧 → 7 轮。
- 同一模板估价：`params` 里自带 `frames=77` 默认值，`_scale` 先取到 77 就再也不探测，**永远按 77 帧报价**，实际 563 帧。
- VACE / SCAIL / LTX-IC-LoRA / VOID 同理：既拿不到 trim，也拿不到真实帧数。

修复：把"哪些参数是视频"收敛成注册表事实，而不是靠键名猜：

```python
video_keys = {k for k, v in (registry.resolve(...)["workflow"]["params"]).items()
              if v.get("type") == "video"}
```

`_trim_video` 与 `_scale` 都改成吃这个集合；同时把 `_scale` 的顺序改成"能 probe 就先 probe，`frames` 只作兜底"。

---

## 3. P1 详情

### F3 · 20/27 视频工作流没有时长窗口

`verify._check_frames` 在 `expect.duration_s` 缺省时直接 `skipped`（`verify.py:204-210`）。实测 27 个视频工作流里只有 6 个带 `duration_s`：

```
有尺寸参数 6 / 有时长窗口 6 / 有帧数参数 8   （共 27）
```

21 个 `pack_video_*`（LTX-2.3 全家、H3 全家、VACE 全家、SCAIL2、FASTH3、Animate2、VOID、IC-LoRA ingredients）只剩 `decode + min_bytes + black + freeze` 四道，其中 `black` 也因 `black_ratio_max` 缺省而 skipped。等价于：**只要产出一个 >50KB 的能解码 mp4，就算 PASS**，出 1 秒也算过。

修复：按 `CreateVideo.fps` 与 `length/frames` 自动推导 `duration_s`（`render.expect_for` 已有入口，只差数据），批量给 21 个工作流补 `expect_defaults.duration_s`。

### F4 · 主产物判定靠文件名排序

`run.py:590-597` 用 `f"{idx:02d}-{fn}"` 落盘，顺序来自 ComfyUI history 的输出顺序；`verify.py:302` 再 `sorted()`，于是"第一路"= 历史返回的最先一个。`verify.py:314` 又用 `files[0].suffix` 决定 `kind`。

一旦模板多一路 png 封面（动作迁移历史上就是 4 路），会出现两种坏结果：

- 封面排第一 → 整单按 `kind=image` 校验，冻结/黑帧全部跳过，**冻结视频被放行**；
- 封面排第一且体积小于 `min_bytes` → 整单 FAIL，**好视频被误杀**（30b5a28 声称已修，实际只修了"非首路"的情况）。

修复：`kind` 取 `wf.output.kind`；primary 取 `output.node` 对应的那一份（落盘时就把 `output` 节点产物重命名成 `00-primary-*`）。

### F5 · 估价基线覆盖不足

`MEASURED` 13 条，27 个视频工作流里 9 个没基线：`pack_video_ltx2_3_*`(5)、`pack_video_wan_vace_*`(4 里的 3 个)、`pack_video_wan21_scail2_*`(2)、`pack_utility_void_video_inpainting`。它们统一回落 `DEFAULT_REF=(300s, 81f, 640²)`，再乘一个最多 8× 的缩放系数——**基线本身就不是这批模型的实测值**，缩放系数再准也是错的。

修复：从 `reports/p2/p2*/specs/*.json` 里回归出每条工作流的实测秒数，补进 `MEASURED`；缺失的标 `measured=false` 并在 UI 上显示"估算"。

### F6 · 预算与超时不自洽

`_scale` 上限 8.0，默认基线 300s → 上限 2400s = 40min，恰好等于多数视频工作流的 `limits.max_minutes=40`。也就是说"最坏情况"落在超时线而不是预算线，用户看到的是"跑满 40 分钟被杀"而不是"提交前被拦"。

修复：加断言 `8.0 * ref_seconds <= max_minutes * 60`，或在 `check_budget` 里同时校验 `est.seconds/60 <= max_minutes`。

---

## 4. 已验证 OK 的部分（不必返工）

1. **注册表零断链**：54 个工作流 / 282 个参数，绑定的节点与输入名全部存在于对应 API 图；`output.node` 全部命中；`model.key` 全部存在。
2. **9 个 `type=video` 参数全部绑到 `LoadVideo`**（533cecc 那批修复是真的）。
3. **`RUNNING → ABORTED` 在白名单里**（`state.py:75-76`），`--abort` 落到 ABORTED 的修复成立。
4. **163 个单测全绿**，编译检查干净。
5. **上传类型识别 5 类魔数 + 128MB 上限与 fileserver 同源**（`webui/api.py:37-43,112-134` ↔ `fileserver.py:657-663`），`max_upload_bytes()` 有测试。
6. **详情页"放大到 1080p"传的是仓库内相对路径**（`webui/pages.py:283`），`_normalize_image` 能命中并归一成 `in-<hash>.mp4`，不是 URL 直传，链路成立。
7. **`expect_for` 的 `_recalibrate_duration`** 在 `frames` 被覆盖时会重算窗口（`render.py:237-264`），h3 241 帧误杀那类问题的机制已修。

---

## 5. 修复顺序建议

| 顺序 | 动作 | 涉及 | 预估 |
|---|---|---|---|
| 1 | F1 崩溃检测改成 `pgrep '[m]ain\.py --listen'` + `/system_stats` 探活 | `run.py:571` | 20 行 |
| 2 | F2 视频参数集合改为读注册表 `type==video`，`_scale` 先 probe 后 frames | `p20ctl.py:280`、`budget.py:59-78` | 40 行 |
| 3 | F3 批量补 20 条 `duration_s`（按 `CreateVideo.fps` 推导） | `registry/workflows/*.json` | 脚本 |
| 4 | F4 primary 走 `output.node`，落盘加 `00-primary-` 前缀 | `run.py`、`verify.py` | 30 行 |
| 5 | F5 补 9 条 `MEASURED` 基线（从历史 specs 回归） | `budget.py` | 数据 |
| 6 | F12 给 F1/F2/F4 各补一条单测（含 pgrep 自匹配的假阳性用例） | `tools/*/tests` | 3 条 |
| 7 | F7/F8/F10 收尾（读流分块、单份落盘、release 失败重试） | `fileserver.py`、`webui/api.py`、`run.py` | 半天 |

---

## 6. 复核命令

```bash
cd /root/智云视频工作流

# 单测
python3 -m unittest discover -s tools/p20/tests -t .
python3 -m unittest discover -s tools/p1/tests -t .
python3 -m unittest discover -s tools/webui/tests -t .

# 绑定一致性（本次审计脚本的等价命令）
python3 - <<'PY'
import json,glob,os
models={os.path.basename(p)[:-5] for p in glob.glob('registry/models/*.json')}
bad=[]
for p in sorted(glob.glob('registry/workflows/*.json')):
    r=json.load(open(p)); g=json.load(open(r['graph']))
    for k,v in (r.get('params') or {}).items():
        for b in v.get('bindings') or []:
            if str(b['node']) not in g: bad.append((r['key'],k,b['node']))
    if str((r.get('output') or {}).get('node')) not in g: bad.append((r['key'],'output',''))
    if (r.get('model') or {}).get('key') not in models: bad.append((r['key'],'model',''))
print('断链', len(bad), bad[:10])
PY

# F1 复现：包装进程自匹配
bash -c "pgrep -af 'ComfyUI.*main.py' | head -3"   # 会看到自己这条 bash
```

---

## 7. 附：27 个视频工作流的闸门覆盖

| 工作流 | 尺寸期望 | 时长窗口 | 帧数参数 | fps |
|---|---|---|---|---|
| h3_t2v_121f / h3_i2v_121f / ltxv_2b_t2v / wan21_i2v_480p | ✅ | ✅ | ✅ | ✅ |
| pack_video_wan2_2_14b_i2v / flf2v | ✅ | ✅ | ✅ | ✅ |
| pack_video_wan2_2_14b_s2v | — | — | ✅ | ✅ |
| pack_video_wan_animate2_distilled | — | — | ✅ | ✅ |
| 其余 19 个 `pack_video_*` / `pack_template_*` / `pack_utility_*` | — | — | — | — |

> 这 19 个既没有尺寸期望、也没有时长期望、也没有可调帧数参数：审核只能看"有没有出文件"。


---

## 8. 修复记录（两轮，2026-11-07 17:00–18:10）

全部改动只碰引擎、注册表与上传链路，未提交 git。

### 第一轮（P0/P1 主干）

| 编号 | 状态 | 改动 | 落点 |
|---|---|---|---|
| F1 | ✅ | 探活改 `pgrep -af '[m]ain\.py --listen'` + `/system_stats` 双判据；解析抽成 `_parse_comfy_alive`，跳过 pgrep/bash 包装行 | `tools/p1/run.py:571-620` |
| F2 | ✅ | 视频参数集合由注册表 `type=="video"` 提供；`_trim_video` 与预算探测共用；估价改为"先 probe 真实帧数，frames 只兜底" | `tools/p20/p20ctl.py:236-306`、`webui/budget.py:59-120` |
| F3 | ✅ 部分 | 从 API 图推导帧率/帧数（支持 ComfyMathExpression、`4n+1` 惯例），`expect_for` 自动补 `duration_s`；新增 8 个（6→14/27），其余 13 个图里数值不唯一或缺 `expression` | `tools/p20/render.py:214-360` |
| F4 | ✅ | 记录 `output` 节点 → 主产物打 `00-primary-` 前缀；`kind` 读工作流声明；无前缀的 legacy 作业按 `expect.ext` 选主产物 | `tools/p1/run.py:500-625`、`tools/p1/verify.py:314-350` |
| F6 | ✅ | `check_budget` 拦截"预估时长 > limits.max_minutes" | `webui/budget.py:check_budget` |
| F10 | ✅ | abort 释放失败重试一次；仍失败提示人工销毁，不再打印成功 | `tools/p1/run.py:1040-1065` |
| F14 | ✅ | `frames` 校验允许 `4n+1`（81/77 合法），其余值仍拒 | `tools/p20/registry.py:533-540` |

### 第二轮（此前标"未做"的全部补上）

| 编号 | 状态 | 改动 | 落点 |
|---|---|---|---|
| F5 | ✅ 机制+披露 | `estimate()` 返回 `basis`：`measured` / `family` / `default`；新增 `FAMILY_REF`（ltx2_3/ltx2_5/ltx/scail/vace/animate/minimax_h3/fasth3 各取同族已实测锚点）；hint 明示"未实测，按同族基线估算"；真机数字回填 fastvideo 164s、animate2 172s | `webui/budget.py:19-45,110-160` |
| F5+ | ✅ 工具 | `scripts/harvest_budget_baselines.py`：扫 `reports/p1/jobs/*/run.log` + `job.json`，回归出 `MEASURED` 条目，`--write` 直接写回（只改已存在的键） | `scripts/harvest_budget_baselines.py` |
| F7 | ✅ | 上传改为流式：8MB 内存池（超出落磁盘）+ 64KB 分块读；multipart 只切一个 file 部分，1MB 流式写临时文件再入库；handler 加 `timeout=120` | `fileserver.py:321-324,657-720`、`webui/api.py:137-200` |
| F8 | ✅ | 作业目录与 share 目录改为**硬链接**（同盘零额外空间），跨盘才 `copy2`；`save_upload_file` 先哈希再落盘，超过上限在拷贝前就拒 | `webui/api.py:_drop_copy` |
| F9 | ✅ | `_probe_frames` 先读容器 `nb_frames`，读不到才 `-count_frames` | `webui/budget.py:43-62` |
| F11 | ✅ | 7 个视频工作流的 `fps` 落库（ltx2_3 i2v/id_lora=25、h3 系=24、vace_flf2v=16…），注册表重建 | `registry/workflows/*.json`、`p20ctl import` |
| F13 | ⚠️ 部分 | 3 个有静态尺寸节点的工作流新增 `width/height` 参数（ltx2_3_ic_lora、ic_lora_ingredients、void_video_inpainting，含双节点绑定），默认值等于原图值、`multiple=32`，`expect` 自动带尺寸门禁；其余 15 个由输入素材决定尺寸，属图结构问题 | `registry/workflows/*.json` |

### 测试与验证

```text
tools/p20/tests  104 tests OK
tools/p1/tests    31 tests OK   （原有 4 + 新增 27）
tools/webui/tests 55 tests OK
python3 -m compileall fileserver.py webui tools scripts   exit 0
p20ctl validate  54 工作流 / 49 模型 / verified 31 / experimental 24 / incompatible 0  全部通过
绑定一致性脚本  断链 0
```

新增用例覆盖：包装行自匹配判死、真进程/HTTP 判活、ssh 不通返回 None、`image_2` 识别为视频、trim 命中与不误伤、真实帧数压过模板默认、`00-primary-` 优先、封面不降级 kind、legacy 按扩展名选主产物、25fps/5.04s 图推导、循环模板主动放弃、`4n+1` 帧数校验、硬链接去重、超限前置拒绝、`basis` 三态、超时拦截、新宽度参数解析。

### 端到端抽检

```text
蒸馏版动作迁移 trim_seconds=2：image_2 → in-<hash>-t2-16fps.mp4（4s/30fps → 2s/16fps）
ic_lora 新增参数：resolve 出 width=1280 height=704，expect 带 width/height 门禁，渲染 40 节点无残留占位符
上传（真 HTTP，curl multipart）：POST /<token>/api/p2/uploads → 201
  {"name":"in-8377bb60.mp4","bytes":16806}
  reports/p2/p21/inputs/in-8377bb60.mp4 与 share/inputs/in-8377bb60.mp4
  → inode 52381 相同、nlink=2（硬链接，不再是两份拷贝）
```

## 9. 仍然存在的限制（说清楚，不含糊）

1. **15 个视频工作流的尺寸由输入素材决定**：`fasth3`、`ltx2_3_flf2v/i2v`、`ltx2_5_flf2v`、`minimax_h3`、`scail2`、`vace_v2v/flf2v/inpainting`、`animate2` 等图里没有静态 width/height 节点，要暴露旋钮必须**在图里加缩放节点**（改图，不是改配置）。要做得单独立项。
2. **13 个视频工作流的时长仍无闸门**：帧率或帧数在 `ComfyMathExpression` 里缺 `expression` 或随输入变化（循环批处理），推导时主动放弃，宁缺勿错。
3. **F5 的 9 条真机基线还差数据**：同族基线只能保证"量级对"，精确值要跑一次真机后用 `harvest_budget_baselines.py --write` 回填。
4. **F1/F2 需要真机复验**：`pkill -f 'main.py --listen'` 后作业应在 30 秒内落 `FAILED_RUN`；563 帧驱动视频估价应从 ≈¥0.2 抬到 ≈¥0.9。
