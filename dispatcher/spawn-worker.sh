#!/bin/bash
# spawn-worker.sh <bot> <chat_id> <session_uuid> <dispatcher_url>
# Idempotent: if tmux session tg-<bot>-<chat_id> exists, no-op.
set -euo pipefail
# Defensive: inherit from dispatcher env; fallback /tmp if absent.
: "${TMUX_TMPDIR:=/tmp}"
export TMUX_TMPDIR

# 保证能找到 claude CLI：launchd 重启的 dispatcher(经 kill-idle-workers.sh 写死的精简
# PATH)不含 ~/.local/bin → 裸命令 `claude` 解析失败 → worker 启动即退、不回复。
# 显式前置 claude 所在目录,不依赖 dispatcher 继承的 PATH。
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

bot="${1:?usage: spawn-worker.sh <bot> <chat_id> <uuid> <dispatcher_url>}"
chat="${2:?chat_id required}"
uuid="${3:?session uuid required}"
dispatcher_url="${4:?dispatcher url required}"

# ─── unified session：uuid 恒为 uuid5(namespace[bot], "unified")，与 dispatcher.ts 一致 ──
# 覆盖调用方传入的 uuid（历史调用方 self-initiate / moments 可能仍传 per-chat uuid）。
# 这样无论哪个组件先 spawn，同一个 bot 的会话都收敛到同一个 unified session-id。
# **必须与 dispatcher.ts 的 BOT_NAMESPACES、chat_history.py 完全一致**，否则丢记忆。
case "$bot" in
  chenlulu) ns="550e8400-e29b-41d4-a716-446655440001" ;;
  *) ns="" ;;
esac
if [ -n "$ns" ]; then
  unified_uuid=$(/usr/bin/python3 -c "import uuid,sys; print(uuid.uuid5(uuid.UUID(sys.argv[1]), 'unified'))" "$ns" 2>/dev/null || echo "")
  [ -n "$unified_uuid" ] && uuid="$unified_uuid"
fi

session="tg-${bot}-worker"
if tmux has-session -t "$session" 2>/dev/null; then
  exit 0
fi

bot_dir="$HOME/.claude/channels/$bot"
chat_dir="$bot_dir/chats/$chat"
# unified inbox：dispatcher/director/server 都写 <bot_dir>/inbox；worker 监听它。
mkdir -p "$bot_dir/inbox"
# 兼容：遗留写入方(self-initiate/moments/scripts)仍写 per-chat inbox，worker 也监听。
mkdir -p "$chat_dir/inbox"

# Workers cwd at $bot_dir (NOT $chat_dir) so all chats of this bot share the
# same project slug → share the same ~/.claude/projects/<slug>/memory/ dir.
# Claude's native auto-memory then auto-syncs cross-chat facts (names,
# preferences, etc.) without custom plumbing.
# Chat isolation comes from --session-id alone (different uuid per chat_id).

MCP_CONFIG="$HOME/.claude/dispatcher/worker-mcp.json"

# If a jsonl for this session-id already exists (from migrate-session.py or a
# prior worker run), use --resume; otherwise --session-id for a fresh start.
# Slug now reflects bot-level cwd (all chats share one project dir for memory).
proj_slug=$(echo "$bot_dir" | sed 's#[/.]#-#g')
existing_jsonl="$HOME/.claude/projects/${proj_slug}/${uuid}.jsonl"

# /clear 残桩防护: 用户在 Telegram 发 /clear 会把会话清成只剩元数据(last-prompt/
# mode/permission-mode), 没有任何 user/assistant 消息. 这种文件"存在但无对话",
# --resume 会报 "No conversation found" → worker 启动即退、不回复(尤其 /clear 之后).
# 检测无真实对话 → 删掉残桩, 下面自动走 --session-id 起全新会话.
if [ -f "$existing_jsonl" ] && ! grep -qE '"type":"(user|assistant)"' "$existing_jsonl" 2>/dev/null; then
  rm -f "$existing_jsonl"
  echo "spawn-worker: 删除无对话残桩 session(可能 /clear 后) @ $uuid" >>"/tmp/spawn-worker.log"
fi

if [ -f "$existing_jsonl" ]; then
  # ─── 重登检测: keychain token 变化 → 本 session 全部 thinking 一次性剥离 ──────
  # 重新登录会换 OAuth accessToken, 旧 thinking blocks 的 signature 随之失效, 交互式
  # worker resume 后 replay 这些 thinking → 401. >12h 的 TTL 兜不住"刚产生就重登"的近
  # 期 thinking. 这里给每个 session 记一个它上次见到的 token 指纹(sidecar 文件), 指纹
  # 一变就把 STRIP_ALL_THINKING 打开, 让下面的剥离块清掉全部 thinking(仅这一次).
  # 每 session 独立比对自己的旧指纹, 不用全局文件 → 无"首个 worker 抢更新"的竞态.
  export STRIP_ALL_THINKING=0
  sess_fp_file="${existing_jsonl}.tokenfp"
  cur_fp=$(/usr/bin/security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
    | /usr/bin/python3 -c "
import sys, json, hashlib
try:
    d = json.load(sys.stdin)
    t = (d.get('claudeAiOauth') or {}).get('accessToken', '')
    print(hashlib.sha256(t.encode()).hexdigest()[:16] if t else '')
except Exception:
    print('')
" 2>/dev/null)
  if [ -n "$cur_fp" ]; then
    prev_fp=$(cat "$sess_fp_file" 2>/dev/null || echo "")
    if [ -n "$prev_fp" ] && [ "$prev_fp" != "$cur_fp" ]; then
      export STRIP_ALL_THINKING=1
      echo "spawn-worker: keychain token 变化(重登) → strip 全部 thinking @ $uuid" >>"/tmp/spawn-worker.log"
    fi
    echo "$cur_fp" > "$sess_fp_file"
  fi

  # ─── 切 provider 检测: BASE_URL/MODEL 变化 → 同样一次性剥离全部 thinking ─────────
  # BA1: 切 provider(改 ANTHROPIC_BASE_URL/MODEL, token 不变)时 token 指纹不变 → 上面
  # 不触发, 但旧 thinking 的 signature 在新上游/新 key 下同样失效 → worker --resume 时
  # 400 "Invalid signature" → resume 失败丢上下文。给每个 session 记一个 provider 指纹,
  # 变了就一次性剥离(纯追加触发器, 最坏多清一次 thinking, 不会 breakage)。
  prov_fp_file="${existing_jsonl}.providerfp"
  cur_prov=$(/usr/bin/python3 -c "
import json, hashlib, os
try:
    d = json.load(open(os.path.expanduser('~/.claude/settings.json')))
    e = d.get('env') or {}
    print(hashlib.sha256((e.get('ANTHROPIC_BASE_URL','')+'|'+e.get('ANTHROPIC_MODEL','')).encode()).hexdigest()[:16])
except Exception:
    print('')
" 2>/dev/null)
  if [ -n "$cur_prov" ]; then
    prev_prov=$(cat "$prov_fp_file" 2>/dev/null || echo "")
    if [ -n "$prev_prov" ] && [ "$prev_prov" != "$cur_prov" ]; then
      export STRIP_ALL_THINKING=1
      echo "spawn-worker: provider 变化(BASE_URL/MODEL) → strip 全部 thinking @ $uuid" >>"/tmp/spawn-worker.log"
    fi
    echo "$cur_prov" > "$prov_fp_file"
  fi

  # ─── CLI 版本变化检测: claude 升级 / 切原生↔npm → thinking signature 失效 → 剥离 ────
  # 频繁升级 claude 时, <12h 的旧版 thinking block 签名在新版 CLI 下 resume 会 400
  # "Invalid signature"。token/provider 指纹都不变(同号同 provider), 只有 CLI 版本变了,
  # 靠 >12h TTL 兜不住近期 thinking → 这里补一道版本指纹(照 provider 指纹同款结构)。
  # 这里的 `claude` 与 worker 启动用的是同一个(上方 line 12 已固定 PATH)。
  ver_fp_file="${existing_jsonl}.clivfp"
  cur_ver=$(claude --version 2>/dev/null | head -1)
  if [ -n "$cur_ver" ]; then
    prev_ver=$(cat "$ver_fp_file" 2>/dev/null || echo "")
    if [ -n "$prev_ver" ] && [ "$prev_ver" != "$cur_ver" ]; then
      export STRIP_ALL_THINKING=1
      echo "spawn-worker: CLI 版本变化($prev_ver → $cur_ver) → strip 全部 thinking @ $uuid" >>"/tmp/spawn-worker.log"
    fi
    echo "$cur_ver" > "$ver_fp_file"
  fi

  # ─── 预防性清**老的** thinking blocks（保留近 12h；重登时清全部）─────────────
  # thinking signature 跨 claude CLI 升级 / 服务端 key rotation 会失效，导致
  # worker --resume 后第一次 LLM 调用 400 "Invalid signature in thinking block"。
  # 但全清会损失 multi-turn 推理连贯。折中：仅剥离 >12h 的老 thinking（高危跨
  # rotation 边界），保留近期的（当前对话上下文里的推理）。
  /usr/bin/python3 -c "
import json, os, shutil, time, sys
from datetime import datetime, timezone
p = '$existing_jsonl'
THINKING_TTL_HOURS = 12
STRIP_ALL = os.environ.get('STRIP_ALL_THINKING') == '1'
try:
    with open(p) as f:
        if '\"type\":\"thinking\"' not in f.read(): sys.exit(0)
except Exception: sys.exit(0)

cutoff = time.time() - THINKING_TTL_HOURS * 3600

def parse_ts(s):
    if not s: return 0.0
    try: return datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
    except: return 0.0

shutil.copy2(p, p + f'.bak.{int(time.time())}')
kept = []
n_old_thinking = 0
n_kept_thinking = 0
with open(p) as f:
    for line in f:
        line = line.rstrip('\n')
        if not line.strip(): kept.append(line); continue
        try: o = json.loads(line)
        except: kept.append(line); continue
        ts = parse_ts(o.get('timestamp', ''))
        msg = o.get('message', {})
        c = msg.get('content')
        if isinstance(c, list):
            nc = []
            for x in c:
                if isinstance(x, dict) and x.get('type') == 'thinking':
                    if STRIP_ALL or (ts and ts < cutoff):
                        n_old_thinking += 1
                        continue   # 老 thinking 剥离（重登时全清）
                    n_kept_thinking += 1
                nc.append(x)
            if not nc: continue   # 整条 message 只有老 thinking → 删
            msg['content'] = nc
        kept.append(json.dumps(o, ensure_ascii=False))
with open(p, 'w') as f: f.write('\n'.join(kept) + '\n')
sys.stderr.write(f'spawn-worker: stripped {n_old_thinking} old thinking (>{THINKING_TTL_HOURS}h), kept {n_kept_thinking} recent\n')
" 2>>"/tmp/spawn-worker.log" || true

  # ─── 尺寸守卫: 防 -p 非交互 session 无限膨胀 → resume 请求体超大 → API timeout ──
  # worker -p 模式永不自动 compact. 真实 LLM context(user+assistant 的 message.content)
  # 超过 170k tokens 时, 结构化压缩到 ~100k(丢最老对话, 近期逐字保留, 纯文件操作不调
  # LLM, 秒级). 200k 窗口留 30k 余量(unified session 群+私聊同脑装得多, 触发线上调; 别
  # 再往上, 顶穿 200k 窗口会 API 报错——"记得久"靠 MEMORY.md 长期记忆, 不靠这个窗口).
  # 老对话保存在 .bak.
  ctx_tokens=$(/usr/bin/python3 -c "
import json
tot=0
for line in open('$existing_jsonl'):
    try: o=json.loads(line)
    except: continue
    if o.get('type') in ('user','assistant'):
        c=(o.get('message') or {}).get('content')
        if c is not None: tot+=len(json.dumps(c,ensure_ascii=False))
print(tot//3)
" 2>/dev/null || echo 0)
  if [ "${ctx_tokens:-0}" -gt 170000 ]; then
    echo "spawn-worker: session ctx ${ctx_tokens} tokens > 170k, 结构化压缩…" >>"/tmp/spawn-worker.log"
    /usr/bin/python3 "$HOME/.claude/dispatcher/compact-worker-session.py" "$existing_jsonl" --apply >>"/tmp/spawn-worker.log" 2>&1 || true
  fi

  session_flag="--resume \"$uuid\""
else
  session_flag="--session-id \"$uuid\""
fi

# Env for this tmux session — passed verbatim to claude + worker MCP via ${VAR} expansion
env_prefix="CHANNEL_DIR=\"$bot_dir\" \
TELEGRAM_WORKER_CHAT_ID=\"$chat\" \
TELEGRAM_WORKER_BOT=\"$bot\" \
TELEGRAM_DISPATCHER_URL=\"$dispatcher_url\""

# ─── 清空 dispatcher 进程继承的旧 ccp env ────────────────────────────
# dispatcher 启动时固化了当时 ccp profile 的 env（OAuth token / BASE_URL / 模型名等），
# 用户 ccp 切换只改 settings.json，不影响 dispatcher 进程 env。worker 通过 tmux
# 继承时这些旧值会污染当前 profile。**必须**先全部清空，下面 settings.json 的 env
# 再 append 进来覆盖（bash `FOO= FOO=val cmd` 后者生效）。任何 settings.json 里
# 没声明的 key 就保持空，不再被继承值污染。
env_prefix="$env_prefix \
  ANTHROPIC_BASE_URL= ANTHROPIC_AUTH_TOKEN= ANTHROPIC_API_KEY= \
  CLAUDE_CODE_OAUTH_TOKEN= \
  ANTHROPIC_MODEL= ANTHROPIC_SMALL_FAST_MODEL= \
  ANTHROPIC_DEFAULT_HAIKU_MODEL= ANTHROPIC_DEFAULT_SONNET_MODEL= ANTHROPIC_DEFAULT_OPUS_MODEL="

# ─── 跟随 cc-profile：从 settings.json env 透传到 worker ─────────────
# 用户用 `ccp deepseek` / `ccp sonnet` 切换会重写 ~/.claude/settings.json 的 env 段。
# launchd 启动的 dispatcher / self-initiate 进程 env 不会自动跟，但每次 spawn-worker
# 现读 settings.json 就能拿到当前 profile 的 ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL。
SETTINGS_JSON="$HOME/.claude/settings.json"
profile_uses_api=false
if [ -f "$SETTINGS_JSON" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    env_prefix="$env_prefix $line"
    case "$line" in
      ANTHROPIC_BASE_URL=*) profile_uses_api=true ;;
    esac
  done < <(/usr/bin/python3 -c "
import json, shlex, sys
try:
    d = json.load(open('$SETTINGS_JSON'))
    for k, v in (d.get('env') or {}).items():
        if k.startswith(('ANTHROPIC_', 'CLAUDE_CODE_', 'DISABLE_', 'ENABLE_')):
            print(f'{k}={shlex.quote(str(v))}')
except Exception as e:
    sys.stderr.write(f'[spawn-worker] read settings.json env failed: {e}\n')
")
fi

# ─── 把 GUI 私有回环代理重定向到常驻 daemon ──────────────────────────────
# GUI 切第三方 provider 时把 ANTHROPIC_BASE_URL 写成它**进程内**的代理
# (anthropic 8789 / openai 8788),GUI 一关那俩端口就没人听 → worker ECONNREFUSED。
# 常驻 daemon(anthropic-proxy-daemon.mjs，launchd 托管)跑在 8799/8798，GUI 开不开都活着。
# 这里把 worker 的 8789→8799、8788→8798，让 bot 永远连常驻 daemon。仅精确替换这两个
# 回环端口，其它 base_url(官方/真上游直连)一律不动。daemon 自己按 marker+cc-switch.db
# 解析真实上游并剥 OAuth，与 GUI 同源。
env_prefix="${env_prefix//127.0.0.1:8789/127.0.0.1:8799}"
env_prefix="${env_prefix//127.0.0.1:8788/127.0.0.1:8798}"

# 订阅模式（profile 不带 ANTHROPIC_BASE_URL）：**不再注入 CLAUDE_CODE_OAUTH_TOKEN**。
# 上面已把它清空（line 131），claude 见到空值会回退读 keychain 的完整凭证
# （Claude Code-credentials，含 refreshToken）并**自动续期**。
#
# 为什么不注入静态 access token（旧做法的坑）：注入静态 token 会**禁用 claude 的
# 自动刷新**。access token ~8h 过期后 worker 无法自愈 → 401；交互式 worker 长跑到
# 需要刷新时也会 401。让 claude 直接读
# keychain 既保证现取最新、又能 refreshToken 续期，根治这类 401。
# API 模式：CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY 已在前面清空，走 settings 的 AUTH_TOKEN。

for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy SSL_CERT_FILE CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD NOVELAI_OUTPUT_DIR; do
  val="${!var:-}"
  if [ -n "$val" ]; then
    env_prefix="$env_prefix $var=\"$val\""
  fi
done

# Launch claude with deterministic --session-id so reconnects resume the same history.
# --channels server:telegram-worker + --dangerously-load-development-channels:
#   required for claude to treat our worker-plugin's notifications/claude/channel
#   as inbound channel messages (the MCP server declares experimental.claude/channel
#   capability but must also be on the session's --channels list).
# --strict-mcp-config: ignore plugin-provided MCP servers (the upstream telegram
#   plugin would otherwise spawn its own grammY polling bot via enabledPlugins
#   and collide with the dispatcher on the same Bot token).
cmd="$env_prefix claude $session_flag \
  --dangerously-skip-permissions \
  --setting-sources user,project,local \
  --add-dir \"$bot_dir\" \
  --append-system-prompt-file \"$bot_dir/CLAUDE.md\" \
  --mcp-config \"$MCP_CONFIG\" \
  --strict-mcp-config \
  --dangerously-load-development-channels server:telegram-worker"

# cwd = $bot_dir so project slug is bot-level (shared auto-memory across chats)
# NOTE: do NOT pipe $cmd through tee/stdbuf/etc — claude detects non-TTY stdout
# and switches to --print mode, which then fails with "Input must be provided".
# If you need to diagnose a crashing worker, use tmux's `set remain-on-exit on`.
tmux new-session -d -s "$session" -c "$bot_dir" "$cmd"

# claude 2.1.92+ shows an interactive "WARNING: Loading development channels"
# dialog on boot and blocks until user presses Enter to confirm option 1.
# Auto-confirm by sending Enter once the dialog text appears (poll up to 20s).
(
  for _ in $(seq 1 40); do
    sleep 0.5
    if tmux capture-pane -t "$session" -p 2>/dev/null | grep -q 'local development'; then
      tmux send-keys -t "$session" Enter
      break
    fi
  done
) &

echo "spawned worker: $session (uuid=$uuid dir=$chat_dir)" >&2
