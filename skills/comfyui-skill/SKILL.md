---
name: comfyui-skill
description: 把"中文场景描述"转成本地 ComfyUI 可吃的英文 prompt（z-image-turbo / SD 系），调用本地 ComfyUI HTTP API（127.0.0.1:8188）出图。规则参考 novelai-skill，但权重语法用 SD 标准 `(xxx:1.5)` 而非 NovelAI 的 `1.5::xxx::`。
compatibility:
  - Bash
  - Python 3
  - Local files
---

# ComfyUI 生图 Skill

## 目的
将一段中文场景/对话描述转成 **danbooru 风格英文 tag prompt**，通过本地 ComfyUI HTTP API（端口 8188）出图。
与 novelai-skill 在"撰写规则"上保持一致，但**权重语法**和**调用方式**不同。

## 何时触发
- 用户在 telegram 私聊里要求生图（自拍/发照片/看看你/按场景来一张/再来一张）
- 用户明确说"画一张""生成一张图"
- 朋友圈框架内部生图（由 `_global.yml.image_generation.provider = comfyui` 控制）
> 装了本 skill 就代表聊天默认用 ComfyUI 出图，无需再判断 provider。和 novelai-skill **二选一**，别同装。

## 何时不触发
- 用户只是讨论功能、规则、按钮、触发词
- 用户是在举例，不是真的要图

## 前置条件（不满足会出图失败）
- 本机 ComfyUI 已启动，监听 `comfyui.url`（默认 `http://127.0.0.1:8188`）
- `configs/_global.yml` 的 `comfyui.workflow` 指向可用的 txt2img workflow API JSON
- 跑脚本的 Python 装了 `PyYAML`（就是你跑朋友圈网页那个 Python）

## 调用入口
仓库根路径在环境变量 `$CLAUDEBOTLIFE_REPO`，bot 名在 `$TELEGRAM_WORKER_BOT`。不要写死绝对路径。

```bash
# Mac / Linux
python3 "$CLAUDEBOTLIFE_REPO/scripts/comfyui_gen.py" "$TELEGRAM_WORKER_BOT" "<英文 prompt>" [--size <预设>] [--neg "<额外负向 tag>"]
```
```bat
:: Windows（PowerShell / cmd）
python "%CLAUDEBOTLIFE_REPO%\scripts\comfyui_gen.py" "%TELEGRAM_WORKER_BOT%" "<英文 prompt>" [--size <预设>]
```

参数：
- 第 1 个位置参数 `bot_id` 直接用 `$TELEGRAM_WORKER_BOT`：把图分目录 + **自动读取 yml 里的 anchor_image / anchor_denoise**（透明走 img2img 保人物一致性）
- 第 2 个位置参数 `<英文 prompt>`：主体正向（不需要写质量前缀，脚本会拼 `_global.yml.positive_prefix`）
- `--neg`: 可选额外负向
- `--size`: **必须根据用户意图 + prompt 内容主动选**。默认 quick 只对"日常单人自拍"成立，其它场景**必须显式传 --size**。详见下面的 Size 决策表。

成功时 stdout 最后一行是 `MEDIA: <图片绝对路径>`，失败输出 `FAIL: <原因>` 并非零退出。

## 出图后怎么发（关键，别搞错）
从 stdout 抓出 `MEDIA:` 后面那个绝对路径，然后**调用 `reply` 工具**把路径放进 `files`：

```
reply(text="给你拍了一张～", files=["<从 MEDIA: 抓到的绝对路径>"])
```

- 唯一能把图发到 Telegram 的通道是 `reply` 的 `files` 参数。
- **绝不要**把 `MEDIA: xxx` 这行文本直接发给用户——新架构里它不会被解析成图片，用户只会看到一串路径。
- 脚本 `FAIL` 或没拿到路径时，不要瞎编路径塞给 `reply`；按人设自然说这次没拍成（如"相机抽风了，等下再拍"）。

### Size 决策表（必读）

每次调用前**必须**根据用户意图 + prompt 内容决定 size。**不要默认 quick 一刀切**。

#### 第 1 步：看用户怎么说（最强信号）

| 用户原话关键词 | 必传 `--size` |
|---|---|
| "竖屏 / 长腿 / 全身 / 站着 / 站立展示 / 高个子" | `tall` (640×1216, 9:16 长竖) |
| "撅屁股 / 撅起来 / 趴着 / 跪着 / 床戏俯视 / 后入 / 横屏 / 宽景 / 风景 / 电影感 / 大场面" | `wide` (1216×640, 16:9 长横) |
| "横图 / 双人 / 合照 / 聚会" | `landscape` (1152×768, 3:2 横) |
| "高清 / 精品 / 特写 / 认真画 / 肖像" | `portrait` (768×1152, 2:3 竖) |
| "头像 / 食物 / 茶 / 物品 / 摆拍 / square" | `square` (1024×1024) |
| "随便看看 / 缩略图 / 快一点" | `tiny` (512×768) |
| 没明确要求 → 看第 2 步 | （继续判断） |

#### 第 2 步：从 prompt 内容自动推断（用户没明确说时）

| prompt 含这些英文 tag | 应传 `--size` |
|---|---|
| `full body shot / standing nude / 1girl standing / from below / leg focus`（站立全身/长腿） | `tall` (9:16) |
| `bent over / on all fours / ass up / doggy style / prone bone / from behind / lying on stomach / kneeling blowjob / cowgirl from above`（横向身体延展） | `wide` (16:9) |
| `wide shot / establishing shot / panorama / cityscape / landscape view`（场景） | `wide` (16:9) |
| `lying on bed / lying on back / legs spread / spread legs / missionary`（平躺，看具体角度） | 仰拍 → `wide`；正面 → `portrait` |
| `1boy, 1girl together / multiple_girls / 2girls / group / outdoor scene` | `landscape` |
| `extreme close-up / face focus / portrait shot` | `portrait` |
| `food photography / still life / object / no humans` | `square` |
| 普通 1girl 自拍/单人室内 | `quick`（默认） |

#### Size 速度参考

| 预设 | 尺寸 | 耗时 | 典型场景 |
|---|---|---|---|
| `tiny` | 512×768 | ~32s | 缩略图 |
| `quick` | 640×960 | ~50s | 日常单人自拍 |
| `portrait` | 768×1152 | ~70s | 高清特写/肖像 |
| `landscape` | 1152×768 | ~70s | 双人/横图 |
| `square` | 1024×1024 | ~70s | 头像/食物/物品 |
| `tall` | 640×1216 | ~70s | 全身/长腿/撅展示 |
| `wide` | 1216×640 | ~70s | 宽景/电影感 |

**❗ 反例**：用户说"画一张你撅着的图"，prompt 写了 `bent over, ass up, full body`，**必须** `--size tall`。如果你忘了传，bot 出来的图会是 640×960 半身比例，撅腿都看不到 → 用户不满意。

**❗ 反例**：用户说"宽屏风景画一张陆家嘴夜景"，**必须** `--size wide`，不能默认 quick 出竖图。

## ⚠️ 强制规则
- **prompt 主体必须是英文**，danbooru 风格 tags（逗号分隔）
- **NSFW 场景必须显式带 `nsfw,` 前缀**
- **不要重复**已在 `positive_prefix` 里写的 quality tag（masterpiece/best quality 等）
- **不要写负向 prompt**（负向走配置）
- 不要绕过固定前缀
- 每次请求生成全新 tags，不要复用上一次的

## ⚠️ z-image-turbo 特别提醒（实测验证 v2）

**z-image 的 CLIP 是 qwen_3_4b（中文 LLM-based），跟 SD 标准 CLIP 完全不同**。它**不喜欢 booru tag 堆砌，喜欢自然语言句子**。

### ⭐ 核心方法论（多次实测确认）

#### 1. prompt 用自然语言句子，不是 tag 堆砌

| ❌ tag 堆砌（z-image 不响应） | ✅ 自然语言（z-image 完美理解） |
|---|---|
| `(mature_female:2.0), (kneeling:1.5), (ass_up:1.5), (from_behind:1.5)` | `跪在床上撅起臀部的35岁中国成熟女性，上半身趴在床上，从背后拍摄的全身照视角` |
| `(MILF:2.0), (older_female:1.8)` | `一位性感的中国少妇` / `35岁成熟女性` |
| `(crow's feet:1.4), (nasolabial folds:1.3)` | `皮肤紧致曲线饱满，鱼尾纹和法令纹清晰可见` |

#### 2. 主语前置 + 关键维度末尾加权（最关键！）

z-image 对句中位置敏感——**关键属性必须放主语**：
- ❌ `一位中国少妇...从背后拍摄的视角` ← 视角被当成修饰，弱化
- ✅ `跪在床上撅起臀部的中国少妇... (从背后拍摄:3.0)` ← 视角变主体一部分

**加权放末尾**，权重 **2.0-3.0**（不是 1.5）：
```
[自然语言描述句子...]，(关键视角:3.0), (姿势:3.0), (全身照:3.0)
```

#### 3. denoise 实测分档（很重要）

| denoise | 能做到 | 不能做到 |
|---|---|---|
| `0.4-0.5` | 微调（衣服/表情/光线） | 大姿势、视角换 |
| `0.6-0.7` | 出现局部线索（背景里有 lingerie/腿） | 主体视角还是 anchor |
| **`0.78`** | ✅ **跪坐全身照、姿势能换** | from behind 视角偶尔出 |
| **`0.82`** | ✅ **趴姿撅起、from behind 出来** | 脸略年轻但还有熟感 |
| `0.85+` | 完全脱离 anchor | 脸不像 |

**默认 0.78-0.82 是甜点**，看你想保多少 anchor。

#### 4. 一个完整的 prompt 模板

```python
PROMPT = """[主语前置：把姿势/动作/视角写进描述本身]，
[场景细节用自然语言描述]，[皮肤/身材自然语言]，
[服装自然语言]，[灯光自然语言]。
(关键视角:3.0), (关键姿势:3.0), (镜头:3.0)"""

# 例子（38岁人妻 + 跪姿撅臀 + from behind）：
PROMPT = """跪在床上撅起臀部的35岁中国成熟女性，
上半身趴在床上，从背后拍摄的全身照视角，
皮肤紧致曲线饱满，鱼尾纹隐约可见，
身穿黑色蕾丝内衣和黑色长筒袜，卧室温暖灯光。
(从背后拍摄:3.0), (跪姿撅臀:3.0), (全身照:3.0)"""
```

#### 5. negative 不加 anime/cartoon/3D（反作用）

z-image 写实分布和 anime tag 解耦——加 `(anime:1.5)` negative 反而把整个分布往均值（甜系真人）推。**只保留 quality/族裔反义**即可。

---

## 权重语法（与 NovelAI 不同！）

| 引擎 | 加权 | 减权 |
|---|---|---|
| NovelAI | `1.5::detailed::` | `0.5::coat::` |
| **ComfyUI/SD（本 skill 用）** | `(detailed:1.5)` | `(coat:0.5)` |

### 权重数值范围

- **可用范围**：`0.5 - 1.5`（z-image turbo 对 1.8+ 容易溢出/过曝/手脚畸形）
- **核心元素**：`1.3 - 1.5`
- **重要细节**：`1.1 - 1.3`
- **环境元素**：`0.8 - 1.2`（环境通常无需加权，除非 img2img 对抗 anchor）
- **降权弱化**：`0.5 - 0.8`

### 推荐分类数值（按 tag 类型分档）

| 分类 | 数值 | 说明 |
|---|---|---|
| `main_character` | `1.3 - 1.5` | 主角色（1girl / 主体身份） |
| `minor_character` | `1.0 - 1.2` | 次角色 |
| `poses` | `1.3 - 1.5` | 姿势/动作 |
| `scene` (txt2img) | `1.0 - 1.2` | 场景，自然出 |
| `scene` (img2img) | **`1.4 - 1.5`** | **img2img 必须强调对抗 anchor** |
| `atmosphere` | `1.0 - 1.2` | 氛围/灯光 |
| `details` | `1.2 - 1.3` | 服装/表情/物品 |
| `nsfw_action` | `1.3 - 1.5` | NSFW 关键动作 |

### 加权原则

- **不要把所有 tag 都加权**——只给以下部分加权：
  - 主角色身份/外观（特别是面部 / 身材关键词）
  - 主动作 / 关键姿势
  - 主场景（img2img 必须，txt2img 视情况）
  - 关键镜头（POV / close-up / wide shot 等）
  - 关键细节（NSFW 动作 / 特殊服装 / 重要物品）
- 修饰词、语气词、副词不加权
- `quality_prefix`（已在 `_global.yml.positive_prefix` 里）不要重复加权
- **⚠️ 年龄/类型词必须加权 1.4-1.5**：`(38 year old:1.5)` `(mature woman:1.5)` `(teenager:1.5)` `(middle-aged:1.4)` —— 否则模型默认出 20 岁年轻甜美脸，所有人都长一样

### 高权重外观示例

```
(long_wavy_black_hair:1.3)
(side_braid:1.4)
(curvy_figure:1.3)
(large_breasts:1.4)
(small_dimples:1.2)
(peach_blossom_eyes:1.3)
(silver_cross_necklace:1.3)
(bodycon_dress:1.4)
(school_uniform:1.4)
(JK_uniform:1.4)
```

### 表情/情绪示例

```
(shy_expression:1.2), (blushing:1.3), (looking_at_viewer:1.2)
(seductive_smile:1.3), (lustful_expression:1.4)
(crying:1.3), (tears_in_eyes:1.2)
(open_mouth:1.2), (heavy_breathing:1.3)
(submissive_pose:1.4), (eyes_half_closed:1.3)
```

### 姿势/动作示例

```
(POV:1.3), (close-up:1.4), (medium_shot:1.3), (wide_shot:1.3)
(taking_selfie:1.3), (looking_back_over_shoulder:1.3)
(sitting_on_bed:1.3), (lying_on_bed:1.3), (on_all_fours:1.4)
(spread_legs:1.5), (kneeling:1.4)
(holding_phone:1.2), (cooking:1.4)
```

### 场景/位置示例（img2img 时必须加权对抗 anchor）

```
(bedroom:1.3), (kitchen:1.4), (classroom:1.4), (office:1.3)
(public_bathroom:1.4), (school:1.3), (street:1.4), (park:1.4)
(outdoor:1.5), (indoor:1.2), (mirror_reflection:1.3)
```

### 服装变化示例（img2img 时为换装必须加权）

```
(apron:1.3), (lingerie:1.4), (bikini:1.4), (cosplay:1.4)
(naked:1.4), (topless:1.4), (uniform:1.4)
(tight_dress:1.3), (silk_stockings:1.3)
```

### NSFW 动作示例（NSFW 场景关键加权）

```
(masturbation:1.4), (touching_self:1.4)
(blowjob:1.4), (vaginal_penetration:1.5)
(cum_on_face:1.4), (cum_on_breasts:1.4)
(spread_pussy:1.5), (anal:1.4)
```

### 灯光/氛围示例

```
(soft_lighting:1.2), (warm_lamp_light:1.2), (moonlight:1.3)
(dim_lighting:1.2), (dramatic_lighting:1.3), (backlighting:1.3)
(golden_hour:1.3), (sunset:1.3), (night_scene:1.2)
```

### 完整加权 prompt 示例（img2img 模式）

**场景**：让 anchor 是"室内白衣自拍熟女"的 bot2 出"在厨房做饭穿围裙"
```
(POV:1.3), (medium_shot:1.3),
1girl, (cooking:1.4), in (kitchen:1.4),
(apron:1.3), (looking_back_over_shoulder:1.2),
(soft_warm_light:1.2),
kitchen counter, vegetables, knife on cutting board
```

**场景**：让 bot2 出"夜里独处发情自拍勾引主人"
```
(POV:1.3), (close-up:1.4), 1girl, nsfw,
(lying_on_bed:1.3), (blushing:1.3), (lustful_expression:1.4),
(naked:1.4), (large_breasts:1.4), (touching_self:1.4),
(silver_cross_necklace:1.3),
(bedroom:1.3), (dim_lighting:1.2), (moonlight:1.3),
night, mirror selfie
```

## ⚠️ img2img 模式下的额外规则（关键！）

当用户切到 comfyui 时，框架**自动用 anchor 图做 img2img**（denoise=0.85）。
这意味着 **anchor 图的人脸特征保留~70%，场景/构图按 prompt 替换（denoise=0.85 甜点）**，prompt 的场景词如果不加权重会**被 anchor 压住**。

**症状**：用户说"在厨房做饭"但出图还是 anchor 的卧室自拍 → 场景词被吞了。

**对策——img2img 时场景元素必须强力加权（1.4-1.5）对抗 anchor**：

| 场景类型 | 必加权重 | 例子 |
|---|---|---|
| 室内换地点 | `(kitchen:1.4)` `(office:1.4)` `(classroom:1.4)` | 想从卧室换厨房 |
| 户外场景 | `(outdoor:1.5)` `(street:1.4)` `(park:1.4)` | 想去外面 |
| 服装变化 | `(apron:1.3)` `(school uniform:1.4)` `(bikini:1.4)` | 想换衣服 |
| 动作变化 | `(cooking:1.4)` `(running:1.4)` `(reading:1.3)` | 不再"selfie" |
| 镜头切换 | `(full body shot:1.4)` `(wide angle:1.5)` | 不再 close-up |

**反面例子**（worker 之前实际写的，错的）：
```
POV close-up selfie, girl taking selfie at home with soft afternoon light, 1girl
```
→ 没任何加权，anchor 是"室内自拍"，输出几乎复刻 anchor。

**正确写法**（img2img 想换厨房做饭场景）：
```
(POV:1.3), (medium shot:1.3), 1girl (cooking:1.4) in (kitchen:1.4),
(apron over sweater:1.3), (looking back over shoulder:1.2),
warm afternoon light, kitchen counter with vegetables
```
→ 场景词全部 1.3-1.4 加权，hard 对抗 anchor 的"室内自拍"惯性。

**判断要不要 img2img 加权**：调脚本时**只要传了 --init-image** 就走 img2img → 用本节规则。
（comfyui_gen.py 自动从 yml 读 anchor，所以**默认就是 img2img**——除非 bot yml 没配 anchor_image）

### ⚠️ 反复刻 anchor 硬规则

worker 容易陷入：anchor 是室内自拍 → 我也写自拍 → 出图永远跟 anchor 一样。**这是错的，毫无价值。**

**判断逻辑**：
- 用户明说场景（在厨房/教室/外面）→ 强力加权该场景 (kitchen:1.5)
- 用户没明说 → **主动选一个跟 anchor 不一样的场景**，让出图有变化
  - anchor 是卧室自拍 → 你画客厅沙发上 / 阳台 / 浴室对镜 / 厨房 等
  - anchor 是站立全身 → 你画坐姿 / 躺姿 / 跪姿
  - 加权该选择的场景 1.4-1.5
- **禁止**：直接复刻 anchor 类型场景（selfie at home 是 anchor 已有，再写一遍没意义）

**例子** —— 用户随口说"画一张"：
- 错：`POV selfie at home, 1girl, lingerie...` （跟 anchor 重复）
- 对：`(medium shot:1.3), 1girl in (living room:1.4) (sitting on sofa:1.4), (silk robe:1.4) loose, evening lamp light` （主动换场景换姿势）

## prompt 撰写顺序（强制）

第一段固定为：**镜头视角 + 角色动作 + 场景 + 位置 + 灯光**，例如：
```
POV close-up shot, mature woman taking a selfie in bedroom by mirror with warm lamp light
```

之后接：
1. 人物总数：`1girl` / `1boy, 1girl`
2. 人物外观：发色、瞳色、身材（按需加权）
3. 服装
4. 表情/动作
5. 风格 / NSFW 标签
6. 时间 / 氛围

## 多角色

ComfyUI/SD CLIP 用 `BREAK` 关键字分隔不同主体（NovelAI 用 `|`）：

```
POV in bedroom on bed with moonlight, 2girls, BREAK
1girl, long_silver_hair, blue_eyes, lingerie, BREAK
1girl, short_black_hair, kneeling, looking at viewer
```

## NSFW 处理
- 主 prompt 第一行后立刻加 `nsfw,`
- 不要写性器官的具体细节（LoRA 会处理），写动作 + 环境 + 表情
- 例：
  ```
  POV close-up, woman lying on bed at night with dim lighting,
  nsfw, 1girl, (mature female:1.3), naked, blushing, looking at viewer,
  bedroom, night, moonlight from window
  ```

## 镜头视角示例
- `POV` / `POV close-up shot`
- `Third-person side view`
- `Low-angle shot` / `High-angle shot` / `Bird's eye view`
- `Over-the-shoulder shot`
- `Wide shot` / `Medium shot` / `close-up`

## 灯光示例
- `moonlight` / `soft afternoon light` / `dramatic lighting` / `dim lighting` / `backlighting` / `warm lamp light`

## 中文 → 英文翻译规则

接到的输入通常是中文场景（朋友圈文案 / 用户消息）。转换思路：

1. 提取**核心动作**（"自拍" → `taking a selfie`）
2. 提取**场景**（"卧室对镜" → `in bedroom by mirror`）
3. 提取**情绪/氛围**（"羞涩" → `(shy expression:1.2), blushing`）
4. **省略**抽象抒情（"心情很好" 不直接翻，体现在表情/光线）
5. **不要照译**比喻和文学手法（"月亮像玉盘" → `bright full moon`）

## 完整示例

输入（中文）：
> 新买的内衣到货，对着镜子拍了好几张

输出 prompt：
```
POV close-up mirror selfie, woman taking selfie in bedroom by mirror with warm lamp light,
nsfw, 1girl, (mature female:1.2), (lingerie:1.3), (looking at viewer:1.2),
bedroom, mirror reflection, soft skin, evening
```

输入（中文）：
> 今天教室空调坏了，全班对着电扇背单词

输出 prompt：
```
Wide shot of a noisy classroom, multiple students sitting at desks with electric fans,
1boy, 1girl, school uniform, summer, bright daylight,
chalkboard, books and notebooks on desks
```

## 与 NovelAI skill 的对应表

| 项 | NovelAI | ComfyUI |
|---|---|---|
| 入口脚本 | `~/.claude/skills/novelai-skill/scripts/generate_novelai_image.py` | `$CLAUDEBOTLIFE_REPO/scripts/comfyui_gen.py` |
| 权重 | `1.5::xxx::` | `(xxx:1.5)` |
| 多角色 | `\|` | `BREAK` |
| 配置 | skill 自带 default_config.json | `_global.yml.image_generation` |
| 固定前缀 | NovelAI 配置里的 positive/negative_prefix | `_global.yml.positive_prefix` / `negative_prefix` |
| 模型 | NovelAI 4.5 full（云端） | z_image_turbo / 用户配的 workflow |

## 切换 provider
```bash
# 切到 comfyui
curl -s -X POST http://127.0.0.1:8765/api/image_provider \
  -H "Content-Type: application/json" -d '{"provider":"comfyui"}'

# 切回 novelai
curl -s -X POST http://127.0.0.1:8765/api/image_provider \
  -H "Content-Type: application/json" -d '{"provider":"novelai"}'

# 查当前
curl -s http://127.0.0.1:8765/api/image_provider
```

## 失败兜底
- ComfyUI 没启动 → `comfyui_gen.py` 会报 connection refused
- workflow JSON 路径错 → 报 `workflow 不存在`
- 240 秒超时（NSFW 多 LoRA 工作流可能 60-90s 出图）
- 任何失败：朋友圈仍会发文字版（image_path 留空）

## 禁止事项
- 不要假装出图（必须真调脚本，再真的 `reply(files=...)` 把图发出去，才对用户说话）
- 不要把脚本输出 `MEDIA: ...`、命令、路径、报错栈直接发给用户；图只能走 `reply` 的 `files` 参数
- 不要复用旧路径 / 旧图，每次都出全新的
- 不要在 prompt 里写中文（除 z-image 自然语言句式那部分）
