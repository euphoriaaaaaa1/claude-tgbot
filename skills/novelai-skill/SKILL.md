---
name: novelai-skill
description: 当用户在聊天里要求发图、发照片时使用。适用于自拍、同框、指定场景、NSFW、生图续发和“再来一张/还想再看”这类续图说法；不适用于讨论按钮、规则、接口或触发词本身。
compatibility:
  - Bash
  - Python 3
  - Local files
---

# NovelAI 生图 Skill

## 目的
把聊天里的生图请求，转换成一次稳定的 NovelAI 4.5 full 出图流程，并始终保留配置里的固定正面前缀和负面前缀。

## 什么时候触发
- 用户明确说“帮我画一张”“来一张图”“生成一张图”
- 用户直接说“看看你”“自拍”“拍一个”“按这个场景来一张”
- 用户已经描述了人物、动作、场景、镜头，希望你出图
- 用户基于上一张继续要图，比如“再来一张”“再发一张”“还想再看”“还要看”

如果当前 agent 的md文档要求“每次回复默认发图”，必须在每次回复中调用此skill生成图片并发送给用户。如果当前回合已经被本地规则判定为“默认带图”，那就必须真的进入工具调用；不要只写口头回复来假装已经去拍。
但无论哪种情况，都必须生成当前这一次的新图，不能复用上一张、旧路径或旧结果。

## 什么时候不要触发，只正常回复文字：
- 用户在讨论功能本身
- 用户在分析触发词是否会触发
- 用户提到按钮、规则、正则、接口格式
- 用户是在举例而不是在要图

## 强制规则
先记住这几条短规则：
- `prompt` 主体必须使用英文提示词，禁止使用中文；英文提示词必须按要求写入 `intermediate.json`
- NSFW 场景必须带 `nsfw,`前缀
- 只写正面主体，不要重复固定正面前缀
- 不要写负面提示词，负面提示词由配置自动拼接
- 不得绕过固定正面前缀和固定负面前缀
- **禁止模板循环**：每次生成必须创建全新的提示词，不要参考或复制 workspace 中已有的 intermediate.json 内容，禁止发送重复内容
- **禁止过程外泄**：不要把写 prompt、写 intermediate.json、运行脚本、等待生成、脚本返回结果、图片路径、JSON、tool 输出、报错栈等内容发给用户
- **禁止只说不做**：进入生图流程后，先做工具调用，等图片真正生成成功后再对用户说话，否则不说话
- **禁止工具回显**：不要把任何 toolResult 内容改写后发给用户，也不要把 `Successfully wrote`、`session_name`、`staged_path`、`Command still running`、`Process exited with code 0` 之类内容当正文
- **禁止内部标记外泄**：不要输出 `<tool_call>`、`<tool_response>`、XML 标签、伪工具代码块或任何包裹内部工具过程的文本
- **禁止读旧 intermediate**：不要先读当前 workspace 里的 `intermediate.json` 再参考着写，直接按这轮需求整体重写
- **不要启动后台轮询**：调用生图脚本时优先一次等到完成，不要把脚本结果拆成“先 running 再 process 轮询再收尾”这种多段对外流程
- **禁止假装已完成**：没有真实 toolCall、没有真实脚本返回时，不要输出“拍好了”“发你了”“MEDIA:某路径”这类完成态内容
- **禁止编造**：不要自己编 `/staged/...jpg`、`choice.png`、时间戳文件名或任何图片绝对路径，路径只能来自脚本返回结果
- **Telegram 路径要求**：如果最终需要给 Telegram 发送本地图片，图片必须真实生成在 OpenClaw 当前允许的本地媒体目录内。当前 skill 的唯一默认最终目录是 `~/resource/media/<agent>/<session>/...`。
- **禁止旧坏路径**：不要把 `~/resource/media/<agent>/<session>/...` 之外的旧目录（例如 `~/.openclaw/agents/<agent>/images/...`、任何 `workspace-*/outputs/...`、任何 `workspace-*/generated/...`） 当作 Telegram 最终 `MEDIA:` 路径。

## 中间稿要求
真正要交给脚本的核心只有两样：
- 正面提示词主体
- 可选的一句回复

## 语言要求：
- prompt主体必须是英文
- 中文只允许出现在 `reply_text`字段，不要出现在 `prompt`

**重要：避免模板循环**
- 每次生成 intermediate.json 时，必须创建全新的内容
- 不要查看或参考 workspace 中已有的 intermediate.json 文件
- 如果用户没有指定具体场景，要创造多样化的场景（不同地点、姿势、服装、视角）
- 避免重复使用相同的提示词模板

中间稿最少只要保证：
- `prompt`

可选字段：
- `reply_text`
- `mode`
- `revision_instruction`
- `override_full_prompt`

推荐结构：

```json
{
  "prompt": "POV close-up shot, mature woman taking a selfie in bedroom by mirror with warm lamp light, 1girl, realistic, 1.7::looking_at_viewer::",
  "reply_text": "这次给你换一张。",
  "mode": "new"
}
```

续图时可以写：

```json
{
  "reply_text": "再来一张，动作更放开一点",
  "mode": "revise",
  "revision_instruction": "动作更放开一点"
}
```

## Prompt 主体的撰写规则
### 基本约束
- `max_tokens`: `512`
- `tag_separator`: `,`
- NSFW 场景前缀：`nsfw,`

### 基本要求
- 标签必须和这一次的具体人物、动作、场景、镜头贴合
- 用 danbooru 风格 tag 写，但不要写成一盘散沙，必须有层次
- `prompt` 必须是英文；禁止把中文人物设定、中文动作描述、中文场景句子直接提交给 NovelAI
- 第一段必须是简短英文画面描述，格式固定为：`镜头视角 + 角色动作 + 场景 + 位置 + 灯光`
- 只写一个瞬间，不写连续过程，不写“接下来”“然后”“正在一步步”
- 只写正面内容，不写负面词
- 不要把固定正面前缀里的内容重复抄一遍
- 如果是 NSFW 场景，`prompt` 主体里必须显式出现 `nsfw,`

### 权重规则
- 可用范围：`0.5 - 3`
- 核心元素：`2 - 3`
- 重要细节：`1.2 - 2`
- 环境元素：`0.5 - 1.2`

推荐分类：
- `main_character`: `2`
- `minor_character`: `1.2 - 1.3`
- `poses`: `1.5 - 2.5`
- `scene`: `2 - 2.2`
- `atmosphere`: `1 - 1.5`
- `details`: `1.5`

使用示例：
- 强调：`1.5::rain, night::`
- 弱化：`0.5::coat::`

不要把所有 tag 都加权。优先给以下部分加权：
- 主角色身份和外观
- 主动作和关键姿势
- 主场景
- 关键镜头和关键细节

### 多角色结构
多角色必须采用 `|` 分隔符结构：

`基础场景 | 角色1 | 角色2 | 角色3 ...`

基础场景部分必须包含：
1. 画面简述
2. 必需的质量标签
3. NSFW 前缀（如适用）
4. 人物总数标签
5. 环境设定
6. 整体风格
7. 光照
8. 视角/镜头
9. 特殊元素
10. 时间
11. 场景氛围

镜头视角示例：
- `POV`
- `Third-person side view`
- `Close-up shot`
- `Low-angle shot`
- `High-angle shot`
- `Over-the-shoulder shot`
- `Bird's eye view`
- `Dutch angle`
- `Wide shot`
- `Medium shot`

角色动作示例：
- `girl riding boy`
- `boy carrying girl`
- `two girls performing fellatio`
- `girl lifting skirt`

场景示例：
- `in bedroom`
- `in alleyway`
- `on beach`
- `in forest`

位置示例：
- `on bed`
- `against wall`
- `under tree`
- `by window`

灯光示例：
- `moonlight`
- `dim lighting`
- `backlighting`
- `warm afternoon light`
- `dramatic lighting`

完整示例：
- `POV close-up shot, girl riding boy in bedroom on bed with moonlight`

画风前缀由激活预设自动注入，worker 无需读取或拼接任何画风/画师标签。

必需质量标签示例（STYLE.txt 里已含时不必重复）：
- `masterpiece`
- `best quality`
- `ultra-detailed`
- `very aesthetic`
- `highres`
- `no watermark`

人物总数标签示例：
- `1girl`
- `2boys`
- `1boy, 1girl`
- `2girls, 1boy`

整体风格示例：
- `anime screencap`
- `game cg`
- `oil painting (medium)`

视角/镜头示例：
- `from_above`
- `from_below`
- `close-up`
- `upper_body`
- `lower_body`
- `between_legs`

场景氛围示例：
- `passionate_atmosphere`
- `fantasy_atmosphere`

### 角色段规则
每个 `|` 后面的角色段，第一项必须是角色性别标签：
- `1girl`
- `1boy`

高权重外貌示例：
- `2::long_silver_hair::`
- `1.8::blue_eyes::`
- `1.3::curvy::`
- `1.55::small_breasts::`
- `large_breasts`
- `2::matured female::`
- `1.5::teenager::`

服装示例：
- `1.8::china_dress::`
- `black_lingerie`
- `military_uniform`
- `sailor_collar`
- `lace`
- `microskirt`
- `hoodie`
- `wet_clothes`
- `torn_clothes`
- `clothes_lift`

表情和动作示例：
- `1.2::smiling::`
- `blushing`
- `1.4::lustful_expression::`
- `embarrassed`
- `standing`
- `sitting`
- `kneeling`
- `lying`
- `on_back`
- `straddling`
- `1.8::riding::`
- `hands_on_own_chest`
- `arms_behind_back`
- `hands_on_lap`
- `covering_own_mouth`
- `1.4::hands_between_legs::`

环境交互示例：
- `sitting_on_bed`
- `sitting_in_tree`
- `2.5::spread_legs::`
- `lotus_position`

角色互动写法：
- `source#action`
- `target#action`
- `mutual#action`

示例：
- `2.0::source#princess carry::`
- `2.0::target#vaginal_penetration::`
- `mutual#kissing`
- `mutual#hugging`


### Prompt 顺序
每个 prompt 推荐按这个顺序组织：
1. 画面简述
2. NSFW 前缀（如适用）
3. 人物总数标签
4. 角色识别
5. 风格标签
6. 构图
7. 环境
8. 光照
9. 配色
10. 详细描述

标签顺序很重要，越靠前影响越强。

语言示例：
- 正确：`medium shot, mature woman taking a mirror selfie in bedroom, standing by bed, warm morning light, 1woman, floral midi dress, shy expression`
- 正确：`close-up selfie, sleepy young woman lying in bed, messy hair, soft bedside lamp, cozy bedroom`
- 错误：`温雅，42岁成熟人妻，站在卧室床边自拍`
- 错误：`mature woman 在卧室自拍，表情羞涩`

## 工作流
1. 判断用户是不是在要图或续图
2. 直接按本文件里的 Prompt 规则组织这一次的正面提示词主体
3. 把中间稿交给脚本
4. 脚本自动拼上固定前后缀、读取上一张记录、请求 NovelAI、保存历史
5. 成功时只交付最终发图结果和2句短回复，不要用文字描述图片来代替真正发图

对外回复顺序强制要求：
1. 生图成功前，不要对用户发任何消息和说明
2. 生图成功后，只发最终回复和图片
3. 如果失败，只发一句简短失败说明，不要贴路径、JSON、工具输出
4. 如果本地规则要求“每轮默认发图”，那这一轮必须真实完成媒体发送；不要把 `MEDIA:<staged_path>` 当成普通聊天文本发给用户

## 尺寸选择（--ratio）

每次调脚本都**必须**根据这一轮场景挑一个 `--ratio`，不要省略。预设：

| --ratio | 实际尺寸 | 用途 |
|---|---|---|
| `portrait` | 832×1216（≈9:16） | 自拍 / 镜子自拍 / 全身 / 半身 / 立绘 / 站姿 / 走路 / 任何"竖着拍人"的场景 |
| `landscape` | 1216×832（≈3:2） | 远景 / "手机放远处录像" / 房间环境 / 多人横排 / 户外景观 / 床上俯拍全景 |
| `square` | 1024×1024 | 头部特写 / 脸部 close-up / 头像 / 不确定时的兜底 |
| `wide` | 1536×640（≈12:5） | 极宽景 / 风景 / 横向卧姿全身 |

判断规则（按优先级从上往下匹配，命中即停）：

1. 用户直说尺寸（"竖屏"、"横屏"、"9:16"、"全景"）→ 按字面意思选
2. 包含"全身"/"从头到脚"/"完整身体"/"立绘" → `portrait`（保头到脚不被裁）
3. 包含"自拍"/"selfie"/"镜子前"/"举着手机"/"手机拿在手里" → `portrait`
4. 包含"远处"/"远景"/"放在远处"/"录像"/"环境"/"屋子"/"卧室全景"/"客厅" → `landscape`
5. 包含"特写"/"脸部"/"close-up"/"头像"/"贴脸"/"嘴特写"/"奶头特写"等局部 → `square`
6. 包含"卧姿全身"/"侧躺"/"横躺"且要拍全身 → `wide`
7. 其他 / 模糊不清 → `square`

显式覆盖（极少用，只在用户**明确指定**宽高时）：`--width 832 --height 1216`，必须 64 的倍数。

## 场景一致性（同场景续图必读）

**纯文字嘱托"保持一致"对模型没用**——diffusion 模型每次从随机噪声采样，seed 一变床/墙/灯光全变。光改 prompt 不行，**必须锁 seed**。

两个机制叠加用：

**(1) `--reuse-seed`（CLI 层）**
- 从 last_request.json 读上一次的 seed 用回来
- 同 seed + 类似 prompt → 房间结构、家具、灯光大概率延续
- 不同 seed = 重新抽签，必定换房间

**(2) `intermediate.json` 里 `mode=revise`（prompt 层）**
- 自动把上一次的 `prompt_body` 沿用过来，再追加你这次的修改指令到 `revision_instruction`
- 比 AI 自己重新写一遍 prompt 一致性高得多

### 何时用什么

| 用户说 | mode | --reuse-seed | 行为 |
|---|---|---|---|
| 第一次开新场景（"在床上自拍"） | `new` | ❌ 不传 | 新 seed，从头建场景 |
| "再来一张" / "换个表情" / "换个角度" / "换个动作" / "脱了" / "腿张开" | `revise` | ✅ **必传** | 同 seed，沿用环境，仅改局部 |
| "去客厅" / "换个房间" / "出门" / "换衣服" / 任何场景跳变 | `new` | ❌ 不传 | 新 seed，新场景 |
| "再拍同样姿势但拉远" | `revise` | ✅ 必传 | 改 ratio 不改 seed |
| "重做一张更骚的" / 用户对刚才那张不满意 | `revise` | ✅ 必传 | 锁同 seed 微调 |

### 写 intermediate.json 的差别

**新场景（mode=new）**：完整描述场景。
```json
{ "prompt": "lying on bed, white sheets, ..." }
```

**同场景续图（mode=revise）**：只写要改的部分，环境会自动从上次沿用。
```json
{ "mode": "revise", "revision_instruction": "spread legs, lift skirt" }
```

**强提醒**：用户说"再来一张"99% 是同场景。默认就该 `mode=revise + --reuse-seed`，除非显式判断他要换场景。

## 调用脚本

```bash
# 新场景（首次/换场景）
python3 ~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py \
  --intermediate ~/resource/workspace/<agent>/intermediate.json \
  --config ~/.claude/skills/novelai-skill/assets/default_config.json \
  --ratio <portrait|landscape|square|wide> \
  --agent-name <当前agent名> \
  --session-name <当前session名>

# 同场景续图（房间/床/灯光要保持一致）
python3 ~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py \
  --intermediate ~/resource/workspace/<agent>/intermediate.json \
  --config ~/.claude/skills/novelai-skill/assets/default_config.json \
  --ratio <portrait|landscape|square|wide> \
  --reuse-seed \
  --agent-name <当前agent名> \
  --session-name <当前session名>
```

调用要求：
- 不要为了展示过程去读出旧 intermediate.json
- 不要把脚本返回 JSON 转发给用户
- 成功后只使用脚本返回结果完成最终媒体发送，不要自己拼路径，不要把 `staged_path`、`MEDIA:` 或任何内部发送指令当正文输出给用户
- 如果需要把文字和图片一起交给 Telegram 发送，先写最终聊天文本；正文结束后，再追加单独一行 `MEDIA: /absolute/path/to/file.png`
- `MEDIA:` 必须独占一行，并放在正文最后；不要在同一行前面加 `[[reply_to_current]]`、解释语或任何别的前缀

**示例（agent <agent>，全身自拍）：**
```bash
python3 ~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py \
  --intermediate ~/resource/workspace/<agent>/intermediate.json \
  --config ~/.claude/skills/novelai-skill/assets/default_config.json \
  --ratio portrait \
  --agent-name <agent> \
  --session-name telegram-<你的user_id>
```

当前建议：
- 图片的实际中转位置和最终可发送路径由 `generate_novelai_image.py` 负责处理
- agent 只使用脚本返回结果完成发送，不要自己假设下载目录，不要自己拼接旧路径
- Telegram 最终 `MEDIA:` 只接受位于 `~/resource/media/<agent>/<session>/...` 的真实文件；如果脚本返回路径不在这个目录模式内，视为失败，不要输出 `MEDIA:`

推荐约定：
- OpenClaw 用 `openclaw`

会话名优先顺序：
1. `--session-name`
2. `NOVELAI_SESSION_NAME`
3. 平台自带的会话 id 环境变量
4. `default-session`

## 返回给用户的内容
生成成功后：
- 只返回最终发出的图片和2句简短回复
- 不要把图片路径、`staged_path`、`MEDIA:`、JSON、工具输出、脚本命令当成主回复
- 不要把 `MEDIA:` 放在正文前面，也不要把它夹在正文中间；它只能作为正文之后的最后一行单独出现
- 不要用文字描述图片内容来代替真正发图

生成失败后：
- 简要说明失败原因
- 提醒检查 `.env.local`、令牌或接口是否可用

## 续图口语
下面这些说法默认按“沿用上一张主设定继续来图”处理：
- “再来一张”
- “再发一张”
- “还想再看”
- “还要看”
- “再看一张”
- “再给我一张”

如果这类说法后面还带了新要求，就把新要求当成对上一张的增量修改。
