# voice-bridge · bot 的「耳朵和嘴」（可选模块）

一个跑在本机的语音小服务，给 claude-tgbot 补上：

- **耳朵（STT，语音转文字）**：本地 SenseVoice 模型。Telegram 里用户发来语音消息，bot 能听懂内容再回复。
- **嘴（TTS，文字转语音）**：走 Fish Audio 云服务。bot 可以回语音，网页打电话模块（`voicecall/`）靠它出声。

**这是可选模块。** 不装它，主 bot 照常跑，一行代码都不受影响——只是收不了语音、发不出声。

> 谁适合装：已经跑通主 bot、想加「语音聊天 / 打电话」玩法的人。只想要文字聊天，跳过本模块。

默认地址 `127.0.0.1:7788`，暴露这些端点（主项目和 voicecall 都在用）：

| 端点 | 干什么 | 谁在调 |
|------|--------|--------|
| `GET /health` | 健康检查 | 你自己 / 运维 |
| `POST /transcribe_telegram` | 下载 Telegram 语音并转文字（STT） | 主项目 dispatcher |
| `POST /transcribe_file` | 转写本地音频文件（STT） | voicecall / MCP |
| `POST /synthesize_voice` | 文字合成 OGG/Opus 语音（TTS） | voicecall |
| `POST /send_voice` | TTS 并直接发到 Telegram（绕代理的 multipart bug） | 主项目 dispatcher |
| `POST /send_file` | 发图片/文件到 Telegram | 备用 |

---

## 一、依赖

| 依赖 | 说明 |
|------|------|
| **Python 3.10+** | 需要 venv |
| **ffmpeg** | TTS 要把 Fish 返回的 mp3 转成 Telegram/浏览器要的 ogg/opus。`ffmpeg -version` 能出版本号即可；Mac `brew install ffmpeg` |
| **Python 包** | `pip install -r requirements.txt`。**提醒：会拉 torch，下载量大**（macOS 几百 MB；Linux 默认拉 CUDA 版 2 GB+，只想 CPU 用文件里注明的 index-url 装） |
| **Fish Audio API Key** | TTS 必需（见第四节）。STT 完全本地免费，不配 key 也能用 |
| **模型文件** | **本仓库不带模型**（约 1 GB），首次启动自动从 ModelScope 下载（见第三节） |

装：

```bash
cd voice-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> `start.sh` 约定用 `.venv/bin/python` 启动。不想放 `.venv`，就手动跑 `.venv/bin/python server_http.py`（换成你的解释器路径）或直接改 `start.sh`。

---

## 二、起 / 停 / 查

```bash
./start.sh start      # 启动（后台常驻，端口 7788）
./start.sh status     # 看状态 + 顺手 curl 一次 /health
./start.sh stop       # 停
./start.sh restart    # 重启
```

- 日志在 `logs/http_server.log`（5MB 滚动 3 份）；启动输出在 `logs/http_server.out`。
- `FISH_AUDIO_API_KEY` / `TELEGRAM_BOT_TOKEN` 从**当前 shell 环境**继承；没设时会再去 `~/.zshrc` 里找 `export` 行。也可以先 `export FISH_AUDIO_API_KEY=...` 再 `./start.sh start`。
- 网络需要代理才能访问 Fish Audio / Telegram 的话，先在 shell 里 `export HTTPS_PROXY=http://...` 再启动（脚本只透传，不设默认值）。
- Windows 原生跑 `./start.sh` 不方便，可直接 `python server_http.py`（或走 WSL）。

**验证服务活着**：

```bash
curl -s http://127.0.0.1:7788/health
# 预期：{"ok":true,"model_loaded":true}
```

首次启动时 `model_loaded` 会先是 `false`（模型在后台预热，约 10-30 秒），等预热完成后变 `true`。

---

## 三、STT：SenseVoice 模型怎么来

用的是阿里开源的 **SenseVoice-Small**（模型名 `iic/SenseVoiceSmall`），经 `funasr` 加载、从 **ModelScope** 下载。

- **首次启动自动下载**，落盘到本目录下的 `models/`（实际路径 `models/models/iic/SenseVoiceSmall/`），约 1 GB，取决于网速要等一会儿。
- 下载走 ModelScope 官方源。国内网络通常直连即可；如果卡住，检查网络/代理。
- 想换缓存位置，改 `server_http.py` 顶部的 `MODEL_CACHE`（默认模块目录下 `models/`）。
- 模型加载是懒加载 + 启动预热：服务起来后第一次识别可能多等几秒，之后常驻内存（约 2 GB）。
- **本仓库不含模型文件**，`.venv/`、`models/`、`logs/` 也都不进 git（已在根 `.gitignore`）。

**手动验证一次 STT**（用仓库自带的示例音频，本地识别、免费、随便测）：

```bash
curl -s -X POST http://127.0.0.1:7788/transcribe_file \
  -H 'Content-Type: application/json' \
  -d '{"path":"'"$PWD"'/examples/asr_example_zh.wav"}'
```

预期返回（`text` 就是示例音频说的一句话，`emotion`/`events`/`language` 是顺带的分析）：

```json
{"text":"欢迎大家来体验达摩院推出的语音识别模型。","emotion":"NEUTRAL","events":["Speech"],"language":"zh","duration_sec":5.55,"latency_ms":989}
```

（`duration_sec` / `latency_ms` 每次不同，正常。）

---

## 四、TTS：Fish Audio 云服务

TTS 不本地跑模型，直接调 [Fish Audio](https://fish.audio) 的云 API，需要两样东西：

| 需要什么 | 怎么来 |
|---|---|
| **`FISH_AUDIO_API_KEY`** | fish.audio 注册账号 → 控制台生成 API Key。**不配 key：STT 照常用，TTS 调用会返回明确的 `FISH_AUDIO_API_KEY 未设置` 错误**（不会崩） |
| **音色 ID（voice_id）** | 在 fish.audio 上选一个现成音色，或上传参考音频创建自己的音色，复制它的 ID（API 里叫 `reference_id`）。留空 → 用 Fish 的默认音色 |

- `FISH_AUDIO_MODEL`（可选）：默认 `s2.1-pro`（付费档，音质与免费档相同）。想先用免费档可以设 `FISH_AUDIO_MODEL=s2.1-pro-free`；该模型失效时会自动降级到 `s2.1-pro`。**注意 TTS 是按字符计费的付费服务**。
- `FISH_AUDIO_CONCURRENCY`（可选）：并发上限，默认 4。

验证 TTS（会真实调用 Fish，产生费用；纯验证服务在不在，其实第 2 节的 `/health` 就够了）：

```bash
curl -s -X POST http://127.0.0.1:7788/synthesize_voice \
  -H 'Content-Type: application/json' \
  -d '{"text":"测试","voice_id":"","emotion":"NEUTRAL"}' --output /tmp/test.ogg && ls -l /tmp/test.ogg
```

---

## 五、接到主项目（三种用法，按需选）

### 1. Telegram 语音消息 → 文字（STT）

**装完就自动生效**：主项目 dispatcher 收到 voice 消息时，内置会调 `127.0.0.1:7788/transcribe_telegram` 转写，把识别文字当消息正文交给 bot。服务没起时它会静默回落成 `(voice message)` 占位符。

### 2. bot 回语音（TTS）

bot 的 `reply` 工具带 `as_voice` 参数，dispatcher 会调 `/send_voice` 发语音。需要在 bot 的 **`access.json`** 里加一项（即「音色 ID」）：

```json
{
  "voiceId": "你的 Fish Audio 音色 ID（reference_id）"
}
```

没配 `voiceId` 时：bot 就算想发语音也会降级成纯文字（内容不丢），日志里会写明。

### 3. 网页打电话（voicecall 模块）

`voicecall/README.md` 第 4 步说的「voice-bridge 服务」就是本模块：用 `/transcribe_file`（听你说）和 `/synthesize_voice`（替 bot 说）。地址不同就改 `voicecall/.env` 的 `VOICE_BRIDGE_URL`。

### 4. MCP 工具（可选）

`server.py` 是个 MCP stdio 代理，把 STT 暴露成 Claude 工具（`transcribe_audio` / `transcribe_telegram_voice`）。要用的 bot 在其 `.mcp.json` 里加上：

```json
{
  "mcpServers": {
    "voice-bridge": {
      "command": "/你的路径/voice-bridge/.venv/bin/python",
      "args": ["/你的路径/voice-bridge/server.py"]
    }
  }
}
```

### 5. 让 bot 知道「什么时候该发语音」

把 `persona-snippet.md` 的内容并入你的 bot 人设（`~/.claude/channels/<bot>/CLAUDE.md`）。它教 bot 什么该用语音回、什么该文字、情绪标签怎么用。

---

## 六、开机自启（可选）

- **macOS**：`launchd/com.example.voice-bridge.plist`，把里面的 `/ABSOLUTE/PATH/TO/voice-bridge` 换成真实路径后照文件头部注释安装。
- **Linux**（systemd user service，示例）：

```ini
# ~/.config/systemd/user/voice-bridge.service
[Unit]
Description=voice-bridge (STT/TTS for claude-tgbot)

[Service]
WorkingDirectory=%h/claudebotlife/voice-bridge
Environment=FISH_AUDIO_API_KEY=你的key
ExecStart=%h/claudebotlife/voice-bridge/.venv/bin/python %h/claudebotlife/voice-bridge/server_http.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now voice-bridge
```

（用了自启就不要再用 `./start.sh start`，两条路会互抢 PID。）

---

## 七、常见问题

| 症状 | 解法 |
|------|------|
| `/health` 通但 `model_loaded=false` 很久 | 模型正在下载（首次约 1 GB）。看 `logs/http_server.log` 里的下载/加载进度 |
| STT 报 `ffmpeg` 相关错误 / TTS 转码失败 | ffmpeg 没装或不在 PATH。`ffmpeg -version` 确认 |
| TTS 返回 `FISH_AUDIO_API_KEY 未设置` | 按第四节配 key，重启服务 |
| TTS 返回 401/402/429 | key 无效 / 余额不足 / 并发超限。Fish 控制台核实 |
| 外网请求全超时 | 网络需要代理却没设。`export HTTPS_PROXY=...` 后重启 |
| 端口被占 | 改 `server_http.py` 末尾的 `port=7788` 和 `start.sh` 的 `PORT`；同时把主项目里引用 7788 的地方（dispatcher、voicecall 的 `VOICE_BRIDGE_URL`）对齐 |
| 识别没反应 / 音频太短 | 小于 1.5 秒的音频会返回 `emotion=UNKNOWN`，属正常设计 |

---

## 八、安全边界（改之前先读）

- **只绑本机**：服务固定监听 `127.0.0.1:7788`，局域网/外网访问不到。
- **端点没有鉴权**：这是刻意的——它只服务本机的 dispatcher / bot / voicecall，用"只绑回环"当边界，和你机器上其它本地开发服务一样。
- ⚠️ **不要把监听地址改成 `0.0.0.0`**：那会把"能合成语音、能读你 Telegram 文件"的接口暴露给同网段任何人，而它没有任何口令保护。要远程用请走 SSH 隧道或 Tailscale，不要改绑定地址。
- 日志里的 token 已用 `mask()` 打码（只留头尾），但 `/transcribe_telegram` 的请求体里会带 `bot_token`——**别把这个端口反代到公网**。
