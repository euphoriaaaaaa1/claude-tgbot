"""读 worker session jsonl，提取 bot 真实出站消息。

这是反重复的真实数据源——引擎自己的 heartbeat_log 只是注入情境，
bot 实际发出去什么需要从 session jsonl 读 assistant role 消息。

slug 算法（与 Claude Code 一致，已实测）：
  ~/.claude/channels/mybot  →  -Users-you--claude-channels-mybot
"""
import os
import glob
import json
import time
import uuid
from datetime import datetime


def _project_slug_for(bot_dir: str) -> str:
    """Claude Code 官方 slug 规则：绝对路径里**非字母数字字符全部**替换为 '-'。
    覆盖 Windows 的反斜杠/盘符冒号（旧版只换 / 和 . 在 Windows 上会算错=丢记忆）。
    与 dispatcher/worker-manager.ts 的 projectSlug() 必须同规则。"""
    import re as _re
    abs_dir = os.path.abspath(bot_dir)
    return _re.sub(r"[^a-zA-Z0-9]", "-", abs_dir)


def _project_dir(bot_dir: str) -> str:
    return os.path.expanduser(f"~/.claude/projects/{_project_slug_for(bot_dir)}")


# bot 名 → UUIDv5 namespace，必须与 dispatcher.ts 的 BOT_NAMESPACES 完全一致。
# worker 的 session jsonl 文件名 = uuid5(namespace, chat_id)，据此可精确定位某个
# chat 的会话文件，不再靠"目录里 mtime 最新的 jsonl"猜（私聊/群聊共享 project 目录，会猜错）。
_BOT_NAMESPACES = {
    "chenlulu": "550e8400-e29b-41d4-a716-446655440001",
}


def unified_session_uuid(bot_name: str) -> str | None:
    """unified session：每个 bot 只有一个 worker 会话（群+私聊同脑），
    uuid = uuid5(namespace[bot], "unified")，与 dispatcher.ts / spawn-worker.sh 一致。"""
    ns = _BOT_NAMESPACES.get(bot_name)
    if not ns:
        return None
    return str(uuid.uuid5(uuid.UUID(ns), "unified"))


def _parse_iso_safe(ts_str: str) -> float:
    if not ts_str:
        return 0.0
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# dispatcher 注入到 worker 的内部任务 marker——这些不是真实用户消息，
# 即使被 <channel ...> xml 包装也要排除掉，否则 since_last_user_min 永远归零。
_INTERNAL_MARKERS = (
    # 框架自身注入的合成消息
    "[self-initiate]",
    "[moment-image-gen]",
    "[voice-image]",
    "[voice-recap]",
    "[moment-react]",
    "[moment-interaction]",   # 实际前缀（原 [moment-react] 拼错，永远匹配不上）
    "[peer-inbound]",
    "[memory-compactor]",
    "[wildcard-daily]",
    "[系统自检]",
    # Claude Code / CLI 原生控制消息（不是用户真实发言）
    "Continue from where you left off.",
    "[Request interrupted by user]",
    "No response requested.",
    "This session is being continued",   # 压缩 marker 文本兜底（双保险）
    "Base directory for this skill",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-",
)


def _is_internal_injection(text: str) -> bool:
    return any(m in text for m in _INTERNAL_MARKERS)


# 跨场景实时记忆(cross_scene.py)注入到 worker 消息里的背景块 sentinel。
# 回读对话尾巴时把整段剥掉：它是别的场景灌进来的背景，不是本场景用户真发的话，
# 否则 self-initiate / voice-recap 会把陈旧的跨场景块当成对话内容复述。
_CROSSMEM_RE = None


def _strip_crossmem(text: str) -> str:
    global _CROSSMEM_RE
    if "⟦跨场景⟧" not in text:
        return text
    if _CROSSMEM_RE is None:
        import re
        _CROSSMEM_RE = re.compile(r"⟦跨场景⟧.*?⟦跨场景完⟧", re.DOTALL)
    return _CROSSMEM_RE.sub("", text).strip()


_VOICE_TEXT_RE = None  # lazy
_CHANNEL_BODY_RE = None


def _strip_channel_wrapper(text: str) -> str:
    """dispatcher 把用户消息包成 <channel ... voice_text="..." ...>BODY</channel>。
    self-initiate prompt 注入对话尾巴时，去掉 xml 包装只留真实内容：
      1. 有 voice_text 属性 → 用 voice_text（语音转写）
      2. 否则 <channel>...</channel> 中间的 BODY（普通文字消息）
      3. 都没有 → 原样返回
    """
    global _VOICE_TEXT_RE, _CHANNEL_BODY_RE
    if _VOICE_TEXT_RE is None:
        import re
        _VOICE_TEXT_RE = re.compile(r'voice_text="([^"]*)"')
        _CHANNEL_BODY_RE = re.compile(r'<channel\b[^>]*>(.*?)</channel>', re.DOTALL)
    if "<channel " not in text:
        return text
    # voice_text 优先（语音消息）
    m = _VOICE_TEXT_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # 普通文字消息：取 <channel ...>...</channel> 中间的 body
    m = _CHANNEL_BODY_RE.search(text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return text


def _extract_text(content) -> str:
    """处理 list[{type,text}] 或 str。过滤 tool_use。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


# bot 真实发出的消息 = 调用了 telegram-worker reply / send_message 的 tool_use
# 用于 get_thread_tail 区分"真发了"和"只思考"
_REAL_REPLY_TOOL_NAMES = ("mcp__telegram-worker__reply",
                          "mcp__telegram-worker__send_message",
                          "mcp__telegram-worker__send")


def _extract_real_reply_text(content) -> str:
    """如果 assistant 调用了 telegram-worker reply 类工具，返回其 input.text。
    没真发就返回空（即 bot 只思考，不应进入接续对话尾巴）。
    """
    if not isinstance(content, list):
        return ""
    for c in content:
        if not isinstance(c, dict) or c.get("type") != "tool_use":
            continue
        if c.get("name") in _REAL_REPLY_TOOL_NAMES:
            inp = c.get("input") or {}
            text = inp.get("text") or inp.get("message") or ""
            if text:
                return text
    return ""


def get_recent_assistant_messages(bot_dir: str, days: int = 3, limit: int = 10) -> list[str]:
    """返回最近 N 天 assistant role 消息文本（前 80 字摘要），最多 limit 条。"""
    proj_dir = _project_dir(bot_dir)
    if not os.path.isdir(proj_dir):
        return []
    files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:5]
    cutoff = time.time() - days * 86400
    msgs = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    ts = _parse_iso_safe(o.get("timestamp", ""))
                    if not ts or ts < cutoff:
                        continue
                    text = _extract_text(o.get("message", {}).get("content", ""))
                    if text and len(text) > 5 and not _is_internal_injection(text):
                        msgs.append(text[:80])
        except (FileNotFoundError, PermissionError):
            continue
    return msgs[-limit:]


def get_recent_dialog(bot_dir: str, days: int = 7, max_chars: int = 12000) -> str:
    """memory_compactor 用：拿最近 N 天的 user+assistant 对话片段。"""
    proj_dir = _project_dir(bot_dir)
    if not os.path.isdir(proj_dir):
        return ""
    files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:10]
    cutoff = time.time() - days * 86400
    lines_out = []
    total_chars = 0
    # 按时间倒序读，再正序输出
    collected = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    # 压缩 marker 行 type=user 但不是真实对话，跳过（否则
                    # "This session is being continued…" 会被当成用户说的话）
                    if o.get("isCompactSummary"):
                        continue
                    ts = _parse_iso_safe(o.get("timestamp", ""))
                    if not ts or ts < cutoff:
                        continue
                    text = _extract_text(o.get("message", {}).get("content", ""))
                    if not text:
                        continue
                    # 跳过框架注入 / CLI 控制消息（不是真实对话）
                    if typ == "user" and _is_internal_injection(text):
                        continue
                    role = "用户" if typ == "user" else "我"
                    snippet = f"[{role}] {text[:200]}"
                    collected.append((ts, snippet))
        except (FileNotFoundError, PermissionError):
            continue
    # 按时间正序
    collected.sort(key=lambda x: x[0])
    for ts, snippet in collected:
        if total_chars + len(snippet) > max_chars:
            break
        lines_out.append(snippet)
        total_chars += len(snippet) + 1
    return "\n".join(lines_out)


def get_recent_voice_log(bot_dir: str, days: int = 7, max_chars: int = 4000) -> str:
    """读电话侧 voice_log.jsonl（voicecall demo 写的），格式同 get_recent_dialog。

    电话里说的事 Telegram session jsonl 里没有，compactor / worker 要把它也纳进来
    才能实现"语音记忆进长期 / 反向共享"。glob 该 bot 所有 chat 的 voice_log。
    每行：{ts,role:'user'|'assistant',text,channel:'voice',image_path?}
    """
    pattern = os.path.join(os.path.abspath(bot_dir), "chats", "*", "voice_log.jsonl")
    files = glob.glob(pattern)
    if not files:
        return ""
    cutoff = time.time() - days * 86400
    collected = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    ts = o.get("ts", 0)
                    if not ts or ts < cutoff:
                        continue
                    text = (o.get("text") or "").strip()
                    if not text:
                        continue
                    role = "用户" if o.get("role") == "user" else "我"
                    collected.append((ts, f"[{role}] {text[:200]}"))
        except (FileNotFoundError, PermissionError):
            continue
    collected.sort(key=lambda x: x[0])
    out, total = [], 0
    for ts, snippet in collected:
        if total + len(snippet) > max_chars:
            break
        out.append(snippet)
        total += len(snippet) + 1
    return "\n".join(out)


def last_user_msg_ts(bot_dir: str, chat_id: str = None,
                     bot_name: str = None) -> int | None:
    """返回最近一条真实用户消息的 unix ts。

    与 mins_since_last_user_msg 共享同一套数据源（marker + jsonl 兜底），
    但返回绝对 ts 而非 mins。给"接续模式判定"用。

    bot_name：dispatcher 写 marker 用 BOT_NAME；若 bot_dir 的 basename 与 bot_name
    不一致，显式传 bot_name 避免 marker 找错（一般两者相同，可不传）。
    """
    state_dir = os.path.expanduser("~/.claude/dispatcher/.self-initiate-state")
    marker_key = bot_name or os.path.basename(os.path.abspath(bot_dir))
    if chat_id:
        marker = os.path.join(state_dir, f"{marker_key}-{chat_id}.last-user")
        if os.path.exists(marker):
            try:
                return int(open(marker).read().strip())
            except Exception:
                pass
    # 兜底：扫最近 jsonl
    proj_dir = _project_dir(bot_dir)
    if not os.path.isdir(proj_dir):
        return None
    files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:3]
    latest_user_ts = 0
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "user":
                        continue
                    text = _extract_text(o.get("message", {}).get("content", ""))
                    if not text or len(text.strip()) < 2:
                        continue
                    if _is_internal_injection(text):
                        continue
                    ts = _parse_iso_safe(o.get("timestamp", ""))
                    if ts > latest_user_ts:
                        latest_user_ts = ts
        except (FileNotFoundError, PermissionError):
            continue
    return int(latest_user_ts) if latest_user_ts > 0 else None


def get_thread_tail(bot_dir: str, chat_id: str = None,
                    n: int = 12, max_hours: int = 72,
                    bot_name: str = None
                    ) -> list[tuple[str, str, int]]:
    """返回最近 N 条 user+assistant 消息：[(role, text, ts), ...]，时间正序。

    给 self-initiate 接续模式用——让 LLM 自己读上次对话尾巴决定怎么接。

    - role: 'user' / 'assistant'
    - user 端：过滤 _INTERNAL_MARKERS（self-initiate 等内部注入不算真实对话），剥掉 <channel> xml
    - assistant 端：**只收真发给用户的消息**（调用了 telegram-worker reply 的 tool_use.input.text），
      跳过"只思考没发"的纯 text 输出（如「跳过/No response requested」等内部独白）—— 防止 bot
      把自己跳过的历史塞进 prompt 后继续选择跳过，形成沉默循环
    - 截 200 字 / 条
    - 只看 max_hours 内的（默认 72h）
    """
    proj_dir = _project_dir(bot_dir)
    if not os.path.isdir(proj_dir):
        return []
    # unified session：群+私聊同一个会话文件 uuid5(bot,"unified")。self-initiate 续接 /
    # voicecall 读尾巴都读这一个（B 后就是"一个脑子"，不再按 chat_id 分文件；chat_id 入参
    # 保留仅为兼容调用方，不再用于定位）。名字缺失或文件不存在时退回旧 glob（测试兜底）。
    su = unified_session_uuid(bot_name) if bot_name else None
    su_path = os.path.join(proj_dir, f"{su}.jsonl") if su else None
    if su_path and os.path.exists(su_path):
        files = [su_path]
    else:
        files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                       key=os.path.getmtime, reverse=True)[:5]
    cutoff = time.time() - max_hours * 3600
    collected: list[tuple[str, str, int]] = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    typ = o.get("type")
                    if typ not in ("user", "assistant"):
                        continue
                    # 压缩 marker 行 type=user 但不是真实对话，跳过（否则
                    # "This session is being continued…" 会被当成用户说的话）
                    if o.get("isCompactSummary"):
                        continue
                    ts = _parse_iso_safe(o.get("timestamp", ""))
                    if not ts or ts < cutoff:
                        continue
                    content = o.get("message", {}).get("content", "")
                    if typ == "assistant":
                        # 只收真发给用户的（tool_use reply 的 input.text），跳过纯思考
                        text = _extract_real_reply_text(content)
                        if not text:
                            continue
                        role = "assistant"
                    else:
                        text = _extract_text(content)
                        if not text or len(text.strip()) < 2:
                            continue
                        if _is_internal_injection(text):
                            continue
                        text = _strip_channel_wrapper(text)
                        text = _strip_crossmem(text)
                        if not text or len(text.strip()) < 2:
                            continue
                        role = "user"
                    collected.append((role, text[:200], int(ts)))
        except (FileNotFoundError, PermissionError):
            continue
    # 按 ts 正序，取最后 n 条
    collected.sort(key=lambda x: x[2])
    return collected[-n:]


def mins_since_last_user_msg(bot_dir: str, chat_id: str = None,
                              bot_name: str = None) -> int | None:
    """读 dispatcher 写的 marker 文件（与 self-initiate.sh 同位置）。
    优先读 marker，没 marker 则扫 jsonl 兜底。

    bot_name：dispatcher 用 BOT_NAME 拼 marker；若 bot_dir.basename 与 bot_name 不同，
    显式传 bot_name 才能找对文件（一般两者相同）。
    """
    state_dir = os.path.expanduser("~/.claude/dispatcher/.self-initiate-state")
    marker_key = bot_name or os.path.basename(os.path.abspath(bot_dir))
    if chat_id:
        marker = os.path.join(state_dir, f"{marker_key}-{chat_id}.last-user")
        if os.path.exists(marker):
            try:
                last = int(open(marker).read().strip())
                return (int(time.time()) - last) // 60
            except Exception:
                pass
    # 兜底：扫最近一个 jsonl
    proj_dir = _project_dir(bot_dir)
    if not os.path.isdir(proj_dir):
        return None
    files = sorted(glob.glob(os.path.join(proj_dir, "*.jsonl")),
                   key=os.path.getmtime, reverse=True)[:3]
    latest_user_ts = 0
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "user":
                        continue
                    # 关键：claude jsonl 里 type=user 包含 tool_result / 系统注入等，
                    # _extract_text 只返回 type==text 部分。空 text 不算真实用户消息。
                    # 否则 worker 启动后产生的 tool_result 会被误算成"用户刚说话"，
                    # → since=0min → prefilter 永远拦下 → bot 全天哑火。
                    text = _extract_text(o.get("message", {}).get("content", ""))
                    if not text or len(text.strip()) < 2:
                        continue
                    # 内部注入消息（dispatcher 用 <channel> xml 包装后 startswith 检查失效，
                    # 改用 substring）
                    if _is_internal_injection(text):
                        continue
                    ts = _parse_iso_safe(o.get("timestamp", ""))
                    if ts > latest_user_ts:
                        latest_user_ts = ts
        except (FileNotFoundError, PermissionError):
            continue
    if latest_user_ts == 0:
        return None
    return int((time.time() - latest_user_ts) // 60)
