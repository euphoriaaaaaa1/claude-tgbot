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
- **禁止假装已完成**：没有真实 toolCall、没有真实脚本返回时，不要输出“拍好了”“发你了”这类完成态内容，也不要在没有真实发送的情况下假装已发图
- **禁止编造**：不要自己编 `/staged/...jpg`、`choice.png`、时间戳文件名或任何图片绝对路径，路径只能来自脚本返回结果
- **Telegram 路径要求**：如果最终需要给 Telegram 发送本地图片，图片必须真实生成在 OpenClaw 当前允许的本地媒体目录内。当前 skill 的唯一默认最终目录是 `~/resource/media/<agent>/chat_<chat_id>/...`。
- **禁止旧坏路径**：不要把 `~/resource/media/<agent>/chat_<chat_id>/...` 之外的旧目录（例如 `~/.openclaw/agents/<agent>/images/...`、任何 `workspace-*/outputs/...`、任何 `workspace-*/generated/...`） 当作 reply files 的图片路径。

## 中间稿要求
交给脚本的核心有三样。`prompt` 用英文写整个画面；`characters` 为画面里每个可见角色写一项，同样用英文；`reply_text` 可选，写一句回复。

## 语言要求：
- prompt主体必须是英文
- 中文只允许出现在 `reply_text`字段，不要出现在 `prompt`

**重要：避免模板循环**
- 每次生成 intermediate.json 时，必须创建全新的内容
- 不要查看或参考 workspace 中已有的 intermediate.json 文件
- 如果用户没有指定具体场景，要创造多样化的场景（不同地点、姿势、服装、视角）
- 避免重复使用相同的提示词模板

中间稿最少要有 `prompt`。可选字段为 `characters`、`position_mode`、`reply_text`、`mode`、`revision_instruction`、`override_full_prompt`。

推荐结构：

```json
{
  "mode": "new",
  "prompt": "Wide shot at eye level, a woman pouring tea for a man at a cafe window seat, warm afternoon light, 1girl, 1boy, both fully visible",
  "position_mode": "manual",
  "characters": [
    {"positive": "girl, long black hair, white blouse, pouring tea, smiling", "center": {"x": 0.3, "y": 0.55}},
    {"positive": "boy, short brown hair, navy hoodie, holding a cup, looking at her", "center": {"x": 0.7, "y": 0.55}}
  ]
}
```

续图时可以写：

```json
{
  "reply_text": "再来一张，动作更放开一点",
  "mode": "revise",
  "revision_instruction": "bolder pose"
}
```

## Prompt 主体的撰写规则
### 基本约束
- `prompt` 与全部角色 `positive` 合计约 512 token，角色最多 6 个。
- `tag_separator`: `,`
- NSFW 场景前缀：`nsfw,`

### 基本要求
- 标签必须和这一次的具体人物、动作、场景、镜头贴合
- 用 danbooru 风格 tag 写，但不要写成一盘散沙，必须有层次
- `prompt` 必须是英文；禁止把中文人物设定、中文动作描述、中文场景句子直接提交给 NovelAI
- `prompt` 只写整个画面，顺序为镜头视角、角色动作、场景、位置、灯光，随后是人数标签、构图约束、环境与氛围。景别词、镜头词、动感词与 `nsfw` 标签都写在 `prompt` 里。`prompt` 不写任何角色的外貌与服装，也不写质量标签。
- 每个可见角色在 `characters` 里写一项，`positive` 以不带数字的 `girl`、`boy` 或 `other` 开头，随后写该角色在本镜头里可见的完整外貌、服装、表情与动作。每一项在每一张里都完整写出，不用同上或编号指代。
- **正文不得以质量标签开头，也不得在正文里堆叠质量/画风标签**：`best quality` / `masterpiece` / `absurdres` / `very aesthetic` / 画师名等由脚本前缀自动注入，正文再抄一遍等于把画面描述挤到最后
- 只写一个瞬间，不写连续过程，不写“接下来”“然后”“正在一步步”
- 只写正面内容，不写负面词
- 不要把固定正面前缀里的内容重复抄一遍
- 如果是 NSFW 场景，当轮 `prompt` 必须含 `nsfw,`（这是唯一的色图开关）；续图（revise）时写在 `revision_instruction` 或新 `prompt` 的开头，否则这张按正常图出（只写 `revision_instruction` 时会沿用上一张正文，上一张里的 `nsfw` 也会一起带上；要转回正常图就写新 `prompt`）。`nsfw` 不写在角色的 `positive` 里。

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

不要把所有 tag 都加权，但每个角色的 `positive` 都要给权重，不允许任何角色裸写。需要加权的有以下几类。
- **每个角色的身份和外观**：写在各自的 `positive` 里，主角从 `2` 起，同框的其他角色从 `1.2 - 1.3` 起；模型按权重分配画面注意力，**没权重的角色会被裁出画面或只留局部**（实测：同框的"用户"消失，就是只给主角加权造成的）
- 主动作和关键姿势
- 主场景
- 关键镜头和关键细节

### 角色分段（characters）
- 画面里每个可见角色写一项，单人画面也写一项；画面里没有人时写 `"characters": []`。
- 人数标签只写在 `prompt` 里，`positive` 以不带数字的 `girl`、`boy` 或 `other` 开头。
- 角色按画面从左到右的顺序排列。
- 两人及以上需要固定站位时写 `"position_mode": "manual"`，并给每个角色写 `center`。`x` 与 `y` 取 0 到 1，`x` 从左到右增大，`y` 从上到下增大。同时在 `prompt` 里用自然语言写出位置关系。站位只是倾向，不保证精确。不需要固定站位时省略 `position_mode` 与 `center`。
- 多人画面在 `prompt` 里写正面式构图约束，避免把镜头锁在单人身上的景别词。
- `characters` 只对 NovelAI 有效。ComfyUI 仍按 comfyui-skill 写整段提示词，两套语法不得混用。
- 脚本以退出码 3 停下时，按报错指出的角色与字段改好 intermediate 再重跑。没写 `characters` 时脚本按整段提示词出图，角色容易串色或缺失。

镜头视角示例：
- `POV`
- `Third-person side view`
- `Close-up shot`
- `Low-angle shot`
- `High-angle shot`
- `Over-the-shoulder shot`
- `Bird's eye view`
- `Dutch angle`
- `Fisheye lens` / `wide-angle lens` / `telephoto lens`
- `From front` / `from behind` / `side view`（第三人称的正面 / 背面 / 侧面）
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

**质量标签同样由脚本前缀（STYLE.txt / 激活预设）注入，正文一律不要写**——`masterpiece` / `best quality` / `ultra-detailed` / `very aesthetic` / `highres` / 画师名等都不要出现在正文里。

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

**禁止在正文里写 `user` / `viewer` 这类词**（NovelAI 不认，等于噪声）：第一人称视角一律用标准 tag `pov` / `pov_hands` / `pov_arm`；要角色看向镜头写 `looking_at_viewer`；POV 图里的"对话方"（镜头对面的人）**不写进正文**——镜头本身就是他，写了反而画蛇添足。

场景氛围示例：
- `passionate_atmosphere`
- `fantasy_atmosphere`

### 角色段规则
每个角色的 `positive` 第一项是不带数字的 `girl`、`boy` 或 `other`。

高权重外貌示例：
- `2::long_silver_hair::`
- `1.8::blue_eyes::`
- `1.3::curvy::`
- `1.55::small_breasts::`
- `large_breasts`
- `2::mature female::`
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

角色互动写法（**必须写清"谁对谁做"**，这是最容易画反的地方）：

- 语法：`<角色是谁>#<动作>`，**同一个动作两边各标一次**，分别写进各自角色的 `positive`：
  - `source#动作` = 这个角色是动作的**发起方**（他/她在做）
  - `target#动作` = 这个角色是动作的**承受方**（动作落在他/她身上）
  - `mutual#动作` = 双方对等（接吻、拥抱、互相抚摸）
- 示例：
  - `2.0::source#princess carry::`（这段的角色是抱人那个）
  - `2.0::target#vaginal_penetration::`（这段的角色是被进入那个）
  - `mutual#kissing` / `mutual#hugging`
  - **`mutual#` 的完整写法 = 同样两边各写一次**（对等动作，各自的 `positive` 里写自己那一半的身体归属，绝不只写一边）：
    - 接吻：`角色的 positive：1.8::mutual#kissing:: the girl's lips pressed against the boy's lips` ＋ `用户的 positive：1.8::mutual#kissing:: the boy's lips meeting the girl's lips, the boy's hand cupping the girl's cheek`
    - 拥抱：`角色的 positive：2.0::mutual#hugging:: the girl's arms wrapped around the boy's back` ＋ `用户的 positive：2.0::mutual#hugging:: the boy's arms holding the girl's waist, her cheek against his chest`

**三道防线（缺一就容易画反，三道都要做）：**

1. **动作写进"做动作那个角色"的 `positive` 里**，不要只写在 `prompt` 里。动作写在谁的 `positive` 里，模型就更可能认为是谁在动。
2. **身体部位必须带归属**：写 `girl's nipples` / `boy's nipples` / `the woman's neck`，**绝不写裸的 `nipples` / `mouth` / `hand`**——不带归属时模型会自己挑一边，往往挑错。
3. **禁止主语不明的裸动名词**：只写 `licking nipples` / `sucking` / `caressing` 而不写谁对谁做，模型默认按"被画面主体做"来画，最容易与用户意图相反。

**按用户原话的施受关系写，不要按画面惯例猜：**

- 用户说「**我来**舔她 / 我服侍她 / 我摸她」→ **用户的 positive** 标 `source#`，**角色的 positive** 标 `target#`
  `角色的 positive：2.0::target#nipple_licking:: the girl's nipples` + `用户的 positive：2.0::source#nipple_licking::`
- 用户说「**让她**舔我 / 她服侍我 / 她摸我」→ 反过来：**角色的 positive** 标 `source#`，**用户的 positive** 标 `target#`
  `用户的 positive：2.0::target#nipple_licking:: the boy's nipples` + `角色的 positive：2.0::source#nipple_licking::`

**写完自检一句**：把 prompt 读一遍，问自己"这段里到底是谁在动？"——与用户原话对不上就重写，不要指望模型自己纠正。


### Prompt 顺序
`prompt` 按此顺序：画面简述、NSFW 前缀（如适用）、人数标签、风格标签、构图、环境、光照、配色。角色识别与详细描述写进各自的 `characters` 项。标签越靠前影响越强。

语言示例：
- 正确：`prompt` 写 `medium shot, taking a mirror selfie in bedroom, standing by bed, warm morning light, 1girl`，`characters[0].positive` 写 `girl, 2::mature female::, floral midi dress, shy expression`
- 正确：`prompt` 写 `close-up selfie, lying in bed, cozy bedroom, soft bedside lamp, 1girl`，`characters[0].positive` 写 `girl, 2::messy hair::, sleepy expression`
- 错误：`某角色名，42岁成熟人妻，站在卧室床边自拍`
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
4. 如果本地规则要求“每轮默认发图”，那这一轮必须真实完成媒体发送；不要把内部路径当成普通聊天文本发给用户，必须经 reply 的 files 参数真实发送

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
- `characters` 与 `position_mode` 也自动沿用；本次写了新 `prompt` 时要连同 `characters` 一起写，否则按整段提示词出图；要改站位或换人，也连同 `characters` 一起写
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
{ "prompt": "lying on bed, white sheets, ...", "characters": [{"positive": "girl, ..."}] }
```

**同场景续图（mode=revise）**：只写要改的部分，环境会自动从上次沿用。
```json
{ "mode": "revise", "revision_instruction": "nsfw, spread legs, lift skirt" }
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
  --chat-id <当前聊天的chat_id>

# 同场景续图（房间/床/灯光要保持一致）
python3 ~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py \
  --intermediate ~/resource/workspace/<agent>/intermediate.json \
  --config ~/.claude/skills/novelai-skill/assets/default_config.json \
  --ratio <portrait|landscape|square|wide> \
  --reuse-seed \
  --agent-name <当前agent名> \
  --chat-id <当前聊天的chat_id>
```

**一轮要出多张图（mode=new）**：多张一律串行，一张跑完再跑下一张。同一动作只换视角的几张用同一个种子，第一张正常跑，其余各张加 `--reuse-seed`；想要构图不同就各用随机种子，不加 `--reuse-seed`。
每张图的提示词互不依赖（**同一时刻同一动作、仅视角不同**——正文除"视角/镜头那段"外逐字相同；该段**仅指 `prompt` 开头的镜头视角短语本身**（`prompt` 的顺序见「基本要求」），同一句里的构图约束/动作/场景/灯光一律逐字照抄，一开始就能全部想好），所以把"写 N 份 intermediate + 依次跑 N 次脚本"合进同一条 Bash 命令。
同账号可能撞并发锁（HTTP 429 Concurrent generation is locked；几只 bot 加朋友圈共用一个账号）。脚本遇到会自动排队重试（等 5/10/15/20/30 秒、共 6 次），排队约 80 秒仍撞锁就按原始那次的错误报失败。
视角由提示词决定，而床、灯光、房间、皮肤纹理等细节由 seed 主导，seed 不同则这些全部重抽，两张会像两个不同的场景。后一张要读前一张记下的 seed，所以同 seed 的几张必须串行。

```bash
W=~/resource/workspace/<agent>
G=~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py
C=~/.claude/skills/novelai-skill/assets/default_config.json
cat > "$W/im_1.json" <<'EOF'
{"mode":"new","prompt":"<画面描述>","characters":[<角色>]}
EOF
cat > "$W/im_2.json" <<'EOF'
{"mode":"new","prompt":"<画面描述>","characters":[<角色>]}
EOF
rm -f "$W"/r*.json
python3 "$G" --intermediate "$W/im_1.json" --config "$C" --ratio portrait \
  --agent-name <agent> --chat-id <chat_id> > "$W/r1.json" 2>&1; rc=$?; printf '\nexit=%s\n' "$rc" >> "$W/r1.json"
grep -q '^exit=0$' "$W/r1.json" && { python3 "$G" --intermediate "$W/im_2.json" --config "$C" --ratio portrait --reuse-seed \
  --agent-name <agent> --chat-id <chat_id> > "$W/r2.json" 2>&1; rc=$?; printf '\nexit=%s\n' "$rc" >> "$W/r2.json"; }
cat "$W/r1.json" "$W/r2.json" 2>/dev/null
```

- 构图不同的几张不带 `--reuse-seed`，彼此互不依赖，仍逐张串行运行，但后一张不以前一张成功为条件。
- 命令 timeout 按每张约 150 秒给足（含服务端 5xx 重试与撞锁排队）。
- 某一张因 HTTP 500 失败多为服务端偶发故障。同一种子的几张中第一张失败时，先补跑第一张，成功后再跑后面几张；补跑仍失败时，这一轮按失败处理，做法同各 bot 生图共享规则中的失败处理。其余情况下某一张失败时补跑一次，或本轮少发一张，已成功的照常发出。

调用要求：
- 不要为了展示过程去读出旧 intermediate.json
- 不要把脚本返回 JSON 转发给用户
- 成功后只使用脚本返回结果完成最终媒体发送，不要自己拼路径，不要把 `staged_path` 或任何内部发送指令当正文输出给用户
- 发送用 reply 工具的 files 参数（路径取脚本返回的真实路径）；想让图插在文字中间，在 text 里想发图的位置单独一行写 `[[图1]]`/`[[图2]]`（对应 files 第几张），不写标记则图片在全部文字之后发；标记绝不写进 voice_text
- 旧的 `MEDIA:` 行协议已废除，不要再输出任何 `MEDIA:` 行

**示例（agent <agent>，全身自拍）：**
```bash
python3 ~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py \
  --intermediate ~/resource/workspace/<agent>/intermediate.json \
  --config ~/.claude/skills/novelai-skill/assets/default_config.json \
  --ratio portrait \
  --agent-name <agent> \
  --chat-id <chat_id>
```

当前建议：
- 图片的实际中转位置和最终可发送路径由 `generate_novelai_image.py` 负责处理
- agent 只使用脚本返回结果完成发送，不要自己假设下载目录，不要自己拼接旧路径
- reply files 只接受位于 `~/resource/media/<agent>/chat_<chat_id>/...` 的真实文件；如果脚本返回路径不在这个目录模式内，视为失败，不要发送

推荐约定：
- OpenClaw 用 `openclaw`

记录文件夹的定法：
- 一律传 `--chat-id <当前聊天的chat_id>`（只许可选负号加数字，不合规脚本会报中文错误退出）。传了它，同一聊天的记录固定落在 `chat_<chat_id>` 文件夹，`--session-name` 和各会话环境变量都被忽略；不要再自己起会话名。
- 没传 `--chat-id` 时才按旧的会话名顺序定文件夹（仅为兼容）：`--session-name` → `NOVELAI_SESSION_NAME` → 平台自带的会话 id 环境变量 → `default-session`。

## 返回给用户的内容
生成成功后：
- 只返回最终发出的图片和2句简短回复
- 不要把图片路径、`staged_path`、JSON、工具输出、脚本命令当成主回复
- 发图只走 reply 的 files 参数；`[[图N]]` 标记只在 text 里用，除此之外不要输出任何发送协议行
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
