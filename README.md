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

- [Claude Code](https://claude.com/claude-code) CLI（worker 用；已登录）
- [Bun](https://bun.sh)（跑 dispatcher.ts）
- `tmux`、Python 3.10+、`pip install -r requirements.txt`
- 一个 DeepSeek API key（情绪/关系数值裁判用）
- Mac 或 WSL/Linux 均可

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
