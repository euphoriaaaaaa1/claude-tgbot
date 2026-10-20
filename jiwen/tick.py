#!/usr/bin/env python3
"""积温 cron 入口 — Phase 3。

每 5 分钟由平台服务/计划任务触发：
  1. 读 _global.yml.jiwen 配置
  2. 遍历 3 个 bot × 主 chat_id
  3. 加载状态文件 → 漂移 → 检测新对话 → 调 delta → 写回
  4. stderr 输出日志

**不破坏现有逻辑**：jiwen.enabled=false 时 tick 仍跑（更新状态文件备用），
但 judge 不会读这些状态——必须等 Phase 4 集成才生效。

跑：python3 jiwen/tick.py [--bot <name>]
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import argparse
from collections import deque
from datetime import datetime

import platform_lock

# 让 jiwen 自身的导入工作
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))  # claudebotlife/

# 包内导入：顶层 `import engine` 会让同进程里出现两份 engine 模块（reader.py 走的是
# jiwen.engine），dataclass.__eq__ 要求 other.__class__ is self.__class__，
# 于是所有 State 比较恒为 False、isinstance 也恒为 False。
from jiwen import engine, deepseek_delta
import config_loader
# jiwen 刻意复用 chat_history 的这几个私有名（注入标记表是开放集合，抄第二份 = 缺陷复发）：
#   _project_dir / _strip_channel_wrapper / _extract_text / _is_internal_injection
# chat_history 里给它们改名，要同步改这里，否则 launchd 里每 5 分钟静默失败一次。
import chat_history

DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.jiwen-state")

# 旧签名兼容 + slug 规则唯一实现（公开版既有测试/调用方依赖这个别名）
_project_slug_for = chat_history._project_slug_for


# ─── 配置加载 ────────────────────────────────────────────────

def load_jiwen_config():
    """从 _global.yml.jiwen 读全局配置。返回 dict 或 None（未配置）。"""
    g = config_loader.load_global()
    return (g.get("jiwen") or None) if isinstance(g, dict) else None


def get_bot_jiwen_overrides(bot_id: str) -> dict:
    """读 bot yml 的 jiwen 个性化覆盖（可选）。"""
    try:
        cfg = config_loader.load_bot(bot_id)
        return cfg.get("jiwen") or {}
    except Exception:
        return {}


def merge_rates(global_jiwen: dict, bot_overrides: dict) -> engine.Rates:
    """合并全局 rates + bot 覆盖 → engine.Rates。

    bot 级 override 路径：bot_yml.jiwen.rates_override（兼容老 jiwen.rates）
    """
    rates_dict = dict((global_jiwen or {}).get("rates", {}))
    bot_jw = bot_overrides or {}
    # 兼容两种写法：rates_override（推荐，更明确）或 rates（旧）
    rates_dict.update(bot_jw.get("rates_override", {}))
    rates_dict.update(bot_jw.get("rates", {}))

    # 默认 immersion_map（dict 类型不能用 float() 强转）
    default_immersion_map = {
        "reading": 0.6, "cooking": 0.5, "search": 0.4,
        "browse": 0.35, "observe": 0.15, "selfcare": 0.5,
    }
    immersion_map = rates_dict.get("immersion_map") or default_immersion_map

    return engine.Rates(
        connection_per_min=float(rates_dict.get("connection_per_min", 0.0007)),
        pride_decay_per_min=float(rates_dict.get("pride_decay_per_min", 0.003)),
        valence_decay_per_min=float(rates_dict.get("valence_decay_per_min", 0.005)),
        arousal_decay_per_min=float(rates_dict.get("arousal_decay_per_min", 0.01)),
        accel_threshold_min=float(rates_dict.get("accel_threshold_min", 5.0)),
        connection_accel=float(rates_dict.get("connection_accel", 1.0)),
        valence_severe_low=float(rates_dict.get("valence_severe_low", -0.5)),
        valence_severe_multiplier=float(rates_dict.get("valence_severe_multiplier", 0.3)),
        valence_mild_low=float(rates_dict.get("valence_mild_low", -0.2)),
        valence_mild_multiplier=float(rates_dict.get("valence_mild_multiplier", 1.5)),
        # 新增 11 项
        valence_lock_threshold=float(rates_dict.get("valence_lock_threshold", 0.5)),
        valence_lock_factor=float(rates_dict.get("valence_lock_factor", 0.3)),
        arousal_connection_rise_threshold=float(rates_dict.get("arousal_connection_rise_threshold", 0.5)),
        arousal_connection_rise_rate=float(rates_dict.get("arousal_connection_rise_rate", 0.002)),
        pride_defend_threshold=float(rates_dict.get("pride_defend_threshold", 0.4)),
        pride_defend_target=float(rates_dict.get("pride_defend_target", 0.6)),
        pride_defend_rate=float(rates_dict.get("pride_defend_rate", 0.003)),
        pride_arousal_conflict_rate=float(rates_dict.get("pride_arousal_conflict_rate", 0.001)),
        pride_erosion_rate=float(rates_dict.get("pride_erosion_rate", 0.002)),
        immersion_decay_per_min=float(rates_dict.get("immersion_decay_per_min", 0.010)),
        activity_connection_relief=float(rates_dict.get("activity_connection_relief", 0.0005)),
        connection_relief_window_min=float(rates_dict.get("connection_relief_window_min", 240.0)),
        connection_relief_per_min=float(rates_dict.get("connection_relief_per_min", 0.007)),
        connection_relief_floor=float(rates_dict.get("connection_relief_floor", 0.15)),
        # F-02 两条下行通道
        connection_decay_after_min=float(rates_dict.get("connection_decay_after_min", 240.0)),
        connection_silence_decay_per_min=float(rates_dict.get("connection_silence_decay_per_min", 0.002)),
        connection_reply_decay=float(rates_dict.get("connection_reply_decay", 0.05)),
        immersion_map=dict(immersion_map),
    )


# ─── 状态文件读写（原子写）─────────────────────────────────

# 序列化口径就是 engine 那一份，这里只是让调用方不必再 import engine
state_to_dict = engine.state_to_dict
state_from_dict = engine.state_from_dict


def state_path(state_dir: str, bot_id: str, chat_id: str) -> str:
    safe = f"{bot_id}-{chat_id}".replace("/", "_")
    return os.path.join(state_dir, f"{safe}.json")


def load_state(path: str) -> engine.State:
    if not os.path.exists(path):
        return engine.State(last_tick_ts=int(time.time()))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return engine.state_from_dict(d)
    except Exception as e:
        # 解析失败：坏文件改名留档（<name>.corrupt-<ts>），绝不用默认值就地覆盖 ——
        # 覆盖一次就等于把积累的关系/情绪整份清零，且下次又会把整段旧对话重评一遍。
        kept = "，已改名留档"
        try:
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
        except OSError:
            kept = "，改名留档失败"
        # 同上：state 文件名带真人 chat_id，只打所在目录与异常类型
        print(f"[jiwen.tick] 加载状态失败({type(e).__name__}) 于 {os.path.dirname(path)}/{kept}",
              file=sys.stderr)
        return engine.State(last_tick_ts=int(time.time()))


def save_state(path: str, state: engine.State):
    """崩溃安全的原子写：唯一名 tmp → fsync → os.replace。

    两处曾经的坑：
    1. tmp 文件名写死 path + ".tmp"。life-context 会调同一个 save_state 写同一个 path，
       与 launchd 每 5 分钟的 tick 是两个进程；同时写会互相 truncate 同一个 tmp，
       先 rename 的那个把混合内容变成正式 state，后 rename 的那个抛 FileNotFoundError。
       改用 mkstemp 拿唯一名，两个进程各写各的，谁后 replace 谁生效（都是完整内容）。
    2. 没有 fsync。rename 只保证目录项原子替换，不保证 tmp 的数据已落盘，
       掉电后可能得到 0 字节或半截的 json → 下次 load_state 整份回默认 = 情绪清零。

    磁盘满/目录不可写照抛 OSError（由 main() 逐 bot 兜底），失败时不留 tmp 残骸。
    权限 0600：文件名里嵌着真人 chat_id，内容是该用户的情绪画像，不该让同机其他用户读到。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".jiwen-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(engine.state_to_dict(state), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 保留 mkstemp 的 0600，不放宽到原来 open(...,"w") 的 0644：
        # 全部消费方（jiwen/reader.py、director.py、life-context.py）都是同一个用户的
        # launchd agent，同 uid 读得到；而 state 文件将来一旦有人往 last_user_msg_meta
        # 里塞对话元信息，0644 就是直接落在世界可读文件里。
        # 之前那句 chmod 0644 是为"换个身份跑"这个并不存在的场景放宽权限。
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def state_lock(path: str):
    """同一份 state 文件的所有"读→改→写"序列共用这把 <state>.lock（flock 语义）。

    tick 与 life-context 是两个进程、同一个文件；没有跨进程锁时后写的整份覆盖前者。
    锁文件常驻不删：删掉会与"正在等这把锁"的另一进程竞态（它仍持有旧 inode），
    留下的空 .lock 是无害的（glob *.json 不会把它当状态文件）。
    """
    lock_path = path + ".lock"
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with platform_lock.file_lock(lock_path, timeout=None):
        yield


# ─── 对话扫描（找新 user 消息）─────────────────────────────

# 属性值里允许出现 '>'（Telegram 昵称带 > 很常见）：用 [^>]* 会在值中间的 > 处提前收尾，
# 后面的 user_id 就取不到，真人消息被静默判成非真人。两个分支首字符不相交，不会回溯爆炸。
_CHANNEL_OPEN_RE = re.compile(r'^\s*<channel\b((?:"[^"]*"|[^>"])*)>')

# dispatcher 在真人私聊入站时写的"最后一条真人消息发送时刻"。
# ⚠️ 这个路径在 Python 侧共有 4 份副本（chat_history.py 两处、memory/memory_inject.py、
#    这里），再加 self-initiate.sh 一份，改路径要同步全部。
#    没有抽成公共函数是因为本次授权只允许对 chat_history 做标记表那一处纯增量改动。
_MARKER_DIR = "~/.claude/dispatcher/.self-initiate-state"


def _marker_user_ts(bot_id: str, chat_id: str) -> int:
    """读 dispatcher 写的 last-user marker，读不到一律返回 0（当作"没有"）。

    这是"真人最后一次跟我说话是什么时候"这个客观事实的权威来源：dispatcher 只在
    非 bot 发送 + 非合成注入 + chat_type=private 的入站消息上写它，值取 ctx.message.date
    （发送时刻）—— 结构性地不含任何框架注入，不像正文标记表那样是个会漏的开放集合。

    键形 {BOT_NAME}-{chat_id}.last-user，BOT_NAME 就是 bot_id（旧版里 channel
    目录名与 bot_id 可能不同，但 marker 用的是 bot_id）。
    marker 是 writeFileSync 非原子写，可能读到半个数字 → int() 抛 → 当作 0，下 tick 重读。

    "没有 marker"（正常态：首次部署、被清过、dispatcher 没跑过该 bot）与"读不了 marker"
    （权限错、被写成目录、内容损坏）分开：前者静默，由调用方的回落告警负责；
    后者打一行告警，只带 bot_id 与异常类型 —— **不打路径**，marker 文件名里嵌着
    真人的 chat_id，属于用户标识，不进日志。
    """
    # 与 state_path() 同一套净化口径：同一份 bot_id/chat_id 被两个函数用来拼文件名，
    # 只有一处做 replace 是早晚会踩的隐患（今天 bot_id 被 config_loader 的白名单挡着，
    # chat_id 来自受信任的 bot yml，所以不可利用）。
    key = f"{bot_id}-{chat_id}".replace("/", "_")
    path = os.path.join(os.path.expanduser(_MARKER_DIR), f"{key}.last-user")
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return 0
    except Exception as e:
        print(f"[jiwen.tick] 警告: {bot_id} 的 last-user marker 读取失败({type(e).__name__})，按缺失处理",
              file=sys.stderr)
        return 0


def _attr(attrs: str, name: str) -> str:
    """从 <channel …> 开标签的属性串里取一个属性值，取不到返回空串。"""
    m = re.search(r'\b%s="([^"]*)"' % re.escape(name), attrs)
    return m.group(1) if m else ""


def _iso_to_ts(s) -> int:
    """ISO-8601（尾部 Z 等价 +00:00）→ unix 秒。空/格式非法一律 0。"""
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _user_text(raw: str) -> str:
    """从 <channel …>…</channel> 原文里取出真人真正说的话，取不到返回空串。

    R4：worker-plugin 拼的入站正文 = 关系状态块 + 时刻前缀 + 情境锚点 + 场景标签 + 真正的正文，
    再加上 ~190 字的 <channel> 属性串。原来直接 content[:300] 截整行，实测送到判官手里的
    真人正文中位数只剩 8~11 个字，约两成消息一个字都没送到。
    场景标签是正文起点的锚（clear_summary 也按它切），所以按第一个场景标签切最稳。

    与 worker-plugin 的耦合：场景标签文案若改，这里会退到兜底分支（只剥关系块）。
    """
    text = chat_history._strip_channel_wrapper(raw)   # 语音优先取 voice_text，否则取 body
    if text == raw:
        # 剥壳**没有发生**：缺 </channel> 闭合标签（被多余换行劈开的那种撕裂行）时
        # _strip_channel_wrapper 原样返回整行，属性串会被当成用户正文送进判官。整条丢弃。
        # 判据是"剥壳是否发生"而不是"结果里还有没有 <channel 字样"——后者会误伤
        # 正文里恰好提到这个标签名的真消息（那种消息剥壳是成功的，只是内容里带这个词）。
        return ""
    i = text.find("【私聊】")
    j = text.find("【群聊】")
    k = min([x for x in (i, j) if x >= 0], default=-1)
    if k >= 0:
        return text[k + 4:].strip()
    # 兜底：没有场景标签（老消息）时只剥关系状态块
    return re.sub(r'^【当前关系状态】.*?）\s*', '', text, flags=re.S).strip()


# bot 真实发出去的消息 = 调了 telegram-worker 的发送工具；只有 type=="text" 的块是内心独白。
# 直接引用 chat_history 那一份：这个集合**会增长**（将来加 send_photo / send_voice 就是新条目），
# 抄第二份 = 加了新工具时 chat_history.get_thread_tail 与判官窗口对同一条消息给出不同答案，
# 而且没有任何测试抓得到。名字被改掉时这里会 AttributeError 炸在 import，不会静默失效。
_REPLY_TOOLS = chat_history._REAL_REPLY_TOOL_NAMES


def _bot_reply_text(content, human_id: str) -> str:
    """取 bot 真发出去的回复文本，取不到返回空串。

    主修的是 R6：原来只取 type=="text" 块，那是 headless worker 的内心独白 ——
    实测某 bot 有可见 text 的 assistant 行与有真实发送调用的行**交集为 0**，
    判官从没读到过一句真正发给用户的话。改成只认发送工具的调用。

    ⚠️ chat_id 这道闸**拦不住大多数群聊表演回复，别以为这里已经干净了**：
    chat_id 是发送工具的可选参数（缺省 = 回最近一条入站消息所在的 chat），实测三个 bot
    带它的比例是 153/155、57/77、2/16。这里只丢**显式指明了别家 chat** 的那些，
    缺省的一律保留 —— 也就是说，对最后那个 bot，14/16 条群聊回复仍会进判官窗口。
    不做严格相等（只收 chat_id == human_id）的理由：那会把该 bot 87% 的真实私聊回复
    一起丢光，判官那侧直接变哑，比放进几条群聊台词更糟。这是方案阶段权衡后的裁定。

    ponytail: 真要拦干净得靠"配对"而不是靠这道闸 —— unified 会话里 assistant 回合紧跟
    它响应的 user 行，倒序扫时可以把待定的 assistant 文本缓存住，遇到下一条 user 行时
    按那条的 scene 决定整批收还是整批丢。等判官 delta 明显被群聊台词带偏了再上。
    """
    if not isinstance(content, list):
        return ""
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        if b.get("name") not in _REPLY_TOOLS:
            continue
        inp = b.get("input")
        if not isinstance(inp, dict):
            continue
        cid = inp.get("chat_id")
        if cid is not None and str(cid) != str(human_id):
            continue
        text = inp.get("text") or inp.get("message") or ""
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _is_human_message(raw: str, human_id: str) -> bool:
    """真人私聊发言 = ①有 telegram channel 外壳 ②scene=private ③发件人是真人本人
    ④正文不带已登记的框架注入标记。四条全中才算，缺一即判为非真人（fail-closed）。

    为什么必须是合取：director.inject_dm 写的注入 payload 是
    chat_id=真人 / from_id=真人 / scene=private —— 在 <channel> 属性上与真人私聊
    **完全同形**，只看发件人拦不住；而框架注入标记表里没有导演剧本用的那个标记，
    只看正文也拦不住。

    ⚠️ 闸④的标记表是**开放集合且已知不完整**（生产上至少有一类注入不在表里），
    所以本函数只用于"给判官取上下文窗口"和 marker 缺失时的回落，
    **不是** last_user_msg_ts 这个客观事实的权威来源。
    """
    m = _CHANNEL_OPEN_RE.match(raw)
    if not m:
        return False                                     # 闸1：CLI 原生行（工具结果/压缩摘要等）
    attrs = m.group(1)
    if _attr(attrs, "scene") != "private":
        return False                                     # 闸2：群聊不算（真人在群里说话是本次的明确非目标）
    # 闸3：依赖"Telegram 私聊 chat_id == 对方 user id"。这不是巧合：director.py 用 HUMAN_ID
    # 常量拼 state 文件名去读 jiwen 状态，tick 用 bot_cfg["chat_id"] 拼同一个文件名，
    # 两者不等的话 director 现在就一个状态都读不到 —— 是现有代码已经依赖的结构性等式。
    # 生产数据里 user_id 属性覆盖率 100%，缺失一律判非真人，由下面的告警暴露。
    if _attr(attrs, "user_id") != str(human_id):
        return False
    return not chat_history._is_internal_injection(raw)   # 闸4：已登记的框架注入


def _channel_scene(raw: str) -> str | None:
    """从带 <channel> 外壳的正文里取 scene 属性；无外壳返回 None。"""
    m = _CHANNEL_OPEN_RE.match(raw)
    if not m:
        return None
    return _attr(m.group(1), "scene")


def _is_foreign_private(raw: str, human_id: str) -> bool:
    """"配错了"探针：这条是私聊、不是框架注入，但发件人不是配置里那个人。

    与 _is_human_message 的差别只在闸3取反 —— 闸1/2/4 都过才算数，
    否则群聊行和框架注入行也会把探针点亮（它们同样带 <channel> 外壳）。
    """
    m = _CHANNEL_OPEN_RE.match(raw)
    if not m or _attr(m.group(1), "scene") != "private":
        return False
    if _attr(m.group(1), "user_id") == str(human_id):
        return False
    return not chat_history._is_internal_injection(raw)


def _legacy_find_recent_messages(channel_dir: str, since_ts: int,
                                 limit: int = 12) -> tuple[list[dict], int]:
    """公开版旧签名 (channel_dir, since_ts, limit) 的行为：取 mtime 最新的 jsonl。

    保留它只为旧调用方/回归测试；生产主路径已换成 unified 会话 + marker 的
    ``_find_recent_messages_unified``。
    """
    project_root = os.path.expanduser("~/.claude/projects")
    proj_dir = os.path.join(project_root, _project_slug_for(channel_dir))
    if not os.path.isdir(proj_dir):
        return [], since_ts
    jsonls = sorted(
        [f for f in os.listdir(proj_dir) if f.endswith(".jsonl")],
        key=lambda x: os.path.getmtime(os.path.join(proj_dir, x)),
        reverse=True,
    )
    if not jsonls:
        return [], since_ts
    latest = os.path.join(proj_dir, jsonls[0])
    msgs, max_user_ts = [], since_ts
    try:
        with open(latest, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return [], since_ts
    for line in reversed(lines):
        if len(msgs) >= limit:
            break
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content
                               if isinstance(c, dict) and c.get("type") == "text").strip()
        if not isinstance(content, str) or not content.strip() or role not in ("user", "assistant"):
            continue
        ts = _iso_to_ts(obj.get("timestamp") or obj.get("ts"))
        if role == "user" and ts > max_user_ts:
            max_user_ts = ts
        content = re.sub(r'【当前关系状态】.*?(?=【群聊】|【私聊】)', '', content, flags=re.S)
        content = re.sub(r'^【当前关系状态】.*?）\s*', '', content, flags=re.S)
        msgs.append({"role": role, "content": content[:300], "ts": ts})
    msgs.reverse()
    if since_ts > 0:
        msgs = msgs[-min(limit, max(6, len(msgs))):]
    return [{"role": m["role"], "content": m["content"]} for m in msgs], max_user_ts


def find_recent_messages(channel_dir: str, human_id=None, bot_id=None,
                         limit: int = 12) -> tuple[list[dict], int]:
    """取最近若干条对话，喂给情绪判官。

    兼容两套签名：
    - 新（CBL）：``find_recent_messages(channel_dir, human_id, bot_id, limit=12)``
    - 旧（公开版）：``find_recent_messages(channel_dir, since_ts, limit=12)``
    第二个参数是数字时按旧签名处理。
    """
    if isinstance(human_id, (int, float)) and not isinstance(human_id, bool):
        legacy_limit = bot_id if isinstance(bot_id, int) and not isinstance(bot_id, bool) else limit
        return _legacy_find_recent_messages(channel_dir, int(human_id), legacy_limit)
    if not human_id or not bot_id:
        raise ValueError("find_recent_messages: human_id 与 bot_id 必填")
    return _find_recent_messages_unified(channel_dir, str(human_id), str(bot_id), limit)


def _find_recent_messages_unified(channel_dir: str, human_id: str, bot_id: str,
                                  limit: int = 12) -> tuple[list[dict], int]:
    """从 bot 的 unified 会话 jsonl 里取最近若干条对话，喂给情绪判官。

    返回 (messages, max_user_ts)：
      messages: [{"role":"user"|"assistant", "content":str}]，按文件出现顺序
      max_user_ts: 窗口内用户发言的最大时间戳（unix 秒），无用户发言时为 0
    """
    # 入参在读任何文件之前先校验：这几个值错了必然是调用方 bug，静默返回空窗口
    # 只会重演"游标纹丝不动却没人知道"的故障形态。
    if not human_id or not str(human_id).strip():
        raise ValueError("find_recent_messages: human_id 必填")
    if not bot_id or not str(bot_id).strip():
        raise ValueError("find_recent_messages: bot_id 必填")
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("find_recent_messages: limit 必须为正整数")

    if not channel_dir:
        return [], 0
    # ⚠️ 两个入参不可互换：_project_dir 吃的是 **channel 目录路径**（bot_cfg.bot_channel_path），
    # unified_session_uuid 吃的是 **bot_id**。两者不恒等——旧版里 bot_id 与
    # channel 目录名可能不同，拿 bot_id 去拼目录会得到不存在的路径。
    proj_dir = chat_history._project_dir(channel_dir)
    if not os.path.isdir(proj_dir):
        return [], 0

    # R5：不再取"目录里 mtime 最新的 jsonl"。统一会话后每个 bot 只有一个正确的会话文件，
    # 同目录还躺着历史文件，猜错就静默读到空壳（旧版曾因此让某只 bot 的游标从未推进）。
    uuid_ = chat_history.unified_session_uuid(bot_id)
    if not uuid_:
        print(f"[jiwen.tick] 警告: {bot_id} 不在 chat_history._BOT_NAMESPACES 里，无法定位会话文件",
              file=sys.stderr)
        return [], 0
    latest = os.path.join(proj_dir, uuid_ + ".jsonl")
    try:
        names = [x for x in os.listdir(proj_dir) if x.endswith(".jsonl")]
    except OSError as e:
        print(f"[jiwen.tick] 警告: {bot_id} 会话目录读取失败({type(e).__name__})", file=sys.stderr)
        return [], 0
    newest = 0.0
    for x in names:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(proj_dir, x)))
        except OSError:
            continue   # 竞态：刚 listdir 到就被轮换/删掉了，跳过这一个，不影响主流程
    if not os.path.exists(latest):
        # 刻意不退回 mtime 兜底：宁可响一声，也不要静默读错文件（那正是 R5 的病根）
        print(f"[jiwen.tick] 警告: {bot_id} unified 会话文件不存在: {os.path.basename(latest)}",
              file=sys.stderr)
        return [], 0
    try:
        # 陈旧超 1 天才喊，避免日志刷屏；说明 worker 可能已经不写 unified 会话了。
        # 这里必须容错：exists() 与 getmtime() 之间文件被轮换/删掉是真实竞态，
        # 让 OSError 穿出去会冒到 main()，**本 bot 本 tick 整个不落盘** ——
        # 与 F2"外部异常不该吞掉整个 tick"的设计意图正好相反。
        if newest > os.path.getmtime(latest) + 86400:
            print(f"[jiwen.tick] 警告: {bot_id} unified 会话文件已 1 天以上不是目录里最新的",
                  file=sys.stderr)
    except OSError:
        pass   # 陈旧检查是纯提示，取不到 mtime 就不提示，不影响下面正常读文件

    msgs = []
    max_user_ts = 0
    # 探针：私聊场景、非框架注入、但发件人不是配置里那个人 —— 只有这种形态才真的指向"chat_id 配错"。
    # 不能拿"有 <channel> 行但没真人"当判据：群聊行和框架注入行都带 channel 外壳，
    # 真人一天没私聊就会每 5 分钟误报一次（4 bot × 288 tick ≈ 每天上千条），
    # 探针天天喊 = 真出问题时没人分辨，等于亲手废掉刚装的探针。
    saw_foreign_private = False
    saw_human = False

    try:
        # errors="replace" 与 chat_history 全文读 jsonl 的口径一致：CLI 侧写入被截断时
        # 会留下半个汉字，不降级则整份会话读取失败（该行的 json.loads 仍会失败，由下面的
        # try/except 逐行跳过——单条记录丢失，其余 3900+ 条照常可见）。
        # deque 尾窗：会话有 6MB/4000 行量级，只需要末尾一小段，内存 O(2000 行) 而非 O(文件)。
        # 再加一道字节上限：deque 封的是**行数**，CLI 会话里单行 tool_result 可以很大，
        # 2000 条大行仍能吃掉几百 MB。Telegram 单条消息上限 4096 字符，64KB 有十几倍余量，
        # 超过这个尺寸的行必然是工具结果之类的噪声，本来也一条都进不了窗口。
        with open(latest, encoding="utf-8", errors="replace") as f:
            lines = deque((ln for ln in f if len(ln) <= 65536), maxlen=2000)
    except Exception as e:
        print(f"[jiwen.tick] 读 jsonl 失败({type(e).__name__}): {bot_id} 的 unified 会话文件",
              file=sys.stderr)
        return [], 0

    # 倒序扫，只有合格消息才占名额（噪声行不占），凑够 limit 条即止
    n_asst = 0
    n_future = 0                       # 时间戳超前于现在的用户消息条数（只计数，不打正文）
    ts_ceiling = int(time.time()) + 300   # 容 5 分钟时钟误差
    # F-25：倒序扫时 assistant 行先于它响应的那条 user 行出现，先缓存住，
    # 等遇到"它前面那条 user 行"再按那条的 scene 决定整批收（private）还是整批丢（group）。
    _pending_asst: list[str] = []

    def _flush_asst(scene):
        nonlocal n_asst
        if scene is None:
            return                      # 无 scene 的 user 行（工具结果等）→ 归属暂不变，保留缓存
        if scene != "private":          # 群聊/其它 → 私聊判官不吃，整批丢
            _pending_asst.clear()
            return
        for t in _pending_asst:
            if len(msgs) >= limit or n_asst >= limit // 2:
                break
            n_asst += 1
            msgs.append({"role": "assistant", "content": t[:300]})
        _pending_asst.clear()

    for line in reversed(lines):
        if len(msgs) >= limit:
            break
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            raw = chat_history._extract_text(content)
            if not raw.strip():
                continue
            # 先按本条 user 行的 scene 结算缓存的 assistant 台词，再处理 user 行本身
            _flush_asst(_channel_scene(raw))
            if not _is_human_message(raw, human_id):
                if _is_foreign_private(raw, human_id):
                    saw_foreign_private = True
                continue
            saw_human = True
            text = _user_text(raw)
            if not text:
                continue                        # 剥壳失败/正文为空 —— 整条丢弃，不喂判官
            # 时钟统一：优先取 <channel ts>（消息**发送**时刻，与 dispatcher 的 marker 同一
            # 时钟），取不到才回落 jsonl 的落盘时刻。生产上实测过一条投递延迟 34 分钟的语音，
            # 两个时刻差得足以让游标系统性滞后。
            ts = (_iso_to_ts(_attr(_CHANNEL_OPEN_RE.match(raw).group(1), "ts"))
                  or _iso_to_ts(obj.get("timestamp")) or _iso_to_ts(obj.get("ts")))
            # 未来时刻不可信（NTP 回拨、时区/夏令时错、人为改钟）：它会把 last_delta_ts
            # 顶到未来，而那个游标**没有任何路径能把它拉回来**（不像 last_user_msg_ts 每
            # tick 被 marker 无条件覆盖），此后 has_new_user_msg 恒假 = 该 bot 永久停止
            # 评分且一个字的日志都没有 —— 正是本次要修的故障形态换个字段复发。
            if ts > ts_ceiling:
                n_future += 1
            elif ts > max_user_ts:
                max_user_ts = ts
            msgs.append({"role": "user", "content": text[:300]})

        elif role == "assistant":
            text = _bot_reply_text(content, human_id)
            if not text:
                continue
            _pending_asst.append(text)          # 归属待定，等前面那条 user 行结算

    # 窗口里没有 user 行使 assistant 结算（如只有 bot 回复）→ 无 scene 线索，默认按私聊收下
    _flush_asst("private")

    msgs.reverse()  # 倒序变正序
    if n_future:
        print(f"[jiwen.tick] 警告: {bot_id} 窗口里有 {n_future} 条时间戳超前于当前时刻的用户消息，"
              f"已不计入游标（时钟异常？）", file=sys.stderr)
    if saw_foreign_private and not saw_human:
        # 静默失效变可见：真人 chat_id 配错/换号/迁群时，判定会全假并悄悄退回本次修的这个 bug
        print(f"[jiwen.tick] 未识别到真人消息: {bot_id} 私聊里出现的 user_id 都不是配置里那个人，"
              f"请核对 bot 配置里的 chat_id", file=sys.stderr)
    return msgs, max_user_ts


def has_new_user_msg(msgs: list[dict], since_ts: int, max_user_ts: int) -> bool:
    """检测是否有新 user 消息触发 delta 计算。since_ts 传的是 last_delta_ts。"""
    return max_user_ts > since_ts and any(m["role"] == "user" for m in msgs)


# 判官返回的情绪维度，用于识别"这返回值根本不是一份评分"。
# 当前的 deepseek_delta.compute_delta 是白名单构造返回值的（要么恰好几个键，要么 None），
# 缺键分支**在当前实现下不可达**——它防的是将来换判官客户端/换供应商时返回结构变形，
# 那时宁可当失败重试，也不要把一份空 delta 记成"已评分"、把游标白推过去。
_DELTA_KEYS = ("connection", "pride", "valence", "arousal", "immersion", "desire")


def _delta_digest(delta: dict) -> str:
    """把判官返回的 delta 摘成"只有已知维度和数字"的一行，给日志用。

    以前直接把整个 delta dict 拼进日志。当前安全只是因为 compute_delta 用白名单重建了
    返回值（几个浮点）；哪天有人让它透传原始 JSON（比如加个 reason 解释评分理由），
    对话内容就会直接进 launchd 日志。把安全性钉死在本地，不依赖上游怎么写。
    自身绝不抛：它只是展示，不该把已经成功的 delta 变成"调用失败"。
    """
    out = []
    for k in _DELTA_KEYS:
        if k not in delta:
            continue
        try:
            out.append(f"{k[0].upper()}{float(delta[k]):+.2f}")
        except (TypeError, ValueError):
            out.append(f"{k[0].upper()}?")
    return "[" + " ".join(out) + "]"


# ─── 单 bot tick ────────────────────────────────────────────

def apply_event_emotion(state: engine.State, ev: dict | None, prev_tick_ts: int):
    """M1 事件→情绪桥：本 tick 窗口内【新开始】的事件(started_at>prev_tick_ts)，把它的
    mood_delta 写进 valence(+少量 arousal 表强度)。这是**与用户无关**的情绪源——bot 因
    自己的事(汤糊了/追的剧完结)真的有情绪波动。只应用一次(靠 started_at>prev_tick_ts 去重)。
    返回 (新state, 应用的mood_delta 或 None)。"""
    if not ev or not ev.get("mood_delta"):
        return state, None
    try:
        if int(ev.get("started_at", 0)) <= int(prev_tick_ts):
            return state, None  # 不是本 tick 窗口内新开始的 → 已应用过，跳过
        md = float(ev["mood_delta"])
    except (TypeError, ValueError):
        return state, None
    new = engine.apply_delta(state, {"valence": md, "arousal": abs(md) * 0.3})
    return new, md


def tick_one_bot(bot_id: str, jiwen_cfg: dict, state_dir: str, dry_run: bool = False) -> dict:
    """对一个 bot 跑一次 tick。返回操作摘要。"""
    summary = {"bot": bot_id, "actions": []}

    try:
        bot_cfg = config_loader.load_bot(bot_id)
    except Exception as e:
        # 同上，bot yml 出错时异常字符串会带出源码片段，那里面有真人 chat_id
        summary["error"] = f"load_bot: {type(e).__name__}"
        return summary

    chat_id = str(bot_cfg.get("chat_id", "")).strip()
    if not chat_id:
        summary["error"] = "no chat_id"
        return summary

    channel_dir = bot_cfg.get("bot_channel_path", "")
    if not channel_dir:
        summary["error"] = "no bot_channel_path"
        return summary

    bot_overrides = bot_cfg.get("jiwen") or {}
    rates = merge_rates(jiwen_cfg, bot_overrides)

    # 1. 加载状态
    sp = state_path(state_dir, bot_id, chat_id)
    _existed = os.path.exists(sp)
    state = load_state(sp)
    # 解析失败时 load_state 把坏文件改名留存 → 原路径消失。据此判定"本 tick 不得写回"，
    # 免得用默认值整份覆盖（关系/情绪累积清零）。
    _state_corrupt = _existed and not os.path.exists(sp)
    now = int(time.time())
    prev_tick_ts = state.last_tick_ts  # M1 事件去重用：记录漂移前的时间点

    # M2 作息冻结："想你"(connection)在上课/上班/睡觉时不涨(base_rate_override=0)，
    # 下班/醒来才恢复。三人作息不同 → 天然错峰，不再集体撑不住想你。
    # 生活作息关着（PLAN-bot-features D2）：这只没有作息，不冻结，_act 也不取（下面喂 energy/desire 按"取不到"处理）。
    # 开关从这只自己的频道目录读（bot_features.read：文件不在全开，读不出全关）。
    _base_override = None
    _busy_reason = ""
    try:
        import bot_features
        from generators import situation as _sit
        from datetime import datetime as _dtm
        if bot_features.read(os.path.expanduser(channel_dir))["features"]["life"]:
            _act = _sit.get_current_recurring(bot_cfg, _dtm.fromtimestamp(now))
            if _act.state.startswith("busy") or _act.state == "sleeping":
                _base_override = 0.0
                _busy_reason = _act.state
    except Exception as e:
        summary["actions"].append(f"作息冻结 skip: {type(e).__name__}")

    # 2. 扫窗口 + 解析"真人最后一次说话"这个客观事实
    #    **必须在漂移之前**：drift 的 since_last_msg_min 吃的就是这个值，放在漂移之后刷新
    #    等于本 tick 用的是上一 tick 的游标 —— 用户刚说完话的那个 tick，drift 仍会按
    #    "很久没说话"跑加速（accel 阈值 5min、pride_defend 阈值 30min 都在这个误差量级）。
    msgs, max_user_ts = find_recent_messages(channel_dir, chat_id, bot_id, limit=12)

    # F1：客观事实由 dispatcher 的 marker 权威提供，会话扫描只负责给判官取上下文窗口。
    # **直接赋值，不与旧值取 max**：生产上有 bot 的旧值是导演剧本时刻、self-initiate
    # 时刻，都**超前于**真值，取 max 会把污染永久固化，游标再也回不到真值。
    marker_ts = _marker_user_ts(bot_id, chat_id)
    if marker_ts:
        state.last_user_msg_ts = marker_ts
    elif max_user_ts:
        # marker 缺失才回落到扫描值。回落值可能含未登记的注入（标记表是开放集合），
        # 所以打一条告警；生产四个 bot 的 marker 都在，正常路径不走这里。
        state.last_user_msg_ts = max_user_ts
        print(f"[jiwen.tick] 警告: {bot_id} 无 last-user marker，游标回落到会话扫描"
              f"（可能被未登记的框架注入污染）", file=sys.stderr)

    # 3. 漂移
    if state.last_tick_ts > 0:
        mins = (now - state.last_tick_ts) / 60.0
    else:
        mins = 0.0  # 首次跑
    if mins > 0.1:  # >6 秒才漂
        old_conn = state.connection
        state = engine.drift(state, rates, mins=mins,
                             since_last_msg_min=(
                                 (now - state.last_user_msg_ts) / 60.0
                                 if state.last_user_msg_ts > 0 else 0
                             ),
                             base_rate_override=_base_override)
        _froze = f" [作息冻结:{_busy_reason}]" if _base_override == 0.0 else ""
        summary["actions"].append(f"drift {mins:.1f}min: connection {old_conn:.3f}→{state.connection:.3f}{_froze}")

    # 3.4 F-02：connection 的两条下行通道（此前只涨不落，bot 长期卡在"必须开口"）
    #   ① bot 的主动消息真正送达（窗口里出现 bot 发出的回复）→ 回落固定量
    #   ② 用户沉默超过 connection_decay_after_min → 按小步回落
    try:
        if msgs and any(m.get("role") == "assistant" for m in msgs):
            _c0 = state.connection
            state = engine.apply_delta(state, {"connection": -rates.connection_reply_decay})
            summary["actions"].append(
                f"主动消息送达 → connection {_c0:.3f}->{state.connection:.3f}")
        _silence_min = ((now - state.last_user_msg_ts) / 60.0
                        if state.last_user_msg_ts > 0 else 0.0)
        if _silence_min > rates.connection_decay_after_min:
            _c0 = state.connection
            _step = rates.connection_silence_decay_per_min * min(mins, 60.0)
            state = engine.apply_delta(state, {"connection": -_step})
            summary["actions"].append(
                f"久未开口 {_silence_min:.0f}min → connection {_c0:.3f}->{state.connection:.3f}")
    except Exception as e:
        summary["actions"].append(f"connection 下行 skip: {type(e).__name__}")

    # 3.5 M1 事件→情绪桥：本 tick 窗口内新开始的自己的事(events.yaml/db)→ 情绪波动(与用户无关)
    try:
        import db as _db
        _ev = _db.get_ongoing_event(bot_id, since_ts=prev_tick_ts)
        state, _md = apply_event_emotion(state, _ev, prev_tick_ts)
        if _md is not None:
            summary["actions"].append(
                f"事件[{_ev.get('name')}] mood_delta={_md:+.2f} → valence {state.valence:.3f}(与用户无关)")
    except Exception as e:
        summary["actions"].append(f"事件→情绪桥 skip: {type(e).__name__}")

    # 4. 调 delta
    _jiwen_delta = None  # 捕获本 tick 的 jiwen delta，下面映射到关系数值
    # 触发判据用 last_delta_ts（已评过分的位置），两个实参同源同时钟：都来自本次窗口扫描。
    # 不用 marker 触发：worker 停摆期 marker 先跳、jsonl 后落，会把游标推过头，
    # 真消息落盘后就永久漏评了。
    # 窗口内"比评分位置更新"的用户发言 = 待评新增；没有时（下面 4.5）关系数值才缓慢回落。
    _has_new = bool(msgs and has_new_user_msg(msgs, state.last_delta_ts, max_user_ts))
    if _has_new:
        # api_key 由 main() 经 config_loader.get_deepseek_api_key() 解析后注入，这里不读盘
        delta_cfg = (jiwen_cfg or {}).get("delta_llm") or {}
        api_key = delta_cfg.get("api_key", "")
        if not api_key:
            summary["actions"].append("skip delta: api_key 未配置")
        else:
            # F2：DeepSeek 返回值是本流程唯一的真外部输入，这里是它唯一的入口。
            # 以前 compute_delta 与 apply_delta 都裸奔，任一处抛异常就冒泡到 main()、
            # **整个 tick 完全不落盘**——失败形态和 delta 失败时冻结游标一模一样。
            # 异常一律等同"调用失败"：不推进 last_delta_ts，但继续往下走到落盘。
            try:
                # DeepSeek-V4 1M 上下文，传完整 persona（不截断）
                persona = bot_cfg.get("persona_summary", "") or ""
                bot_jiwen = bot_cfg.get("jiwen") or {}
                delta_hints = bot_jiwen.get("delta_hints", "") or ""
                delta = deepseek_delta.compute_delta(
                    persona=persona,
                    messages=msgs,
                    api_key=api_key,
                    delta_hints=delta_hints,
                    base_url=delta_cfg.get("base_url", deepseek_delta.DEFAULT_BASE_URL),
                    model=delta_cfg.get("model", deepseek_delta.DEFAULT_MODEL),
                    proxy=delta_cfg.get("proxy", deepseek_delta.DEFAULT_PROXY),
                    timeout=delta_cfg.get("timeout", deepseek_delta.DEFAULT_TIMEOUT),
                )
                if delta is None:
                    # API 失败：不 apply、不推进游标 → 下 tick 自动重试，这段对话不丢
                    summary["actions"].append("delta 调用失败(None)→不推进游标，下tick重试")
                elif not isinstance(delta, dict) or not any(k in delta for k in _DELTA_KEYS):
                    # 一个已知维度都没有 = 返回值不是一份评分（见 _DELTA_KEYS 的说明）
                    summary["actions"].append("delta 调用失败(结构缺键)→不推进游标，下tick重试")
                else:
                    old = (state.connection, state.pride, state.valence, state.arousal)
                    state = engine.apply_delta(state, delta)
                    _jiwen_delta = delta
                    state.last_delta_ts = max_user_ts   # 先落游标，再做纯展示的格式化
                    new = (state.connection, state.pride, state.valence, state.arousal)
                    summary["actions"].append(
                        f"delta applied {_delta_digest(delta)} → C{old[0]:.3f}->{new[0]:.3f} "
                        f"P{old[1]:.3f}->{new[1]:.3f} V{old[2]:.3f}->{new[2]:.3f} A{old[3]:.3f}->{new[3]:.3f}"
                    )
            except Exception as e:
                # 异常类型进日志，免得宽 except 把编程错误也静静吞成"API 失败"。
                # 不 catch BaseException：KeyboardInterrupt/SystemExit 照常传播。
                summary["actions"].append(
                    f"delta 调用失败({type(e).__name__})→不推进游标，下tick重试")

    # 4.5 关系数值：energy 随时段漂移(治随叫随到) + 复用 jiwen delta 慢速推进 好感/信任/淫欲
    try:
        import relationship as _rel
        from datetime import datetime as _dt3
        # relationship._path 以 ~/.claude/channels/<目录名> 拼路径：取配置里的频道目录名，
        # 不用 bot_id（两者可能不同，例如旧版目录名与 bot_id 不一致）。
        _bot_dir = os.path.basename(os.path.normpath(os.path.expanduser(channel_dir))) or bot_id
        _prof = "established"
        _rstats = _rel.load(_bot_dir, _prof)
        # 当前活动/事件(上面 M1/M2 已取，取不到则 None)——喂给 energy/desire 让"当日行为"驱动
        _astate = getattr(_act, "state", None) if "_act" in dir() and _act else None
        _event = _ev if "_ev" in dir() else None
        if mins > 0.1:
            _now3 = _dt3.fromtimestamp(now)
            _rstats = _rel.drift_energy(_rstats, _now3, mins, activity_state=_astate,
                                        event=_event, salt=_bot_dir)  # salt→每bot各自随机
            _rstats = _rel.drift_desire(_rstats, _now3, mins,
                                        affection=_rstats.get("affection", 50), event=_event, profile=_prof)
        # F-01：本 tick 无待评新增 → 好感/信任向基线缓慢回落（每日累计不超 DECAY_DAILY_CAP）
        if mins > 0.1 and not _has_new:
            _rstats = _rel.decay_affection_trust(_rstats, mins, _prof)
        if _jiwen_delta:  # 有新对话 → 映射到关系数值(保守系数，慢速累积)
            _vd = float(_jiwen_delta.get("valence", 0))
            # 判官若给了专门的 desire(性张力)用它，否则退回 arousal*正向(见 deepseek_delta)
            _dd = float(_jiwen_delta.get("desire", _jiwen_delta.get("arousal", 0)))
            # 例假/生病在场：不涨 desire(否则热聊几句就把压制顶穿)
            _ev_suppress = bool(_event and any(k in (_event.get("name", "") or "")
                                               for k in ("例假", "生病", "感冒", "痛经", "姨妈")))
            _rstats = _rel.apply_delta(_rstats, {
                "affection": _vd * 8,                       # 聊得开心→更喜欢，惹到→掉
                "trust": _vd * 4 if _vd >= 0 else _vd * 6,  # 难建易毁：正向慢涨(×4)、被惹/冷暴力快掉(×6)
                "desire": (_dd * 10 if _vd >= 0 else 0) if not _ev_suppress else 0,
            })
        if not dry_run:
            _rel.save(_bot_dir, _rstats)
        summary["actions"].append(
            f"关系 好感{_rstats['affection']:.0f}/信任{_rstats['trust']:.0f}/淫欲{_rstats['desire']:.0f}/精力{_rstats['energy']:.0f}")
    except Exception as e:
        summary["actions"].append(f"关系数值 skip: {type(e).__name__}")

    # 5. 写回状态
    state.last_tick_ts = now  # 无条件推进(drift 在 mins<=0.1 时被跳过不更新 → 否则 M1 事件会重复应用)
    if not dry_run and not _state_corrupt:
        # 锁内重读-合并-写回：tick 的读-评-写与 life-context 的 set_activity 写回共用这把锁。
        # 重读是为了不覆盖两进程间 life-context 刚写下的活动；tick 本 tick 不管活动，
        # 磁盘上若有活动而内存里没有，就把它并进来（immersion 取高者，别让漂移把活动抹掉）。
        with state_lock(sp):
            _latest = load_state(sp)
            if _latest.last_activity and not state.last_activity:
                state.last_activity = dict(_latest.last_activity)
                state.immersion = max(state.immersion, _latest.immersion)
            save_state(sp, state)
        # 只打目录：文件名里嵌着真人 chat_id，属于强标识符，不进日志。
        # 需要定位具体文件时看 main() 启动那行的 state_dir + 本行上方的 bot 名。
        summary["actions"].append(f"saved → {os.path.dirname(sp)}/")
    elif dry_run:
        summary["actions"].append("(dry-run, not saved)")
    else:
        summary["actions"].append("state 加载失败(已改名留档)，本 tick 跳过写回")

    summary["state"] = engine.state_to_dict(state)
    summary["triggers"] = engine.get_triggers(
        state,
        engine.Thresholds(
            notice=float((jiwen_cfg or {}).get("thresholds", {}).get("notice", 0.20)),
            consider=float((jiwen_cfg or {}).get("thresholds", {}).get("consider", 0.35)),
            forced=float((jiwen_cfg or {}).get("thresholds", {}).get("forced", 0.50)),
            pride_block=float((jiwen_cfg or {}).get("thresholds", {}).get("pride_block", 0.5)),
        ),
    )
    summary["description"] = engine.get_state_description(state)
    return summary


# ─── 主入口 ────────────────────────────────────────────────

def _harden_log_perms(fds=(1, 2)):
    """把重定向出来的日志文件收紧到 0600，返回实际收紧了几个 fd。

    launchd 的 StandardOutPath/StandardErrorPath 是 launchd 自己 open() 的，
    进程的 umask 影响不到它 —— 落地就是 0644，而默认落点 /tmp 是所有人可读。
    对 fd 直接 fchmod 不需要改 plist，跑完一次该文件就是 0600。

    只处理**普通文件**：stderr 是终端时 fchmod 会去改 /dev/ttysXXX 的权限（绝不能碰），
    是管道时无意义 —— 两者都跳过。
    已知边界：日志文件是新建的那一次，从 launchd 创建到这里执行之间有个毫秒级窗口是 0644；
    文件一旦存在就一直是 0600（launchd 的 O_APPEND 不重置权限）。
    加固失败（换了属主、只读挂载）一律吞掉：它不该把整个 tick 带崩。
    """
    n = 0
    for fd in fds:
        try:
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode) and (st.st_mode & 0o077) and hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
                n += 1
        except OSError:
            pass
    return n


def main():
    # 先于任何输出做：本进程所有日志都要落在仅属主可读的文件里
    _harden_log_perms()
    p = argparse.ArgumentParser()
    p.add_argument("--bot", help="只跑单个 bot（默认全跑）")
    p.add_argument("--dry-run", action="store_true", help="不写状态文件")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    try:
        jiwen_cfg = load_jiwen_config()
    except Exception as e:
        # 只打异常类型名：PyYAML 的 MarkedYAMLError 会把出错那一行的源码原文带进异常字符串，
        # 而 _global.yml 那一行可能就是 api_key。原来这里没有 try，异常直接冒到解释器，
        # 默认 traceback 把它打进 stderr（世界可读的日志文件）并永久留存。
        print(f"[jiwen.tick] 全局配置读取失败({type(e).__name__})，跳过本次", file=sys.stderr)
        return
    if not jiwen_cfg:
        print("[jiwen.tick] _global.yml.jiwen 未配置，跳过", file=sys.stderr)
        return

    # enabled=false 时不烧 DeepSeek API（仍允许 --dry-run 强跑测试）
    if not jiwen_cfg.get("enabled") and not args.dry_run:
        print("[jiwen.tick] enabled=false，跳过（用 --dry-run 可强跑测试）", file=sys.stderr)
        return

    # DeepSeek 密钥在这里解析一次再往下传，tick_one_bot 内部不读盘（它的测试会直接调它）。
    # 新位置 configs/_secrets.yml 优先，旧写法 jiwen.delta_llm.api_key 兼容；
    # 复制出新 dict，不改 load_jiwen_config() 返回的原对象。
    delta_cfg = jiwen_cfg.get("delta_llm")
    if not isinstance(delta_cfg, dict):
        delta_cfg = {}
    jiwen_cfg = {**jiwen_cfg, "delta_llm": {
        **delta_cfg, "api_key": config_loader.get_deepseek_api_key(delta_cfg)}}

    state_dir = jiwen_cfg.get("state_dir", DEFAULT_STATE_DIR)
    os.makedirs(state_dir, exist_ok=True)

    # F-06：bot 名单来自配置，不写死（旧版名单含已删除的 bot，每个 tick 对它失败一次）。
    # bot 标识取 _bot_id；目录名由各自 bot_cfg.bot_channel_path 提供（tick_one_bot 内 load_bot 取）。
    if args.bot:
        bots = [args.bot]
    else:
        try:
            import bot_features   # 名单经 bots_with：关系进展关着的不在里面（PLAN-bot-features D2、审查 I8）
            bots = [b.get("_bot_id") or b.get("bot_id")
                    for b in bot_features.bots_with("relationship")]
            bots = [b for b in bots if b]
        except Exception as e:
            print(f"[jiwen.tick] 枚举 bot 名单失败({type(e).__name__})，跳过本次", file=sys.stderr)
            return
    print(f"[jiwen.tick] 开始 ts={int(time.time())} bots={bots} state_dir={state_dir}", file=sys.stderr)

    for bot_id in bots:
        try:
            summary = tick_one_bot(bot_id, jiwen_cfg, state_dir, dry_run=args.dry_run)
            print(f"[jiwen.tick] {bot_id}:", file=sys.stderr)
            for action in summary.get("actions", []):
                print(f"    {action}", file=sys.stderr)
            if summary.get("triggers"):
                print(f"    triggers: {summary['triggers']}", file=sys.stderr)
            if args.verbose:
                # 去掉 last_user_msg_meta：这个字段 schema 上允许装对话元信息
                # （今天没有生产代码写它）。-v 是调试开关，但日志文件是长期留存的，
                # 别让"以后有人开始写 meta"变成又一次正文外泄。
                _st = {k: v for k, v in (summary.get("state") or {}).items()
                       if k != "last_user_msg_meta"}
                print(f"    state: {_st}", file=sys.stderr)
                print(f"    desc: {summary.get('description')}", file=sys.stderr)
            if summary.get("error"):
                print(f"    ERROR: {summary['error']}", file=sys.stderr)
        except Exception as e:
            import traceback
            print(f"[jiwen.tick] {bot_id} 异常: {type(e).__name__}", file=sys.stderr)
            # print_tb 而不是 print_exc：print_exc 会把异常的**消息**也打出来，
            # 上一行刚把消息挡掉、下一行又原样放出去等于白挡（单测已实证）。
            # print_tb 只打调用栈（文件/行号/我们自己的源码行），诊断力不减，零数据。
            traceback.print_tb(e.__traceback__, file=sys.stderr)


if __name__ == "__main__":
    main()
