# 动作迁移提示词（按你的人物图定制）

> 2026-11-08 · 对应工作流 `pack_video_wan_animate2_distilled`
> 素材：黄衣女生照片（1280×2463）+ 驱动视频

## 为什么要改

工作流默认提示词描述的是**男性街舞人偶（male street dancer action figure）**：

```
Character appearance description: A realistic premium collectible action figure of
a male street dancer, lifelike matte vinyl and resin material... black hoodie,
dark cargo pants, white sneakers... rainy neon-lit city street corner...
```

而实际素材是**真人女生、黄衣、室内**。提示词和素材完全对不上，
模型在「按提示词生成玩偶」和「跟随驱动视频动作」之间冲突，
结果妥协成「人物基本不动，只做微弱变化」。

---

## 改这两个就够（其他保持默认）

### `prompt` 和 `prompt_4`

**这两个填一样的内容**（分别对应阶段 1 / 阶段 2 的正向）。

```
Character appearance description: A young Asian woman with long straight black hair and side-swept bangs, fair pale skin with soft rosy blush, large dark expressive eyes, full pink lips. Wearing a yellow blue and green plaid off-shoulder top with a twist-front design, and a delicate silver butterfly necklace with a small cross pendant. Upper body framed from the chest up, facing the camera, natural relaxed posture, photorealistic human, detailed skin texture, individual hair strands.
Background description: dim indoor setting with a dark wooden door behind her, soft low-key ambient lighting, shallow depth of field.
```

### `prompt_2` 和 `prompt_5`

**这两个也填一样的内容**（阶段 1 / 阶段 2 的姿态描述）。

```
A young woman dancing naturally to music, fluid body movements, swaying shoulders and hips, expressive arm gestures, long hair swinging with the motion, lively natural facial expressions.
```

### `neg_prompt` / `prompt_3`

**不要改。** 默认值是一套通用的避坑词（防手部畸形、画面静止、糊、水印），
换了反而容易出问题。

---

## 关键改动对照

| 项 | 原默认值 | 新值 |
|---|---|---|
| 主体 | male street dancer（男性街舞者） | young Asian woman（年轻亚裔女性） |
| 材质 | matte vinyl / resin（哑光vinyl、树脂） | photorealistic human（写实真人） |
| 衣着 | black hoodie / cargo pants / sneakers | 黄蓝绿格纹一字肩上衣（照你的图） |
| 配饰 | 无 | silver butterfly necklace with cross pendant（照你的图） |
| 背景 | rainy neon city street（雨夜霓虹街） | dim indoor with dark wooden door（室内深色门） |
| 构图 | full body head to toe | upper body chest up（上半身，动作更明显） |

---

## 如果动作还是不明显

按顺序试这几招：

1. **降低 `cfg`**（如果工作流暴露了它）。低 cfg 更贴合输入素材，
   不容易被提示词「带偏」成玩偶。

2. **加大驱动视频的动作幅度**。动作迁移的动作主要来自驱动视频的 pose，
   提示词只是辅助。你现在用的是截 4 秒（16fps→64 帧），
   如果原视频前 4 秒动作幅度小，可以：
   - 把 `trim_seconds` 改成 0（用整条，但更贵更慢）
   - 或者先用剪辑软件把动作最大的片段剪成 4 秒再上传

3. **`prompt_2` / `prompt_5` 干脆留空**。动作完全交给驱动视频，
   避免提示词干扰。

---

## 已验证

- `p20ctl resolve` 通过：74 节点 · 无残留占位符
- 提示词正确注入 4 个节点（1609/1612/1649/1652）
- 参数校验通过（frames=81, trim_seconds=4 合法）
