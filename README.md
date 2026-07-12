# claude-tgbot · 数字生命 Telegram bot 引擎

用 Claude Code 当"大脑"驱动的 Telegram 角色扮演机器人。每个机器人是一个持续运行的 Claude 会话，有人设、记忆、情绪，还有随时间变化的「好感/信任/NSFW/精力」四个关系数值，会像真人一样有分寸地回应——不随叫随到、也不无脑拒绝。

内附一个示例人设 **陈露露**（35 岁小学语文老师，慢热高冷，从陌生人起步）演示整套机制。

> ⚠️ 成人向角色扮演项目，仅供授权的个人娱乐用途。请遵守当地法律与 Telegram / 模型服务条款。
>
> ✅ **单 bot 私聊**（主链路）已在 macOS 与原生 Windows 双平台端到端验证通过。
> 🧪 **朋友圈生图、多 bot 群聊导演**是进阶功能，代码齐全但尚未在所有平台完整跑通，用到时留意。

---

## 先搞懂几个词（不然后面看不懂）

| 词 | 大白话 |
|----|--------|
| **仓库 / repo** | 就是这堆代码。用 `git clone` 下载到你电脑上。 |
| **Telegram bot token** | 你在 Telegram 里找 `@BotFather` 申请机器人后，它给你的一串密码（形如 `12345:AAxxxx`）。程序拿它替你收发消息。 |
| **你的 Telegram user_id** | 你自己账号的数字 ID（不是用户名）。用来限定「只有你能跟这个 bot 私聊」。查法：Telegram 里找 `@userinfobot` 发条消息，它回你的 id。 |
| **DeepSeek key** | 去 [platform.deepseek.com](https://platform.deepseek.com) 注册拿的 API key（形如 `sk-xxxx`）。程序用它在后台给对话打情绪分、算关系数值。**和下面的 Claude provider 是两回事。** |
| **Claude provider** | 机器人「说话」用哪个模型。可以是你的 Claude 官方订阅，也可以是第三方中转站。详见后面「切换模型来源」。 |
| **dispatcher** | 常驻后台的收发进程，每个 bot 一个。它收 Telegram 消息、唤起 Claude 生成回复。 |
| **worker** | 真正「扮演角色说话」的那个 Claude 进程，由 dispatcher 按需拉起。 |

---

## 开始之前：装 3 样东西

无论 Mac 还是 Windows，先装好这三个（都是一次性的）：

1. **Python 3.10 以上** —— [python.org](https://www.python.org/downloads/) 下载安装（Windows 安装时勾选 "Add Python to PATH"）。
2. **Bun** —— 一个 JS 运行时，跑收发进程用。装法见 [bun.sh](https://bun.sh)。
3. **Claude Code CLI** —— 机器人的大脑。装好后在终端跑一次 `claude` 登录你的 Claude 账号（或按后面「切换模型来源」配中转站）。

装完在终端敲 `python --version`、`bun --version`、`claude --version`，三条都能打出版本号就算齐了。

---

## 需要哪些密钥（钥匙清单）

### 必需的 3 把（不配 bot 跑不起来）

| 密钥 | 配在哪 | 干什么 |
|------|--------|--------|
| **Telegram bot token** | `channels/<bot>/.env` 的 `TELEGRAM_BOT_TOKEN` | 收发 Telegram 消息（每个 bot 一把，找 `@BotFather` 拿） |
| **DeepSeek key** | `configs/_global.yml` 的 `jiwen.delta_llm.api_key` | 后台给对话打情绪分、算关系数值、群聊导演、主动消息 |
| **Claude provider** | `~/.claude/settings.json` 或直接 `claude` 订阅登录 | 机器人「说话」的模型。见下方「切换模型来源」 |

### 可选的（对应功能才需要，不用就不配）

| 密钥/服务 | 配在哪 | 什么时候需要 |
|-----------|--------|--------------|
| **NovelAI token** | 朋友圈网页(`:8765`)的设置页填写 → 写进 novelai-skill 的 `.env.local` | 仅当你要**用 NovelAI 生图**（自拍/配图）。生图 skill **仓库已自带**（`skills/novelai-skill/`），把它复制到 `~/.claude/skills/` 即可用 |
| **ComfyUI** | `configs/_global.yml` 的 `moments.image_generation.comfyui.url` | 用本地 ComfyUI 生图时。是本地服务，**无需 key**，只填地址（默认 `127.0.0.1:8188`） |

**不需要 key 的**：天气（wttr.in）、RSS 热榜（rsshub）、节假日（timor.tech）——都是免费公开接口。

> 小结：**只想让 bot 能私聊聊天** → 配前 3 把里的 Telegram token + DeepSeek key + Claude 登录，就够了。生图、朋友圈是锦上添花，用到再配。

### 每把钥匙具体怎么弄

**① Telegram bot token + 你的 user_id**
1. Telegram 里搜 **@BotFather**，发 `/newbot`，按提示起个名字和用户名 → 它回给你一串 token（形如 `12345678:AAxxxx`），这就是 `TELEGRAM_BOT_TOKEN`。
2. 再搜 **@userinfobot**，随便发条消息，它回你的数字 **user_id**（填进 `access.json` 的 `allowFrom`）。

**② DeepSeek key**
1. 打开 [platform.deepseek.com](https://platform.deepseek.com)，注册/登录。
2. 充值几块钱（后台打分很省，一个月几毛钱）。
3. 左侧「API keys」→「创建 API key」→ 复制那串 `sk-xxxx`，填进 `configs/_global.yml`。

**③ Claude provider（机器人说话用的模型）—— 二选一**
- **官方订阅**：有 Claude Pro/Max 订阅的话，终端跑 `claude` 登录一次即可，不用填 key。
- **第三方中转（没有官方订阅走这个）**：某宝 / 发卡网搜「Claude API 中转」「Claude 镜像站」买一个中转 key，会给你一个**中转地址**和一把 **key**，按下方「切换模型来源」的 B 方案填进 `settings.json`。⚠️ 认准支持 **Claude Messages API 格式**的中转（多数都支持），纯 ChatGPT/OpenAI 格式的不能用。

**④ NovelAI token（可选，只有要 NovelAI 生图才需要）**
1. 需要一个 NovelAI 订阅账号：官网 [novelai.net](https://novelai.net) 自己订阅（要外币卡/PayPal），或**某宝搜「NovelAI 账号」直接买一个已订阅的**（便宜省事）。
2. 拿到账号后登录 novelai.net → 右上头像 → **Account / User Settings** → 找 **"Get Persistent API Token"** 生成一串长 token。
3. 把生图 skill 复制到 Claude 的 skills 目录（仓库已自带，脱敏干净）：
   - Mac/Linux：`cp -r skills/novelai-skill ~/.claude/skills/`
   - Windows：`Copy-Item -Recurse skills\novelai-skill $env:USERPROFILE\.claude\skills\`
4. 这串 token 填进朋友圈网页（`http://localhost:8765` 的设置页），程序会写进 skill 的 `.env.local`。
> 只想聊天不生图的话，这几步整个跳过。生图默认走 NovelAI；也可改用本地 ComfyUI（`_global.yml` 里配）。

---

## 换成你自己的人设（重要！别一直用陈露露）

**陈露露只是个示例。** 你几乎肯定想要自己的角色。人设写在两个地方：

| 文件 | 改什么 |
|------|--------|
| `channels/<bot>/CLAUDE.md` 的 **`# SOUL.md`** 和 **`# USER.md`** 段 | 角色的性格、说话方式、和你的关系、怎么称呼你——**这是核心** |
| `configs/<bot>.yml` | `display_name`(显示名)、`user_address`(角色怎么叫你)、`bio`、`persona_summary`(一句话摘要) |

（`CLAUDE.md` 最上面的 `# AGENTS.md` 段是通用回复规则，不用动。）

### 两条路，挑一条

**路 A — 让 AI 帮你写（最省事，推荐给小白）**
把下面这段丢给 ChatGPT 或 Claude，描述你想要的角色，让它生成，然后把结果贴进 `channels/chenlulu/CLAUDE.md` 的 `# SOUL.md` 段（替换掉陈露露那部分）：
> 「我在做一个 Telegram 角色扮演 bot。请帮我写一份角色人设，包含这些小节：核心设定、外貌特征、性格特征、说话特点、行为准则（含对 NSFW 内容的态度）、关系网（角色和用户是什么关系）、3-4 组示例对话。我想要的角色是：**<在这里描述你的角色：名字、年龄、身份、性格、和你的关系>**。语言口语化、有分寸、像真人。」

**路 B — 照模板手动填**
仓库里有个填空模板 `channels/_persona_template/`，每一段都标了「这里写什么」。复制它建自己的角色：
```bash
# Mac/Linux（把 myrole 换成你的角色英文名）
cp -r channels/_persona_template ~/.claude/channels/myrole
# 然后用编辑器打开 ~/.claude/channels/myrole/CLAUDE.md，把每个 < > 占位换成你的角色
```
```powershell
# Windows
Copy-Item -Recurse channels\_persona_template $env:USERPROFILE\.claude\channels\myrole
notepad $env:USERPROFILE\.claude\channels\myrole\CLAUDE.md
```
用新角色名（如 `myrole`）时，记得同步：`configs/myrole.yml`（复制 `configs/_example.yml` 改）、两处 `BOT_NAMESPACES`、启动脚本的端口——详见文末「加更多机器人」。

> **最简单的玩法**：不想新建，直接改 `channels/chenlulu/CLAUDE.md` 的 `# SOUL.md`/`# USER.md` 两段，把陈露露换成你的角色即可，其它配置都不用动。改完重启一次 bot 生效。

---

## 部署（Mac / Linux）

> 下面每一行 `#` 后面是"这条命令干什么"的说明。`cd 某目录` = "进入某个文件夹"。
> 仓库可以放任何位置，本文统一放在用户主目录下的 `claudebotlife` 文件夹（即 `~/claudebotlife`）。

```bash
# ① 下载代码到 ~/claudebotlife 这个文件夹，然后进入它
git clone -b feature/cross-platform https://github.com/euphoriaaaaaa1/claude-tgbot.git ~/claudebotlife
cd ~/claudebotlife

# ② 装 Python 依赖 + 装 dispatcher 的 JS 依赖
pip install -r requirements.txt
cd dispatcher && bun install && cd ..     # 进 dispatcher 装依赖，再退回上级目录

# ③ 全局配置：填你的 DeepSeek key
cp configs/_global.example.yml configs/_global.yml   # 复制示例配置为正式配置
nano configs/_global.yml                             # 打开编辑，把 jiwen.delta_llm.api_key 改成你的 sk-xxxx

# ④ 把示例机器人「陈露露」的资料放到 Claude 的 channels 目录
mkdir -p ~/.claude/channels
cp -r channels/chenlulu ~/.claude/channels/          # 复制整个 chenlulu 文件夹过去

# ⑤ 配置这个机器人的 token 和白名单
cd ~/.claude/channels/chenlulu
cp .env.example .env
nano .env                # 把 TELEGRAM_BOT_TOKEN 改成你从 @BotFather 拿的 token
nano access.json         # 把 allowFrom 里的 YOUR_TELEGRAM_USER_ID 改成你自己的数字 user_id
cd ~/claudebotlife       # 回到仓库目录

# ⑤.5 换成你自己的人设（强烈建议！别一直用陈露露）——详见下方「换成你自己的人设」
#      最省事：编辑器打开 ~/.claude/channels/chenlulu/CLAUDE.md，改里面的 # SOUL.md 段

# ⑥ 启动！
cp restart-bots.example.sh restart-bots.sh
bash restart-bots.sh     # 起后台收发进程；之后在 Telegram 给你的 bot 发消息就有回应了

# ⑦（可选）开启情绪/关系数值引擎，每 5 分钟算一次
python3 jiwen/tick.py    # 想长期自动跑，就挂到 launchd / cron 定时任务
```

看实时对话：`bash scripts/watch-bot.sh chenlulu`

---

## 部署（Windows 10/11，原生，不需要 WSL）

> 在 **PowerShell** 里跑（开始菜单搜 "PowerShell"）。`cd 某目录` = "进入某个文件夹"。
> 本文把仓库放在 `你的用户目录\claudebotlife`（即 `$env:USERPROFILE\claudebotlife`）。

```powershell
# ① 下载代码到 用户目录\claudebotlife，然后进入它
git clone -b feature/cross-platform https://github.com/euphoriaaaaaa1/claude-tgbot.git $env:USERPROFILE\claudebotlife
cd $env:USERPROFILE\claudebotlife

# ② 装 Python 依赖 + dispatcher 的 JS 依赖
pip install -r requirements.txt
cd dispatcher; bun install; cd ..        # 进 dispatcher 装依赖，再退回上级目录

# ③ 全局配置：填 DeepSeek key
Copy-Item configs\_global.example.yml configs\_global.yml
notepad configs\_global.yml              # 记事本打开，把 jiwen.delta_llm.api_key 改成你的 sk-xxxx，存盘

# ④ 把示例机器人「陈露露」的资料放到 Claude 的 channels 目录
mkdir $env:USERPROFILE\.claude\channels -Force
Copy-Item -Recurse channels\chenlulu $env:USERPROFILE\.claude\channels\

# ⑤ 配置这个机器人的 token 和白名单
cd $env:USERPROFILE\.claude\channels\chenlulu
Copy-Item .env.example .env
notepad .env                # 把 TELEGRAM_BOT_TOKEN 改成 @BotFather 给的 token
notepad access.json         # 把 allowFrom 里的 YOUR_TELEGRAM_USER_ID 改成你自己的数字 user_id
cd $env:USERPROFILE\claudebotlife    # 回到仓库目录

# ⑤.5 换成你自己的人设（强烈建议！别一直用陈露露）——详见下方「换成你自己的人设」
#      最省事：notepad $env:USERPROFILE\.claude\channels\chenlulu\CLAUDE.md，改里面的 # SOUL.md 段

# ⑥ 启动！（首次若提示"禁止运行脚本"，前面加 -ExecutionPolicy Bypass）
powershell -ExecutionPolicy Bypass -File windows\start-bots.ps1

# ⑦（可选）注册后台任务：开机自启 + 主动消息 + 情绪引擎 + 朋友圈 + 记忆压缩
notepad windows\register-tasks.ps1       # 先把里面的 YOUR_TELEGRAM_USER_ID 改成你的 user_id，存盘
powershell -ExecutionPolicy Bypass -File windows\register-tasks.ps1
```

看实时对话：`powershell -File windows\watch-bot.ps1 chenlulu`
常用运维：`windows\restart-bots.ps1` 重启全部 · `windows\unregister-tasks.ps1` 卸载后台任务。

**先自测环境（强烈建议，不碰 Telegram）**：`cd dispatcher; bun test-e2e.ts` —— 打出 `5 过 / 0 挂` 说明这台机器整条链路 OK。

---

## 查看后台对话（所有平台）

worker 是后台进程，没有能 attach 的窗口——看 `channels/<bot>/logs/` 下的日志：

- **`chat.log`** —— 人话对话流（谁说了什么、调了什么工具）。这就是"实时看消息"，等价旧的 `tmux attach`：
  - Mac/Linux：`bash scripts/watch-bot.sh chenlulu`（或 `tail -f ~/.claude/channels/chenlulu/logs/chat.log`）
  - Windows：`powershell -File windows\watch-bot.ps1 chenlulu`
- **`stream.jsonl`** —— 原始事件流，出 bug 时看这个（含报错、每轮耗时/成本）。

> **Windows 日志中文乱码？** 日志文件本身是 UTF-8、没坏（也不影响 AI 回复）——乱码只是 PowerShell 5.1 默认按 GBK 读文件。`watch-bot.ps1` 已内置 `chcp 65001` + `-Encoding UTF8` 修好了。若你直接 `Get-Content` 看，请加 `-Encoding UTF8`；想让 emoji（👤🤖⚙）也正常显示，用 **Windows Terminal** 而不是老式 cmd 窗口。
- **翻历史 / 手动介入**：先停掉该 bot（重启脚本，或 `/status` 查到 pid 后结束进程），再 `claude --resume <会话uuid>` 打开同一会话手动聊；退出后 worker 会在下条消息自动复活。⚠️ 同一会话两端不能同时开。

---

## 装完自检 + 常见报错速查

**先自检环境**（不碰 Telegram，最快确认整条链路 OK）：
```bash
cd dispatcher && bun test-e2e.ts     # Windows: cd dispatcher; bun test-e2e.ts
```
打出 **`5 过 / 0 挂`** = 这台机器能跑。有红叉照下表查。

| 症状 | 原因 / 解法 |
|------|-------------|
| `bun: command not found` / `claude: command not found` | 装完没刷新 PATH → **重开一个终端窗口**再试。Windows 上 claude 若还找不到，设环境变量 `CLAUDE_BIN` 指向 `claude.cmd` 全路径。 |
| `pip install` 卡在 `chinese_calendar` 或 `Pillow` | 这俩是可选（只管节假日判定/生图）。装不上就先跳过，不影响私聊：`pip install feedparser PyYAML lunardate requests flask` 单独装必需的即可。 |
| dispatcher 起来了，但**给 bot 发消息没反应** | ① `access.json` 的 `allowFrom` 是不是填了**你自己的**数字 user_id（不是用户名）② `.env` 的 `TELEGRAM_BOT_TOKEN` 对不对 ③ 看 `channels/<bot>/logs/chat.log`（新架构）或 `tmux attach`（旧架构）有没有收到消息。 |
| bot 收到了但**不回复 / 报错** | 多半是 Claude provider 没配好 → 终端跑一次 `claude` 确认能对话；或看 `logs/stream.jsonl` 里的报错。 |
| **导演/情绪/主动消息不工作** | `configs/_global.yml` 的 DeepSeek `api_key` 没填或填错。 |
| 端口被占 `EADDRINUSE` / `address in use` | 已有一个 dispatcher 在跑同端口 → 先停掉（`restart-bots.sh` / `windows\restart-bots.ps1`）再起。 |
| Windows 日志乱码 | 见上方「查看后台对话」——用 `watch-bot.ps1`，或 `Get-Content -Encoding UTF8`，emoji 用 Windows Terminal。 |

> **让 AI 帮你部署时**：直接把本 README 甩给它，让它照着一步步来。到「填 key」那几步（DeepSeek key / Telegram token / user_id）AI 会停下来问你要——这些只有你能提供，按上方「每把钥匙具体怎么弄」拿到后给它即可。

## 切换模型来源（Provider）

系统有**两处互相独立的模型来源**，分开配、别搞混：

| 用途 | 用谁 | 配在哪 |
|------|------|--------|
| 角色说话（worker） | `claude` CLI | `~/.claude/settings.json` 的 `env` |
| 后台情绪/关系数值裁判 + 群聊导演 | DeepSeek（独立） | `configs/_global.yml` 的 `jiwen.delta_llm` |

换角色说话用的模型/中转，只改 `~/.claude/settings.json`。三种配法任选一种：

- **A. 官方订阅（最省事）**：`claude` 登录一次 Claude 账号即可；`settings.json` 的 `env` **不要**写 `ANTHROPIC_BASE_URL`。worker 自动读系统凭证并续期。
- **B. 第三方中转 / API key（手动）**：编辑 `~/.claude/settings.json`：
  ```json
  {
    "env": {
      "ANTHROPIC_BASE_URL": "https://你的中转站/api",
      "ANTHROPIC_AUTH_TOKEN": "sk-你的中转key",
      "ANTHROPIC_MODEL": "claude-sonnet-4-5"
    }
  }
  ```
- **C. [cc-switch](https://github.com/farion1231/cc-switch)（图形界面，不想手改用它）**：加个 provider 点切换，它替你写 `settings.json`。
  > ⚠️ 别同时用多个「写 settings.json」的工具（cc-switch / 手改会互相覆盖），选一个。

**切完必须重启一次**（worker 只在启动时读 settings.json）：`bash restart-bots.sh`（Windows：`windows\restart-bots.ps1`）。
另外要能用还需：① `configs/_global.yml` 的 DeepSeek key 已配好；② 中转站支持 Claude 的 **Messages API** 格式（多数 claude 中转支持，纯 OpenAI 格式的不行）。

---

## 关系数值怎么工作（治"随叫随到"）

每个 bot 一份 `channels/<bot>/relationship.json`，四个数值 0–100：**好感 / 信任 / NSFW / 精力**。

- **信任** 决定关系阶段（陌生 → 客气 → 多聊 → 暧昧 → 亲密），门槛没到不会跳级。
- **NSFW** 没起来时 bot 不主动，需要被撩、有情境；夜里有随机小高峰。
- **精力** 随作息+当日活动+随机波动：晚上高、后半夜 3–6 点低谷；低了会**温柔婉拒并给补偿**，不是冷脸拒。
- 数值由 `jiwen/tick.py` 后台读对话涨跌，每个 bot 各算各的，不会时刻相同。

陈露露出厂是陌生人（好感 5 / 信任 5 / NSFW 0），需要慢慢处。

---

## 加更多机器人

1. `cp configs/_example.yml configs/<新bot>.yml`，改里面的人设摘要。
2. `cp -r channels/chenlulu ~/.claude/channels/<新bot>`，改 `CLAUDE.md`(人设)、`access.json`、`.env`，删掉 `relationship.json`（让它按新关系重新起）。
3. **两处 UUID 命名空间必须各加一行且完全一致**（否则会读错记忆）：`dispatcher/worker-manager.ts` 的 `BOT_NAMESPACES`、`chat_history.py` 的 `_BOT_NAMESPACES`。
4. 启动脚本里加这个 bot 的端口：`restart-bots.sh` 的 `BOTS`（Windows：`windows/start-bots.ps1` 的 `$Bots`）。
5. 多 bot 群聊由 `director.py` 调度（可选，单 bot 用不到）。

---

## 目录速览

```
dispatcher/     dispatcher.ts / worker-manager.ts / worker-plugin.ts —— Telegram 收发 + worker 托管
windows/        Windows 部署脚本（start / restart / register-tasks / watch-bot）
scripts/        watch-bot.sh(看实时对话) / self_initiate.py(主动消息) + 朋友圈生图等运维脚本
jiwen/          情绪引擎 + DeepSeek 关系数值裁判 + 5 分钟 tick
relationship.py 四维关系数值（作息精力 / NSFW 波动 / 分寸提示）
director.py     多 bot 群聊导演（可选）
moments/        朋友圈网页（Flask，可选）
generators/     情境 / 心情 / 世界事件生成器
configs/        _global.yml + 每个 bot 一个 yml（模板见 _example.yml）
channels/chenlulu/  示例人设（CLAUDE.md 人设 + access.json + 出厂关系种子）
channels/_persona_template/  填空人设模板（做自己的角色照它填）
skills/novelai-skill/  NovelAI 生图 skill（可选，复制到 ~/.claude/skills/ 启用）
```

---

## 安全

`.env`、`configs/_global.yml`（含 DeepSeek key）、运行时的 inbox/记忆/日志都在 `.gitignore` 里，不会被提交。fork 或改动后请自查，别把 key / token 传上仓库。
