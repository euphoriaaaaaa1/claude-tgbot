# claude-tgbot

把 Claude Code 变成一个有长期记忆、生活作息和关系状态的 Telegram 角色。它在 Telegram 里和你私聊、在群里和其他 bot 聊天，也能发朋友圈、发语音、按人设生图。

> **本仓库不附带现成人设。** 新建 bot 时只有一份空白模板，角色、说话方式、边界都由你自己写。仓库里的 `channels/_persona_template/` 也只是空壳。

---

## 1. 它由哪几块组成

| 模块 | 作用 |
|---|---|
| `dispatcher/` | 每个 bot 一个 Telegram 长轮询进程，负责收消息、合并连发、调用 worker、投递回复 |
| `adapters/` | 8767 管理台背后的两套底层：本机 Claude Code（`claude`）和外部 dsh（`dsh`） |
| `moments/` | 8767 管理台（登录、总览、供应商、功能开关、画风、朋友圈、参数页） |
| `provider_config.py` | 供应商清单的唯一写者；页面、代理、dispatcher 都只读它写出的结果 |
| `bot_features.py` | 八项功能开关的唯一写者；同步技能文件 |
| `platform_services.py` | macOS launchd / Linux systemd --user / Windows 计划任务的统一注册入口 |
| `jiwen/`、`holiday.py`、`school_calendar.py` 等 | 情绪/关系数值、节假日、校历、生活情境 |

---

## 2. 安装（macOS / Linux）

### 2.1 前置依赖

- Python 3.10 或更新
- [Bun](https://bun.sh)
- Claude Code CLI：`npm install -g @anthropic-ai/claude-code`，装完运行一次 `claude` 完成登录
- 可选：dsh（第二底层，见 §7）

### 2.2 一条命令

```bash
git clone https://github.com/euphoriaaaaaa1/claude-tgbot.git
cd claude-tgbot
bash install.sh
```

`install.sh` 会：

1. 检查 Python / Bun / Claude CLI；
2. `pip install -r requirements.txt`，并在 `dispatcher/` 里 `bun install`；
3. 只在文件不存在时复制 `configs/_global.example.yml` → `configs/_global.yml`、`configs/providers.example.json` → `configs/providers.json`（0600）、`restart-bots.example.sh` → `restart-bots.sh`；
4. 运行 `scripts/setup_keys.sh`，逐项告诉你去哪拿、填哪里；
5. 注册登录自启（macOS launchd / Linux systemd --user），并注册 `provider-web`（8767 管理台）。

重复运行是安全的：已有的配置、`.env`、`channels/<bot>/` 不会被覆盖。只想预览：`DRY_RUN=1 bash install.sh`。

装完先设置管理台口令（没有默认口令）：

```bash
python3 -m moments.panel_auth --set-password
```

然后打开 <http://127.0.0.1:8767/login>。管理台由 `provider-web` 服务提供；若脚本提示注册失败，手动重跑：

```bash
python3 platform_services.py register provider-web --repo "$PWD"
```

首次用 `claude` 新建 bot 前，按 §5 信任 `~/.claude/channels`。

`install.sh` 不接受位置参数，只支持 macOS/Linux；Windows 用 §3 的原生步骤。

### 2.3 三把必填密钥

| 密钥 | 用途 | 填到 |
|---|---|---|
| Telegram bot token | bot 收发消息 | `~/.claude/channels/<bot>/.env` 的 `TELEGRAM_BOT_TOKEN` |
| 你的 Telegram 数字 user_id | 私聊白名单 | `~/.claude/channels/<bot>/access.json` 的 `allowFrom` |
| DeepSeek API key | 情绪分、关系数值、群聊导演、主动消息 | `configs/_global.yml` 的 `jiwen.delta_llm.api_key` |

- token：Telegram 里找 `@BotFather` → `/newbot`。
- user_id：找 `@userinfobot` 发一句话，它回的 `Id` 就是；不是 `@用户名`。
- DeepSeek key：`platform.deepseek.com` → API keys。

随时补填：

```bash
bash scripts/setup_keys.sh          # 交互式
bash scripts/setup_keys.sh --check  # 只检查
```

### 2.4 启动 bot

```bash
bash restart-bots.sh
```

日志默认在 `~/.claude/dispatcher/logs/<bot>-dispatcher.log`。停止：

```bash
bash stop-bots.sh
```

### 2.5 装完自检（三步）

1. **管理台能开**：设置口令后打开 <http://127.0.0.1:8767/login>，应看到登录页；登录后 `/console`、`/provider` 都能打开。`/login` 返回 503 表示还没设置口令，回 §4.1。
2. **服务在跑**：分别查两个服务：
   ```bash
   python3 platform_services.py status autostart --repo "$PWD"
   python3 platform_services.py status provider-web --repo "$PWD"
   ```
3. **能建出 bot**：按 §5 新建一只，总览出现这张卡片；如果“首次拉起”显示待 dispatcher，先 `bash restart-bots.sh`，再启用一次。

---

## 3. 安装（Windows 10/11，原生，不需要 WSL）

依赖同 §2.1。打开 PowerShell：

```powershell
git clone https://github.com/euphoriaaaaaa1/claude-tgbot.git
cd claude-tgbot
python -m pip install -r requirements.txt
cd dispatcher; bun install; cd ..

copy configs\_global.example.yml configs\_global.yml
# 用记事本填 DeepSeek key
notepad configs\_global.yml

# 启动 dispatcher
powershell -ExecutionPolicy Bypass -File windows\start-bots.ps1

# 启动 8767 管理台
powershell -ExecutionPolicy Bypass -File windows\start-provider-web.ps1

# 注册计划任务（登录自启、主动消息、情绪 tick、记忆压缩、8767、8770）
powershell -ExecutionPolicy Bypass -File windows\register-tasks.ps1
```

卸载计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File windows\unregister-tasks.ps1
```

提示“禁止运行脚本”时，在命令前加 `-ExecutionPolicy Bypass`。中文路径、带空格路径已在脚本里处理；仍建议把仓库放在纯英文路径下。

---

## 4. 8767 管理台与登录

管理台是本项目唯一的网页入口，默认只监听 `127.0.0.1:8767`，**没有默认口令**。

### 4.1 第一次：设置口令

```bash
python3 -m moments.panel_auth --set-password
python3 -m moments.panel_auth --show-status
```

`--set-password` 会在终端里提示输入两次口令（不回显），所以要在**交互式终端**里跑，不能把口令管道进去。口令哈希和 cookie secret 写在 `~/.claude/secrets/moments-panel.env`（0600）。设置新口令会让旧会话全部失效。

### 4.2 启动

```bash
MOMENTS_WEB_MODE=provider python3 -m moments.web
```

或注册成用户级服务：

```bash
python3 platform_services.py register provider-web --repo "$PWD"
python3 platform_services.py start provider-web --repo "$PWD"
```

打开 <http://127.0.0.1:8767/login>。未设置口令时页面会提示先跑 `--set-password`，不会以零鉴权状态开放。

| 页面 | 用途 |
|---|---|
| `/console` | bot 总览、新建、启停、归档/删除、人设、生活设定、语音、功能开关、群聊 |
| `/provider` | 供应商清单、绑定、模型、思考强度、重启、代理 |
| `/params` | `configs/_global.yml` 的参数页 |
| `/styles` | 画风（可选） |
| `/moments` | 朋友圈页 |
| `/console/stickers` | 表情包 |

> `MOMENTS_WEB_HOST` 只能填回环地址；填非回环会直接退出。不要把 8767 直接暴露到公网。

---

## 5. 新建 bot（空白模板）

推荐从管理台建：

1. 登录 8767 → `/console` → 新建；
2. 填 bot 编号（小写字母开头，2–32 位）、显示名；
3. 粘贴从 `@BotFather` 拿到的 token；
4. 填你的数字 user_id；
5. 人设选 **空白（blank）**，或直接粘贴你自己写的人设；
6. 选功能开关，保存。

编号 `jiwen`、`director`、`voicecall`、`media`、`group_transcripts`、`telegram` 等是保留字，不能用作 bot id。

> 首次用 `claude` 启动 bot 时，Claude Code 会询问是否信任工作目录；选信任 `~/.claude/channels`（所有 bot 目录都在其下），否则 bot 的文件读写会被拦截。

新建后：

- 人设：`/console` → 该 bot → 人设。空白模板只写了一句“在这里写下你的 bot 的角色、说话方式与边界”，**没有现成人设**。
- 生活设定：作息、爱好、城市、生日、校历、思念值变化规则等可在 `/console` 的生活设定页填。
  - 「思念值变化规则」是嵌套在 `jiwen.delta_hints` 的一项，留空时页面会提示它影响 bot 主动找你的频率；保存只改这一个子键，`jiwen` 下其他设置和注释原样保留。
  - 不想手填可以用起草脚本：`python3 scripts/draft_proactive_config.py <bot编号>` 会读这只 bot 的人设，调你在 `jiwen.delta_llm` 里配的模型地址起草「人设摘要」「什么情况下主动说话」「思念值变化规则」「睡觉时段」四项，写到 `<bot编号>-proactive-draft.yml`（0600）。**草稿只供你过目，不会自动写入**；确认后用 `--apply` 才写进配置，其他键不变、自动备份。要草拟 dsh 后端 bot 的睡觉时段用 `--backend dsh`。
  - 人设全文会发给该模型地址（单份超过 256 KiB 会直接拒绝），地址只允许 https 或本机回环 http，不跟随重定向。
- 语音：可填 TTS 音色编号；不填走默认音色。
- 压缩与清空后的补回：四条 SessionStart 钩子，管理台新建的 bot 自动带齐。
  - 压缩后补回群聊：对话上下文被压缩后，把所在群最近的聊天补回去；
  - 压缩后补回私聊：把最近的私聊原文补回去；
  - 一次性找回：布防后 72 小时内的下一次恢复会话，把更早压缩时丢掉的私聊补回，用一次即失效；没布防什么都不做；
  - 清空后回顾：/clear 后的新会话里放进清空时写下的对话摘要全文，每次清空只放一次。

  更早建的 bot 跑一次 `python3 scripts/install_compact_hook.py ~/.claude/channels/<bot编号>` 补齐四条（可重复跑，已装的不会再装、不会再备份；该 bot 目录下要已有 `.claude/settings.json`，内容是 `{}` 也行）；加 `--recall` 同时布防一次性找回。补装命令只打印 `hooks.SessionStart` 的变化，别的钩子显示为占位，settings 里的 env 等内容不会打到终端。
- 启停：总览页的启用/停用。停用后不再收发消息。

手动建（管理台不可用时）：

```bash
cp -r channels/_persona_template ~/.claude/channels/mybot
cp configs/_example.yml configs/mybot.yml
# 编辑 configs/mybot.yml：id / display_name / chat_id / birthday 等
# 编辑 ~/.claude/channels/mybot/.env：TELEGRAM_BOT_TOKEN=...
# 编辑 ~/.claude/channels/mybot/access.json：allowFrom 填你的数字 user_id
bash restart-bots.sh
```

### 5.1 bot 与你的全局 Claude 设置

bot 的 worker 默认只加载 bot 自己的项目设置与本地设置（`--setting-sources project,local`），你的用户级设置 `~/.claude/settings.json` 不再整份进 bot。

- 不再给 bot 用的：用户级钩子、插件、`~/.claude/skills/` 下的个人技能、全局 `CLAUDE.md`，以及 Claude Code 自带的通用技能（代码审查、定时任务、调试之类）。bot 只剩它自己目录 `.claude/skills/` 里的技能和生图技能。
- 个人技能与个人命令（`~/.claude/commands/`）会自动按名字关掉。个人子代理（`~/.claude/agents/`）在 bot 文件夹位于家目录下时仍会进 bot，这是 Claude Code 的已知限制，bot 文件夹放在家目录以外的不受影响。
- 依赖某个个人技能的，两条出路：把它放进 bot 目录的 `.claude/skills/`，或建下面的关闭标记。
- 生图技能照常可用：dispatcher 在 `~/.claude/worker-shared/` 下建一个指向 `$NOVELAI_SKILL_ROOT`（默认 `~/.claude/skills/novelai-skill`）的链接挂给 bot；没装生图技能就不建。
- 跟随全局的：bot 自己没配的供应商、模型、思考强度。全局 `settings.json` 的 `env` 段整段带进 worker 的进程环境（bot 自己 env 里已有的键不被覆盖）；`effortLevel` 与 `modelSettings`、`alwaysThinkingEnabled`、`switchModelsOnFlag`、顶层 `model` 在 bot 两份设置都没设时照搬。
- 全局的禁止规则 `permissions.deny` 与 bot 自己的合并后照样带给 bot；要约束 bot 的规则，请写进 bot 自己的 `.claude/settings.json`。worker 以跳过权限确认的模式运行，全局禁止规则在这种模式下是否生效尚未验证。
- /clear 时生成摘要的那次模型调用同样不加载用户级设置，模型与供应商仍跟随全局。Windows 上这次调用的临时目录位于用户目录之下，全局 `CLAUDE.md`、个人技能与个人命令仍可能进入摘要调用，这是已知限制。

关闭标记有两种，存在即生效（不读内容），下次拉起 worker 或下次 /clear 时生效，不用重启 dispatcher：

- 只关这只 bot：`touch ~/.claude/channels/<bot编号>/.worker-isolation-off`
- 关全部 bot（含之后新建的）：`touch ~/.claude/channels/.worker-isolation-off`，即所有 bot 目录的上一级目录下。
- Windows 上在 PowerShell 里建：只关这只用 `New-Item -ItemType File -Force "$env:USERPROFILE\.claude\channels\<bot编号>\.worker-isolation-off"`，关全部用 `New-Item -ItemType File -Force "$env:USERPROFILE\.claude\channels\.worker-isolation-off"`。

关闭后这只 bot 的 worker 与 /clear 摘要都回到旧方式（加载用户级设置）。以下情形自动走旧方式，不用手动关：用户级设置里有 `apiKeyHelper`、`awsAuthRefresh`、`awsCredentialExport`、`gcpAuthRefresh`、`proxyAuthHelper` 任一，且 bot 自己没设同名键；准备步骤失败，例如共享链接建不成、`.worker-settings.json` 写不成；每次启动自查发现隔离设置没被 Claude Code 认出、生图技能没挂上、或 worker 在初始化前异常退出，这一次对话照常跑完，下次启动退回旧方式，dispatcher 重启后再试。用了上述名单之外的自定义登录方式的，请建关闭标记。

说明与自检：

- dispatcher 日志里每次拉起有一行 `WORKER-ISOLATION`：`mode=on status=ok` 表示已隔离；`status=degraded reason=<原因>` 表示这次走了旧方式；`mode=off` 表示关闭标记生效。
- 想确认全局 `CLAUDE.md` 没进 bot：在全局 `CLAUDE.md` 里临时加一句只有你知道的话，下次 /clear 后问 bot 是否知道，确认后删掉。
- 已在进行的会话要到下次压缩或 /clear 后才完全生效，此前会话里还留着旧的技能清单与全局说明。
- 不支持 `CLAUDE_CONFIG_DIR`：dispatcher 与四条钩子都按 `~/.claude` 定位会话与设置。
- Windows 上共享链接用目录联接（junction），这一套尚未在 Windows 真机上实测；遇到 bot 画不了图或行为异常，先建全局关闭标记。另有一个已知限制：Windows 用户名含 `&` 时，bot 可能读不到图片附件。

---

## 6. 模型与供应商

一个 bot 的“说话模型”来自供应商绑定。两种用法：

### 6.1 Claude 官方订阅

终端里跑一次 `claude` 登录。管理台的 Claude 底层会走本机凭据；不需要把 key 写进任何仓库文件。

### 6.2 第三方 Anthropic 兼容中转

在 `/provider` 新建供应商：填名称、Base URL、API key、模型列表、默认模型。要求上游支持 **Anthropic Messages API**；只支持 OpenAI 格式的中转不能用。

- 供应商清单只由 `provider_config.py` 写：`configs/providers.json`（0600，含明文 key，已在 `.gitignore`）。
- 页面永远只回 `****abcd` 形式的掩码；不会回显完整 key。
- 绑定后按页面提示重启 bot 生效。
- `provider-proxy`（8770）是可选的本机代理实例，由同一份清单派生；不用可不注册：

```bash
python3 platform_services.py register provider-proxy --repo "$PWD"
```

- 该服务需要自行放置 `cli-proxy-api` 二进制（公开版不随附）：优先读 `CLAUDEBOT_CLI_PROXY_BIN`，否则找 `~/Library/Application Support/claudebotlife-provider-proxy/cli-proxy-api` 或 `~/.claude-tgbot/cli-proxy-api`。未安装时服务会明确报错退出，不影响其他功能。

- 每个 bot 的 `<channels>/<bot>/.claude/settings.json` 由 `provider_config` 写。**不要手改**；如果你在 `settings.local.json` 里写了 `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` / `ANTHROPIC_API_KEY`，管理台会报 `local_conflict` 并拒绝写入，需要你手工清掉这几个键再重试。
- 旧版配置迁移（一次性、默认 dry-run）：

```bash
python3 scripts/migrate_legacy_providers.py --dry-run
# 确认后再加 --apply
```

---

## 7. 双底层：Claude 与 dsh

管理台可以同时显示两套底层，顺序由 `CONSOLE_BACKENDS` 控制，默认 `claude,dsh`。

- `claude`：本机 Claude Code。装好 CLI 并登录即可用。
- `dsh`：**外部依赖，不在本仓库内**。需要你另行安装并运行它的本机适配器：
  - 地址：`CONSOLE_DSH_ADAPTER_URL`，默认 `http://127.0.0.1:17990`，只允许回环；
  - 令牌文件：`CONSOLE_ADAPTER_TOKEN_FILE`，默认 `~/.config/claude-dsh-console/adapter.token`，只读，程序不打印、不修改；
  - Windows 上给令牌文件收紧 ACL：
    ```powershell
    icacls "%USERPROFILE%\.config\claude-dsh-console\adapter.token" /inheritance:r /grant:r "%USERNAME%:R"
    ```
### 7.1 装 dsh（可选，三步）

1. **装 dsh**：clone `https://github.com/wsxwj123/dsh-bot`（公开仓），需要 bun 和 python3。仓库必须放在**家目录根下**（如 `~/dsh-bot`）——放桌面/文稿/下载/iCloud 下，macOS 隐私保护会拦住 launchd。
2. **生成令牌**：写一个随机串到 `~/.config/claude-dsh-console/adapter.token`，权限 0600。两边默认读同一个文件，只需生成一次。
   ```bash
   mkdir -p ~/.config/claude-dsh-console
   python3 -c 'import secrets; print(secrets.token_hex(32))' > ~/.config/claude-dsh-console/adapter.token
   chmod 600 ~/.config/claude-dsh-console/adapter.token
   ```
3. **起适配服务**：在 dsh 仓里运行 `python3 -m moments.adapter_service`，默认监听 `127.0.0.1:17990`，只允许回环。macOS 可用 dsh 仓自带的 `com.dsh-bot.console` launchd 任务常驻；Linux/Windows 需要自己用 tmux 或系统服务拉起。

最小验证：

```bash
curl -H "Authorization: Bearer $(cat ~/.config/claude-dsh-console/adapter.token)" \
  http://127.0.0.1:17990/adapter/v1/meta
```

应返回 `{"backend":"dsh","adapter":"v1",...}`。然后在启动 8767 时设置 `CONSOLE_DSH_ADAPTER_URL=http://127.0.0.1:17990`（只认回环，写局域网 IP 或主机名会被拒）。

### 7.2 必须知道的限制

- dsh 只提供本机 `/adapter/v1` 接口，不提供公网服务；管理台和 dsh 必须在同一台机器上。
- 开机自启只有 macOS（launchd）；Linux/Windows 要用户自己拉起适配服务。
- dsh 侧 bot 运行还需要它自己的供应商清单与每只 bot 的配置（`~/.dsh-bot/` 下），先按 dsh 仓文档配好，公开版的“新建 bot”才能建出可用的 dsh bot。
- dsh 的 oauth 账号接口在没配 cliproxy 时会返回 `warnings: ["cliproxy_unconfigured"]`，功能可用，忽略这条 warning 即可。
- 公开版暂不接入 dsh 的技能全文接口（`/adapter/v1/bots/{id}/skills/{name}`）；管理台不展示技能页。

- dsh 没装或没启动时，总览里该侧显示 `未安装 / 未配置 / 连接失败`，Claude 侧不受影响；相关写接口返回 502/503，不会静默成功。
- 切换底层：`/console` 的 bot 页 → 切换。两边都必须启用，目标边必须开启群聊；切换是异步任务，页面会显示进度和失败步骤。失败不会假装回滚，按页面提示处理。

---

## 8. 功能开关（八项）

每个 bot 的开关存在 `<channels>/<bot>/features.json`，管理台的 `/console` 功能开关页是唯一写入口。

| 键 | 页面标签 | 说明 |
|---|---|---|
| `voice` | 发语音 | 需要语音桥/TTS 配置；会放上 voice 技能 |
| `image` | 生图 | 需要画风页的 NovelAI 令牌或本机 ComfyUI |
| `stickers` | 表情包 | 会放上 gif 技能 |
| `moments` | 朋友圈 | 发圈、看圈、回评论 |
| `proactive` | 主动找你聊 | 按生活素材和随机间隔主动私聊 |
| `life` | 生活作息 | 作息、爱好、情境；关掉后主动消息会缺素材 |
| `relationship` | 关系进展 | 情绪/关系数值影响语气和主动行为 |
| `groups` | 群聊 | 群内发言、群聊发现、群成员管理 |

- 文件不存在 = 八项全开；文件读不出 = 八项全关并给出 warning。
- 改开关会让程序同步对应技能文件；部分改动需要重启 bot 才完全生效。
- 缺前提时页面会提示缺什么（缺语音编号、缺 NovelAI 令牌、缺朋友圈库、缺私聊编号等），不会只报一个笼统失败。

---

## 9. 朋友圈、生图与画风（可选）

### 9.1 朋友圈

- 页面：`/moments`。
- 内容存在 SQLite；首次打开朋友圈页会建库。
- 关掉 `moments` 后仍然可以正常私聊；关掉 `life` 后很少自己发圈，但回评论、看你的朋友圈照常。

### 9.2 画风

`/styles` 是可选页。没有配置令牌时，生图功能不可用，文字聊天不受影响。

NovelAI 令牌只通过脚本写入，不从网页接收、不打印：

```bash
printf '%s\n' "$NOVELAI_TOKEN" | python3 scripts/set_novelai_key.py
python3 scripts/set_novelai_key.py --check   # 只输出 set: true|false
```

落点：`$NOVELAI_SKILL_ROOT/.env.local`（默认 `~/.claude/skills/novelai-skill/.env.local`），权限 0600，已在 `.gitignore`。不要把令牌贴进 README、issue、日志或 `configs/`。

bot 的 worker 不加载用户级设置，生图技能经共享目录 `~/.claude/worker-shared/` 挂给 bot（见 5.1），照常可用。

### 9.3 ComfyUI（可选）

在 `configs/_global.yml` 的 `moments.image_generation` 里把 `provider` 改成 `comfyui`，填本机 `url`、workflow 路径和输出目录。默认仍可用 NovelAI；两者都只是生图后端，不影响聊天。

---

## 10. 聚会模式与摄像视角

- **聚会模式（party）**：按群开启，群级共享，最长 6 小时；可带 `exclude`（不参与的 bot）和 `note`（场景说明）。入口有三处：bot 工具、群聊导演、管理台群聊页。关闭词包括“聚会结束”“结束聚会”“散了”等。
- **摄像视角（photo）**：按 bot + 会话开启。消息里出现“拍照 / 摄像 / 摄影”“每轮拍照”“摄像机视角拍照”等会开启；每次图片投递算一轮；最长 24 小时。退出词包括“停 / 停下 / 别拍了 / 正常拍照”等。
- 两者都是本地状态文件，不写数据库：`.party-mode/`、`.photo-mode/`。损坏、时钟回拨或过期按关闭处理。

---

## 11. NSFW 说明

本仓库**没有 NSFW 开关，也没有成人人设模板**。

- 角色是否涉及成人内容、边界在哪，由你自己写的人设决定；
- 不提供现成成人模板，也不在代码里按测试输入或环境识别“特殊模式”；
- 请自行确认内容合法、符合 Telegram 与模型供应商的使用条款。由此产生的账号或合规风险由使用者承担。

---

## 12. 常用命令

```bash
bash restart-bots.sh                  # 重启全部 bot（读 configs/*.yml）
bash stop-bots.sh                     # 停止全部 bot
bash scripts/watch-bot.sh mybot       # 跟踪某个 bot 的实时对话日志
python3 -m bots_registry --format=sh  # 查看注册表：id / 端口 / namespace
python3 -m bot_features --show mybot  # 查看八项开关
python3 scripts/install_compact_hook.py ~/.claude/channels/mybot  # 给更早建的 bot 补齐四条压缩与清空补回钩子（可重复跑）
python3 scripts/install_compact_hook.py ~/.claude/channels/mybot --recall  # 同上，并布防一次性找回（下一次恢复会话补回更早丢掉的私聊）
python3 platform_services.py status provider-web --repo "$PWD"
python3 platform_services.py unregister autostart
```

---

## 13. 常见错误

| 现象 | 原因 / 处理 |
|---|---|
| 打开 8767 连接被拒 | 管理台没启动。跑 `MOMENTS_WEB_MODE=provider python3 -m moments.web`，或注册并启动 `provider-web` |
| 登录页提示“面板未配置口令” | 先跑 `python3 -m moments.panel_auth --set-password` |
| 登录一直 401 / 429 | 口令错，或连续失败触发限速；等几分钟再试 |
| bot 不回消息 | 检查 token 是否写进 `<channels>/<bot>/.env`、`allowFrom` 是否有你的数字 user_id、`bash restart-bots.sh` 是否成功、日志有没有 409（同一 token 被两个进程长轮询） |
| `local_conflict` | `<channels>/<bot>/.claude/settings.local.json` 里有 provider 键；手工删掉 `ANTHROPIC_*` 后重试 |
| dsh 页面 502 / 503 | dsh 没启动、地址不是回环、或 `adapter.token` 不存在/无权限 |
| 操作返回 409 busy | 同一 bot 有生命周期任务在跑；等任务结束后重试 |
| 8767 / 8770 / 17801 起端口占用 | 先停旧进程；`platform_services.py status` 查服务状态 |
| Windows 提示禁止运行脚本 | 命令前加 `-ExecutionPolicy Bypass` |
| 路径含中文/空格导致 worker 起不来 | 把仓库移到纯英文、无空格路径；Windows 下不要用 `~` |
| 生图报缺令牌 | 跑 `scripts/set_novelai_key.py`，或改用 ComfyUI |
| 新 bot 加完没起来 | `restart-bots.sh` 读的是 `configs/*.yml`；确认 `enabled` 不是 `false`，并重新跑一次 |

---

## 14. 安全说明

- 8767 默认只监听 `127.0.0.1`，非回环地址直接拒绝启动。
- 口令只存哈希；会话 cookie 名 `cb_provider`，可在页面退出或 `POST /api/panel/logout_all` 全部撤销。
- 密钥文件：`<channels>/<bot>/.env`、`configs/providers.json`、`~/.claude/secrets/moments-panel*.env`、`~/.claude/skills/novelai-skill/.env.local`，均按 0600 写入，且已在 `.gitignore`。
- worker 隔离写在 bot 目录下的 `.worker-settings.json`（0600）只含隔离设置、透传的模型与思考设置和禁止规则，不含 env 与任何密钥；全局 `settings.json` 的 env 段（可能含供应商令牌）只进 worker 与摘要子进程的环境，不进日志、命令行和文件。
- dsh 令牌只读，只进 `Authorization` 头；不进日志、异常文本或页面。
- 日志与页面响应会对 token / key 做打码；但**不要主动把日志原文发到公开渠道**。
- 不要把 8767 直接挂公网。必须远程访问时，用 SSH 隧道或带鉴权的反向代理，并确认 dsh 转发路径不会绕过登录门。
- `.api-token` 是 dispatcher 启动时生成的 32 字节随机口令，只经 `X-Api-Token` / Bearer 使用，不提交。
- `reply` 的 `files` 有出口白名单：只允许生图输出目录、表情库、`<channels>/<bot>/inbox`、`<channels>/<bot>/media` 和 `MOMENTS_MEDIA_ROOT` 内的真实文件；`..`、软链、`.env`、`configs/`、`secrets/`、`providers.json` 一律丢弃（日志只记 `file_dropped` 原因码，不记路径）。
- voice-bridge 默认 fail-closed：没有令牌时除 `/health` 外全部 401。`install.sh` 会在 `~/.claude/secrets/voice-bridge.token` 生成共享令牌（0600），dispatcher、voicecall、moments 从 `VOICE_BRIDGE_TOKEN_FILE` 读取并带 Bearer；只有显式 `VOICE_BRIDGE_ALLOW_INSECURE=1` 才允许无鉴权。
- **群聊默认不放行陌生人（重要）**：群策略 `allowFrom` 为空时回退到私聊 owner 名单，owner 名单也为空则拒绝。群成员的发言会驱动一个拥有本机工具权限的 agent；**只把 bot 拉进你信任的群**。
- 本程序没有“测试环境识别”或按特定输入写特例的逻辑；验收测试使用临时 HOME 和假服务。

---

## 15. 停用、卸载与计划任务

**停用 bot 不等于卸载计划任务。** 在管理台把 bot 设为 `enabled: false` 后，它的 dispatcher 不再收发消息，但已经注册的自启/主动消息任务仍会按计划触发，只是启动时发现 bot 已停用而跳过。要彻底不再触发，必须显式卸载任务。

```bash
bash stop-bots.sh

python3 platform_services.py unregister autostart
python3 platform_services.py unregister provider-web
python3 platform_services.py unregister provider-proxy
python3 platform_services.py unregister jiwen-tick
python3 platform_services.py unregister memory-compactor
# 每只 bot 的主动消息任务：
python3 platform_services.py unregister self-initiate-mybot
```

任务名在各平台分别是：

- macOS：`com.example.claudebotlife-self-initiate-<bot>`。也可以手动卸载：
  ```bash
  launchctl bootout "gui/$(id -u)/com.example.claudebotlife-self-initiate-<bot>"
  ```
- Windows：`claude-tgbot-self-initiate-<bot>`。也可以手动卸载：
  ```powershell
  Unregister-ScheduledTask -TaskName "claude-tgbot-self-initiate-<bot>" -Confirm:$false
  ```
- Linux：`claudebotlife-self-initiate-<bot>.service`，用 `systemctl --user disable --now` 卸载。

Windows 全量卸载：

```powershell
powershell -ExecutionPolicy Bypass -File windows\unregister-tasks.ps1
```

然后按需删除（**会丢数据，先备份**）：

- 仓库目录（只放代码，运行数据默认在 `~/.claude-tgbot/state.db`）；
- `~/.claude/channels/<bot>/`（对话记忆、人设、关系状态）；
- `configs/providers.json`、`~/.claude/secrets/moments-panel*`、`~/.claude-tgbot/`（含状态库 `state.db`）；
- 旧服务若仍存在，用 `platform_services.py status` 逐个确认后卸载。

---

## 16. 平台验证程度

| 平台 | 验证程度 | 说明 |
|---|---|---|
| macOS | 实跑验证 | 安装、8767、新建 bot、供应商、功能开关、party/photo、双门禁测试 |
| Linux | 静态检查 | 脚本与路径按 POSIX 检查；未在真实发行版上完整跑通 |
| Windows | VM 先验 + 真机复验清单 | 已做路径/编码/计划任务三项先验；管理台全流程、语音、聚会/摄像、长稳运行仍需真机复验 |

未验证项会如实写在这里，不把“能启动”说成“全功能已验证”。

---

## 17. 目录速览

```text
adapters/        两套底层适配器（claude / dsh）
channels/        空人设模板（_persona_template）
configs/         示例配置；真实 _global.yml 与 providers.json 不进仓
dispatcher/      Telegram 调度器（TypeScript / Bun）
jiwen/           情绪与关系数值
moments/         8767 管理台
scripts/         安装辅助、key 写入、脱敏扫描、门禁
tests/           单元与验收测试（夹具均为假值）
windows/         Windows 启动/注册/卸载脚本
voicecall/       可选电话模块
voice-bridge/    可选语音桥
```

---

## 18. License

MIT，见 [LICENSE](LICENSE)。Copyright (c) 2026 euphoriaaaaaa1。
