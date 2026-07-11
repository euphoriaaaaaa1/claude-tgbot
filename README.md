# claude-tgbot · 数字生命 Telegram bot 引擎

用 Claude Code 当"大脑"驱动的 Telegram 角色扮演 bot 引擎。每个 bot 是一个持续运行的 Claude worker，有人设、记忆、情绪、随时段变化的精力/好感/信任/淫欲等关系数值，会像真人一样有分寸地回应——不随叫随到、也不无脑拒绝。

内附一个示例人设 **陈露露**（35 岁小学语文老师，慢热高冷，陌生人起步）演示整套机制。

> ⚠️ 成人向角色扮演项目，仅供授权的个人娱乐用途。请遵守当地法律与 Telegram/模型服务条款。

## 架构一图流

```
Telegram ──► dispatcher.ts (每 bot 一个, grammY 长轮询)
                │  gate + 路由，写 inbox 文件
                ▼
          spawn-worker.sh ──► tmux 里的 claude worker（读人设+记忆+关系数值，生成回复）
                                   │ reply 工具
                                   └──► dispatcher /send ──► Telegram

后台幕后（都用 DeepSeek，独立于 worker 的主模型）：
  · director.py     群聊导演：决定群里这一轮谁开口（可选，多 bot 才需要）
  · jiwen/tick.py   每 5 分钟：读对话，给情绪 + 关系数值打分、随时段漂移精力
```

两个"大脑"分工：
- **worker**（说话的那个）= `claude` CLI，走你的 `settings.json`（OAuth 或 API）。切主模型只影响它。
- **DeepSeek 裁判/导演** = 独立 HTTP，自带 key（配在 `_global.yml`），不读 `settings.json`。

## 依赖

- [Claude Code](https://claude.com/claude-code) CLI（worker 用；provider 配置见下方「切换模型来源」）
- [Bun](https://bun.sh)（跑 dispatcher.ts）
- `tmux`、Python 3.10+、`pip install -r requirements.txt`
- 一个 DeepSeek API key（情绪/关系数值裁判用；与上面的 Claude provider 相互独立）

### 平台

- **macOS**：完整支持。dispatcher+worker 核心 + 全套定时/常驻服务（导演、jiwen、主动开口、朋友圈）都靠 **launchd**（`plist-templates/`）。
- **Linux / WSL**：dispatcher+worker **核心能跑**（bun+tmux+claude CLI），但 `plist-templates/` 是 macOS launchd 专属——那套「数字生命」自动层需自行改用 `cron` / `systemd` / `tmux` 常驻。
- **Windows**：不能原生跑，须经 **WSL**（同上 Linux 说明）。

## 快速上手（示例 bot 陈露露）

> 约定：把仓库克隆到 `~/claudebotlife`（朋友圈等可选脚本默认从此路径找 `scripts/`）。放别处需相应改 `moments/` 里的路径。

```bash
# 0) 克隆到约定路径
git clone <this-repo> ~/claudebotlife && cd ~/claudebotlife

# 1) 装依赖
pip install -r requirements.txt
bun --version   # 确认有 bun

# 2) 全局配置：填 DeepSeek key
cp configs/_global.example.yml configs/_global.yml
$EDITOR configs/_global.yml        # 把 jiwen.delta_llm.api_key 换成你的

# 3) 把示例 bot 的 channel 放到 Claude 的 channels 目录
mkdir -p ~/.claude/channels
cp -r channels/chenlulu ~/.claude/channels/

# 4) 建 Telegram bot（找 @BotFather），拿 token
cd ~/.claude/channels/chenlulu
cp .env.example .env
$EDITOR .env                       # 填 TELEGRAM_BOT_TOKEN
$EDITOR access.json                # 把 allowFrom 的 YOUR_TELEGRAM_USER_ID 换成你的 user_id

# 5) 启动
cp restart-bots.example.sh restart-bots.sh
bash restart-bots.sh               # 起 dispatcher；给 bot 发消息即可

# 6)（可选）情绪/关系数值引擎，每 5 分钟 tick 一次
python3 jiwen/tick.py              # 或挂到 launchd/cron 定时
```

## 切换模型来源（Provider）

系统有**两处相互独立的模型来源**，分开配、别搞混：

| 用途 | 谁 | 配在哪 | 说明 |
|------|-----|--------|------|
| 角色说话（worker） | `claude` CLI | `~/.claude/settings.json` 的 `env` | 换它 = 换角色回复用的模型/中转 |
| 情绪裁判 + 群导演 | DeepSeek（独立 HTTP） | `configs/_global.yml` 的 `jiwen.delta_llm` | 和 worker provider **无关**，永远单独配一个 DeepSeek key |

**worker 用哪个 provider，只取决于 `~/.claude/settings.json`。** `spawn-worker.sh` 每次起 worker 都会先清掉继承的旧 env、再现读 settings.json 的 `env` 注入。三种配法任选其一：

**A. 官方订阅（最省事）** — `claude` 登录一次 Claude 账号即可；settings.json 的 `env` **不要**写 `ANTHROPIC_BASE_URL`。worker 会自动读系统钥匙串里的 OAuth 凭证并自动续期。

**B. 第三方中转 / API key（手动）** — 编辑 `~/.claude/settings.json`：
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://你的中转站/api",
    "ANTHROPIC_AUTH_TOKEN": "sk-你的中转key",
    "ANTHROPIC_MODEL": "claude-sonnet-4-5"
  }
}
```

**C. cc-switch（GUI，推荐给不想手改的）** — 装 [cc-switch](https://github.com/farion1231/cc-switch)，加一个 provider 配置、点切换，它会把上面那段 `env` 写进 settings.json。
> ⚠️ 别同时用多个「写 settings.json」的工具（cc-switch / ccp / 手改都写同一个 `env` 字段，会互相覆盖）——选一个。

### 🔴 切完必须重启 worker 才生效

worker 是常驻进程，只在**启动时**读 settings.json。切完 provider 要杀掉旧 worker，让它用新配置重生（记忆不丢，会自动 resume）：
```bash
export TMUX_TMPDIR=/tmp
tmux ls | grep -oE 'tg-[a-z0-9]+-worker' | xargs -I{} tmux kill-session -t {}
# 下一条消息会用新 provider 重新拉起 worker
```

### 切完就能用了吗？还要满足 3 条

1. **DeepSeek key 已在 `configs/_global.yml` 配好**——切 worker provider 不影响它；不配好，导演/情绪/主动消息都不工作。
2. **已按上面重启 worker**。
3. **中转站支持 Claude 的 Messages API 格式**（多数 claude 中转支持；纯 OpenAI 格式的中转不行）。

## 关系数值怎么工作（治"随叫随到"）

每个 bot 一份 `channels/<bot>/relationship.json`，四维 0–100：**好感 / 信任 / 淫欲 / 精力**。
- **信任**决定关系阶段（陌生→客气→多聊→暧昧→亲密），门槛没到就不会跳级亲密。
- **淫欲**没起来时 bot 不主动露骨；被撩/夜里有随机阵发尖峰。
- **精力**随昼夜+当日活动+随机波动漂移：晚上托高（NSFW 主场），只有后半夜 3–6 点真低谷；低了会**温柔婉拒并给补偿**，不是冷脸拒。
- 数值由 `jiwen/tick.py` 的 DeepSeek 裁判读对话涨跌。四个 bot 各算各的，不会时刻相同。

陈露露出厂是陌生人种子（好感 5 / 信任 5 / 淫欲 0），需要慢慢攻。

## 加更多 bot

1. `cp configs/_example.yml configs/<bot>.yml` 改人设摘要。
2. `cp -r channels/chenlulu ~/.claude/channels/<bot>`，改 `CLAUDE.md`（人设）、`access.json`、`.env`，删掉 `relationship.json`（或按新关系改初值）。
3. **三处 UUID 命名空间必须同步加一行且完全一致**（否则丢记忆）：
   `dispatcher/dispatcher.ts` 的 `BOT_NAMESPACES`、`dispatcher/spawn-worker.sh` 的 `case`、`chat_history.py` 的 `_BOT_NAMESPACES`。
4. `restart-bots.sh` 的 `BOTS` 加 `"<bot>:<新端口>"`。
5. 多 bot 群聊由 `director.py` 调度（可选，单 bot 不需要）。

## 目录

```
dispatcher/     dispatcher.ts / worker-plugin.ts / spawn-worker.sh —— Telegram 收发 + 拉起 worker
jiwen/          情绪引擎 + DeepSeek 关系数值裁判 + 5 分钟 tick
relationship.py 四维关系数值（昼夜精力/淫欲阵发/门槛提示）
director.py     多 bot 群聊导演（可选）
moments/        朋友圈网页（Flask，可选）
generators/     情境/心情/世界事件生成器
configs/        _global.yml + 每 bot 一个 yml（示例见 _example.yml）
channels/chenlulu/  示例人设（CLAUDE.md 人设 + access.json + 出厂关系种子）
scripts/        朋友圈/生图/锚定等运维脚本
```

## 安全

`.env`、`configs/_global.yml`（含 DeepSeek key）、运行时 inbox/记忆/transcript 都在 `.gitignore` 里，不会入库。fork 后请自查勿把 key/token 提交上去。
