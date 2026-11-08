# 创意引擎分支审计报告

- 分支 `feature-/creative-engine` · HEAD `30b5a28 2026-11-07 13:46 fix(p1): 动作迁移跑 23 分钟的三件事——帧数循环、服务崩溃傻等、估价按 77 帧`
- 生成于 2026-11-08T00:19:11+08:00
- 相对 main：75 提交 / 366 文件；工作区 32 改 / 35 新增（未提交 git）

## 一、结论

这个分支已是一条能真正跑起来的 AI 出图/出视频流水线：54 工作流接入注册表、有状态机、预算闸门、产物复验、并发去重，真机累计 62 个成功作业。

能力分三档：**13 个可直接交付**、**18 个出过片但估价不准**、**23 个实验性**。本轮审计共修掉 **18 个真实错误**，其中 2 个 P0 会直接白烧 GPU 钱。

## 二、审计发现（18 项，全部已修）

| 级别 | 状态 | 问题与修复 |
|---|---|---|
| P0 | 已修 | **崩溃检测自匹配**：探活命令 pgrep 匹配到 ssh 的 bash 包装行，恒判「活着」，服务 OOM 后白等 20–45 分钟。改 [m]ain\.py + /system_stats 双判据；真机杀进程后 2 秒落 FAILED_RUN。 |
| P0 | 已修 | **trim/估价只认 video 键**：7 个工作流的驱动视频叫 image_2/image，trim_seconds 与真实帧数估价全部静默失效。改按注册表 type==video 取键；563 帧片子估价 ¥0.25→¥1.29，trim 后回落 ¥0.22。 |
| P1 | 已修 | **失败作业被去重复用**：去重按「非 TERMINAL 即进行中」判，而 FAILED_* 不在 TERMINAL，用户重提失败任务会拿到死作业且什么都不执行。改为只复用主链未终态状态。 |
| P1 | 已修 | **webui 取错 job_id**：jobctl 先打印旧作业 id，webui 用 search() 取第一个 → run=True 时去执行死作业。改取最后一个，与 p20ctl 同口径。 |
| P1 | 已修 | **图缓存在同秒改写后失效**：lru_cache 键只有 mtime；本环境同秒同长度改写时 mtime/ctime/size 全不变，duration_s 按旧 fps 算会误杀好产物。缓存键改为内容 sha1。 |
| P1 | 已修 | **freeze 门禁一刀切**：0.2 阈值本是给生成类抓时间维崩坏的，却套到 9 个素材驱动类（动作迁移/超分/插帧/抠像）上，静态素材产静止片被判 FAILED_VERIFY。改为 full_freeze_only：只拦整片冻结，中度静止降级告警。 |
| P1 | 已修 | **ziamge_ae 拼写错误**：共享库原名 ae.safetensors，模型卡写成 ziamge_ae，靠镜像烤的别名软链才没崩。上机确认 inode 后改正，真机复验警告消失。 |
| P1 | 已修 | **提交竞态**：查重与建目录之间无锁，双击提交会各建一个作业各开一台 GPU。加独立 .submit.lock 原子化；真双进程验证只建 1 个。 |
| P1 | 已修 | **frames 默认值违反自身校验**：81/77 不满足 multiple=4，不传参建作业直接被拒。放宽为 4n+1 惯例。 |
| P2 | 已修 | **主产物靠文件名排序**：多路输出时封面 png 可能抢主位，导致整单按图片校验或误杀。改为 output 节点产物打 00-primary- 前缀。 |
| P2 | 已修 | **上传整块进内存 + 双份落盘**：128MB 视频一次性 read 进内存且存两份。改为流式落盘 + 硬链接去重（inode 相同）。 |
| P2 | 已修 | **abort 释放失败仍报成功**：实例销毁异常只打一行警告仍打印「已清理」，会持续计费。改为重试一次并如实报告。 |
| P2 | 已修 | **21 个视频工作流无时长闸门**：只验体积+解码，出 1 秒也算 PASS。从图里推导帧率/帧数，14/27 有了时长窗口（其余数值不唯一，主动放弃）。 |
| P2 | 已修 | **估价与运行上限不自洽**：8× 缩放上限恰好撞 max_minutes，变成跑到超时被杀。提交前增加「预估超过运行上限」拦截。 |
| P2 | 已修 | **测试污染**：test_lock_queue 替换 store.list_jobs 后未还原，导致后续查重测试拿到假列表。已修，并让查重直接遍历真实目录。 |
| P1 | 已修 | **enum 参数校验完全失效**：registry.py:521 读 spec['enum']，但 25 个 enum 参数配置里用 values 键 → 校验形同虚设。实测 resolve('sdxl_t2i', sampler='NO_SUCH_SAMPLER') 直接通过，非法值会写进图送 ComfyUI。改为 values||enum 双兼容。 |
| P1 | 已修 | **trim 残片污染 params**：p20ctl._trim_video 用 out.is_file() 判定已完成，且 ffmpeg -y 先落盘；失败/超时留下 0 字节残片会被当成成品，下次跳过重编码并把坏文件上传进实例。改为非空才算完成 + 写 .part 再原子改名 + 失败清理并保留原素材。 |
| P2 | 已修 | **表达式求值器拒掉真实表达式**：白名单缺 floor/ceil/位运算/比较/BoolOp，54 张图里 14 个真实表达式有 6 个被拒（floor、a & ~1、a > 2048 等），这些工作流推不出 duration_s。已补全白名单；另修根因：_resolve_num 一律返回 float 导致位运算 TypeError，加整数值 float→int。现 14/14 可求值，10 个逃逸样本全拒。 |

## 三、直接可用（13 个）

| 工作流 | 类型 | fps | 能力标记 |
|---|---|---|---|
| `h3_i2v_121f` | video | 24 | 尺寸旋钮 · 时长闸门 · 实测基线 |
| `h3_t2v_121f` | video | 24 | 尺寸旋钮 · 时长闸门 · 实测基线 |
| `ltxv_2b_t2v` | video | 25 | 尺寸旋钮 · 时长闸门 · 实测基线 |
| `pack_video_fastvideo_fasth3_i2v` | video | 24 | 时长闸门 · 实测基线 |
| `pack_video_minimax_h3_multiframe_reference` | video | 24 | 时长闸门 · 实测基线 |
| `pack_video_minimax_h3_r2v` | video | 24 | 时长闸门 · 实测基线 |
| `pack_video_wan2_2_14b_flf2v` | video | 16 | 尺寸旋钮 · 时长闸门 · 实测基线 |
| `pack_video_wan2_2_14b_i2v` | video | 16 | 尺寸旋钮 · 时长闸门 · 实测基线 |
| `pack_video_wan2_2_14b_s2v` | video | 16 | 时长闸门 · 实测基线 |
| `pack_video_wan_animate2` | video | — | 素材驱动 · 实测基线 |
| `pack_video_wan_animate2_distilled` | video | 16 | 素材驱动 · 实测基线 |
| `seedvr2_3b_upscale_video` | video | — | 素材驱动 · 实测基线 |
| `wan21_i2v_480p` | video | 16 | 尺寸旋钮 · 时长闸门 · 实测基线 |

## 三、需先验证（18 个）

| 工作流 | 类型 | fps | 能力标记 |
|---|---|---|---|
| `detail_polish` | image | — | — |
| `flux2_klein_4b_fast` | image | — | 尺寸旋钮 |
| `pack_utility_z_image_turbo_2k_upscaler_app` | image | — | — |
| `qwen_image_edit_2509` | image | — | — |
| `qwen_image_t2i` | image | — | 尺寸旋钮 |
| `qwen_image_t2i_landscape` | image | — | 尺寸旋钮 |
| `qwen_image_t2i_portrait` | image | — | 尺寸旋钮 |
| `sdxl_img2img` | image | — | 尺寸旋钮 |
| `sdxl_inpaint` | image | — | — |
| `sdxl_t2i` | image | — | 尺寸旋钮 |
| `sdxl_t2i_lora` | image | — | 尺寸旋钮 |
| `seedvr2_3b_upscale` | image | — | — |
| `upscale_4x` | image | — | — |
| `zimage_turbo_batch4` | image | — | 尺寸旋钮 |
| `zimage_turbo_controlnet_canny` | image | — | — |
| `zimage_turbo_img2img` | image | — | — |
| `zimage_turbo_t2i` | image | — | 尺寸旋钮 |
| `zimage_turbo_t2i_hq` | image | — | 尺寸旋钮 |

## 三、实验性（23 个）

| 工作流 | 类型 | fps | 能力标记 |
|---|---|---|---|
| `pack_image_flux2_klein_image_edit_4b_distilled` | image | — | — |
| `pack_image_qwen_image_2_1_background_removal` | image | — | — |
| `pack_image_qwen_image_2_1_image_edit` | image | — | — |
| `pack_image_qwen_image_edit_2511_int8` | image | — | — |
| `pack_image_qwen_image_layered` | image | — | — |
| `pack_template_ltx2_3_ic_lora_ingredients` | video | — | 尺寸旋钮 |
| `pack_template_qwen_image_illustration_lora` | image | — | — |
| `pack_templates_hellorob_facegen_skindetail_upscale` | image | — | — |
| `pack_utility_image_upscale_supir` | image | — | — |
| `pack_utility_seedvr2_7b_int8_upscale_image` | image | — | — |
| `pack_utility_void_video_inpainting` | video | — | 尺寸旋钮 · 素材驱动 |
| `pack_video_fasth3_ominiref_r2v` | video | 24 | 时长闸门 |
| `pack_video_ltx2_3_flf2v` | video | — | — |
| `pack_video_ltx2_3_i2v` | video | 25 | 时长闸门 |
| `pack_video_ltx2_3_ic_lora` | video | — | 尺寸旋钮 · 素材驱动 |
| `pack_video_ltx2_3_id_lora` | video | 25 | 时长闸门 |
| `pack_video_ltx2_5_flf2v` | video | — | — |
| `pack_video_wan21_scail2_character_replacement` | video | — | 素材驱动 |
| `pack_video_wan21_scail2_character_replacement_int8` | video | — | 素材驱动 |
| `pack_video_wan_vace_14b_ref2v` | video | — | — |
| `pack_video_wan_vace_14b_v2v` | video | — | 素材驱动 |
| `pack_video_wan_vace_flf2v` | video | 16 | 时长闸门 |
| `pack_video_wan_vace_inpainting` | video | — | 素材驱动 |

## 四、平台侧能力

- 一键建机到出片：创建实例→挂载共享库→自动软链→启动 ComfyUI→提交→轮询→拉取→七项复验→销毁
- 成本控制：提交前估价、预算硬闸门、超运行上限拦截、定时关机、空闲自动关机
- 可靠性：崩溃检测（进程+HTTP 双判据，真机 2 秒识别）、失败保留实例可续跑、abort 释放重试
- 防重复：同参数并发提交幂等复用（真双进程验证只建 1 个作业、只开 1 台机）
- 产物闸门：解码/尺寸/时长/体积/黑帧/冻结/参数回读七项，主产物由 output 节点锁定
- 参数校验：枚举/类型/上下限/倍数/未知键全校验（本轮补齐了失效的枚举校验）

## 五、能力边界（不夸大）

- 15 个视频工作流没有分辨率旋钮（图里无静态 width/height 节点）
- 13 个视频工作流没有时长闸门：逐个核查，全部是 fps 或帧数不可静态求值（GetVideoComponents / GetImageSize / 无 length 节点 / 素材驱动），属合理放弃
- 23 个组合 experimental，默认 409 拒绝
- 9 个素材驱动型 freeze 只拦整片冻结
- 18 个用同族锚点估价，非实测值

## 六、审计自查

| 核查项 | 结论 |
|---|---|
| graph_sha256 与实际图 | 54/54 一致（sha 存 DB，不在 workflow json；改过的 10 个图也匹配） |
| 10 个改过的 json 补的 width/height | 全部与 API 图原值一致 |
| 绑定 / output.node / model.key / constants | 零断链 |
| 9 个素材驱动类判定 | 与 type=video、LoadVideo 完全一致 |
| _default_math_frames 启发式 | 无误用（vace_flf2v 的 512 是分辨率常量，实算 81 帧/5.06s 正确） |
| _eval_num_expr 恶意表达式 | 10 个逃逸样本全部拒绝 |
| 未知参数键 / 缺必填 / 默认值回填 | 均正确拒绝或回填 |

## 七、测试

| 套件 | 用例数 | 状态 |
|---|---|---|
| tools/p20/tests | 104 | OK |
| tools/p1/tests | 51 | OK |
| tools/webui/tests | 58 | OK |
| tools/p2/tests | 47 | OK |
| **合计** | **260** | **全绿** |

## 八、花费

本次审计全程本地静态分析 + 只读复现，未新建 GPU 实例、未产生费用。余额 ¥9.78，运行中实例 0 台。
