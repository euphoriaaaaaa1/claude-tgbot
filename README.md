# claude-tgbot · 数字生命 Telegram bot 引擎

用 Claude Code 当"大脑"驱动的 Telegram 角色扮演机器人。每个机器人是一个持续运行的 Claude 会话，有人设、记忆、情绪，还有随时间变化的「好感/信任/NSFW/精力」四个关系数值，会像真人一样有分寸地回应——不随叫随到、也不无脑拒绝。

内附一个示例人设 **陈露露**（35 岁小学语文老师，慢热高冷，从陌生人起步）演示整套机制。

> ⚠️ 成人向角色扮演项目，仅供授权的个人娱乐用途。请遵守当地法律与 Telegram / 模型服务条款。

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
| **NovelAI token** | 朋友圈网页(`:8765`)的设置页填写 → 写进 novelai-skill 的 `.env.local` | 仅当你要**用 NovelAI 生图**（自拍/配图）。⚠️ 生图还需另外装 `novelai-skill`，本仓库不含它 |
| **ComfyUI** | `configs/_global.yml` 的 `moments.image_generation.comfyui.url` | 用本地 ComfyUI 生图时。是本地服务，**无需 key**，只填地址（默认 `127.0.0.1:8188`） |

**不需要 key 的**：天气（wttr.in）、RSS 热榜（rsshub）、节假日（timor.tech）——都是免费公开接口。

> 小结：**只想让 bot 能私聊聊天** → 配前 3 把里的 Telegram token + DeepSeek key + Claude 登录，就够了。生图、朋友圈是锦上添花，用到再配。

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
```

---

## 安全

`.env`、`configs/_global.yml`（含 DeepSeek key）、运行时的 inbox/记忆/日志都在 `.gitignore` 里，不会被提交。fork 或改动后请自查，别把 key / token 传上仓库。
