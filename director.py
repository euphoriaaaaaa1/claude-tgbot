#!/usr/bin/env python3
"""群聊"导演"决策大脑（MVP，只做决策，不接生产）。

核心思想：把"谁开口"的决策权从三个 bot 收归一个中央导演，根治去中心化抢答。
- 读群历史（按 message_id 去重）→ 算 heat（确定性）→ 让 DeepSeek 选一个 bot 接话。
- heat 只由真人消息补满，bot 发言只消耗 → bot 互聊最多 MAX_HEAT 轮必停（死循环掐断）。
- 同一回合只选一个 bot（抢答物理上不可能）。

本文件不碰 dispatcher、不写真 bot inbox、不改任何现有文件——只提供 decide()。
接生产（写 inbox + 关 peer_inbound）是后续步骤，需用户批准。
"""
import os
import re
import json
import math
import sys
import time
import random
from datetime import datetime, timezone

from claude_cli import call_claude_json  # 走 DeepSeek 快通道，不烧 Claude 配额
import retry_backoff  # 模型失败的退避片段（I-7）
import chat_history  # session_uuid：确定性定位 worker 会话，别靠 mtime 猜
import bot_features  # 每只 bot 的功能开关（PLAN-bot-features D2）：群聊关着不点名，主动找你聊关着不替它私聊
from adapters import party_state  # 线下聚会状态文件：与 8767 管理台共用同一份
from bots_registry import disabled_ids_safe as _disabled_ids_safe  # T04：停用判定唯一权威
from bots_registry import read_dispatcher_token  # 本机 dispatcher 接口口令


def _stopped_bots(bots):
    """停止集合注入点（INTERFACE §10.2）：只保留被 T04 注册表判停用的那些。"""
    return set(bots) & _disabled_ids_safe()

# 外部触发/额度依赖：全部 optional + 调用点 try/except fail-open，
# 任一模块缺失/异常都不能让 director 常驻进程崩（和 life-context.py 同策略）。
try:
    import config_loader as _config_loader
except Exception:
    _config_loader = None
try:
    import quota as _quota
except Exception:
    _quota = None
try:
    from jiwen import reader as _jiwen_reader
except Exception:
    _jiwen_reader = None
try:
    import db as _db
except Exception:
    _db = None
try:
    import holiday as _holiday
except Exception:
    _holiday = None
try:
    import relations as _relations
except Exception:
    _relations = None

CHAT_ID = os.environ.get("DIRECTOR_CHAT_ID", "")  # 你的 Telegram 群 id（负数），从环境变量配置
MAX_HEAT = 5  # 一条真人消息后，女生之间最多接 MAX_HEAT 轮（你一句我一句），额度耗尽自然停
SCENE_TURNS = 10  # 自主群聊场：没有真人也能聊，一场最多 SCENE_TURNS 轮（1轮=1bot发一次），耗尽自然停
# ── 自主场节流（全部对齐 moments/post.py 的 should_post_moment 手感）──
SCENE_COOLDOWN_MIN = 60     # 两场自主群聊之间最短间隔
SCENES_DAILY_LIMIT = 4      # 自主场每天上限（human 场不计）
SCENE_KIND_COOLDOWN_H = 4   # 同一触发类型 4h 内不重开
NIGHT_SKIP = (1, 8)         # 本地 01:00–08:00 不自主开场（对齐 sleeping 硬闸）
PACE_MIN, PACE_MAX = 15, 45 # bot 轮间随机延迟秒（真人不会秒回，防刷屏）
TURN_TIMEOUT = 120          # 点名后等发言超时（秒），超时累计 fail_count
SCENE_MAX_AGE_MIN = 20      # 场次绝对寿命，防 worker 卡死场次悬挂
ABS_MAX = 15               # transcript 级硬兜底：最后真人消息后 bot 发言 > 此数无条件停
HUMAN_QUIET_MIN = 30        # 真人 30min 内说过话 → 不自主开新场（真人主导优先）
# 缺陷②：群 transcript 看不到私聊。dispatcher 每条真人私聊都写 <MARKER_DIR>/<bot>-<chat>.last-user（int 秒），
# 30min 内私聊过的 bot 视为"忙"：不当开场人、不被续轮点名；可用 bot < 2 不开场/收场。
PRIVATE_BUSY_MIN = 30
MARKER_DIR = os.path.expanduser(
    os.environ.get("DIRECTOR_MARKER_DIR") or "~/.claude/dispatcher/.self-initiate-state")
BUSY_LOG_THROTTLE_SEC = 300  # busy_skip/stopped_skip/resumed_skip 等同键日志限流
RESUME_GRACE_MIN = 30        # 需求⑤：bot 启用后宽限期内仍排除，防补发停用期积压的开场理由（边界不含）

# ── 运行层参数（全部可环境变量覆盖，测试指到假目录，零碰生产）──
# 模型失败的退避与阈值（I-7）：状态文件顶层 llm 片段记连续失败；场内满 3 次收场；当天坏场满 2 场熔断
LLM_BACKOFF_BASE_SEC = 4
LLM_BACKOFF_CAP_SEC = 600
LLM_GIVEUP_FAILS = 4          # 真人消息：连续失败满此数即记账放弃这批消息
SCENE_LLM_FAIL_MAX = 3        # 自主场内连续模型失败满此数 → 收场（不计名额）
SCENE_LLM_ABORT_DAILY_MAX = 2 # 当天因模型失败收场满此数 → 当天不再开自主场
LOOP_BACKOFF_BASE_SEC = 2     # run() 兜底异常后的轮询退避：2、4、8…封顶 300
LOOP_BACKOFF_CAP_SEC = 300

POLL_SEC = 2          # 主循环轮询间隔
DEBOUNCE_SEC = 4      # 新消息后等一拍再决策（人可能连发几条）
LOCK_MIN = 30         # 急停口令锁时长（分钟）
INITIATE_MIN = 45     # 冷场多久后允许导演发起话题（分钟）
HISTORY_N = 12        # 决策看最近几条

CHANNELS_ROOT = os.environ.get(
    "DIRECTOR_CHANNELS_ROOT", os.path.expanduser("~/.claude/channels"))
STATE_DIR = os.environ.get(
    "DIRECTOR_STATE_DIR", os.path.expanduser("~/.claude/dispatcher/.director-state"))
MODE_DIR = os.environ.get(
    "DIRECTOR_MODE_DIR", os.path.expanduser("~/.claude/dispatcher/.director-mode"))

# ── 旧名单兜底，只在没有群配置（configs/_group_chat.yml）时用 ──────────────
# 有群配置时，tick 每轮按群现读名单（_load_roster），把下面五张表整体换成该群的成员（_apply_roster）。
# 公开版给的是占位示例；实际部署用 configs/<bot>.yml 的 display_name / telegram_username /
# dispatcher_port / bot_channel_path，或环境变量覆盖。
BOT_DIR_NAME = {"bot1": "bot1", "bot2": "bot2", "bot3": "bot3"}
BOT_PORTS = {"bot1": "17801", "bot2": "17802", "bot3": "17803"}

# 急停口令（用户在群里说了就锁 LOCK_MIN 分钟）。保守词表，避免"别停"之类误伤。
HARD_STOP_WORDS = ("别聊了", "闭嘴", "安静一下", "都别说", "停停停", "别刷了")

# bot 的 Telegram username → 内部 bot_id。公开版是占位符；有群配置时以 access.json / yml 为准。
BOT_BY_USERNAME = {"@your_bot1_username": "bot1",
                   "@your_bot2_username": "bot2",
                   "@your_bot3_username": "bot3"}
BOTS = {"bot1": "角色一", "bot2": "角色二", "bot3": "角色三"}
# 导演提示词里对每只的介绍（有群配置时取各自 access.json 该群条目的 intro）
INTROS = {bid: "" for bid in BOTS}
_LEGACY_ROSTER = (dict(BOTS), dict(BOT_BY_USERNAME), dict(BOT_PORTS), dict(BOT_DIR_NAME), dict(INTROS))
_roster_applied = False  # 换成过群配置名单 → 群清单被删后要退回旧名单
HUMAN_ID = os.environ.get("DIRECTOR_HUMAN_ID", "")  # 你的 Telegram user_id
HUMAN_NAME = os.environ.get("DIRECTOR_HUMAN_NAME", "主人")  # 内部角色标识；实际称呼由各 bot 人设决定

_GT_DIR = os.environ.get(
    "DIRECTOR_GT_DIR", os.path.expanduser("~/.claude/channels/group_transcripts"))


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _norm_row(o: dict) -> dict:
    """transcript 原始行 → 决策用的规范行 {msg_id, ts(float), text, is_bot, from_username, speaker, reply_to_*}。
    已是规范行（有 msg_id/speaker 且 ts 为数）→ 原样返回，所以可反复调用。"""
    if "msg_id" in o and "speaker" in o and isinstance(o.get("ts"), (int, float)):
        return o
    is_bot = str(o.get("is_bot", "")).lower() == "true"
    uname = o.get("from_username") or ""
    if is_bot:
        speaker = BOT_BY_USERNAME.get(uname, uname or "某bot")
    else:
        speaker = HUMAN_NAME if str(o.get("from_id", "")) == HUMAN_ID else "路人"
    rt_from = o.get("reply_to_from") or ""
    ts = o.get("ts")
    return {
        "msg_id": str(o.get("message_id", "")),
        "ts": float(ts) if isinstance(ts, (int, float)) else _parse_ts(ts or ""),
        "text": (o.get("text") or "").strip(),
        "is_bot": is_bot,
        "from_username": uname,
        "speaker": speaker,
        # 引用回复：这条消息是"回复"谁的哪句话
        "reply_to_text": (o.get("reply_to_text") or "").strip(),
        "reply_to_speaker": BOT_BY_USERNAME.get(rt_from, rt_from) if rt_from else "",
    }


def _as_hist(rows) -> list[dict]:
    """决策入口统一收口：接受 transcript 原始行或规范行（INTERFACE §10.2 最小行形状），一律转规范行。"""
    return [_norm_row(m) for m in (rows or []) if isinstance(m, dict)]


def read_group_history(chat_id: str = CHAT_ID, n: int = 12) -> list[dict]:
    """按 message_id 去重读群历史（transcript 每条被多个 bot 各记一遍），时间正序取最后 n。

    返回 [{msg_id, ts, text, is_bot, from_username, speaker}]，speaker = bot_id 或 'user'。
    """
    path = os.path.join(_GT_DIR, f"{chat_id}.jsonl")
    if not os.path.exists(path):
        return []
    seen: dict[str, dict] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            mid = str(o.get("message_id", ""))
            if not mid or mid in seen:  # 去重：同 message_id 只留第一次
                continue
            seen[mid] = _norm_row(o)
    msgs = [m for m in seen.values() if m["text"]]
    msgs.sort(key=lambda m: (m["ts"], m["msg_id"]))
    return msgs[-n:]


_read_hist = read_group_history  # tick 读群历史的注入点（INTERFACE §10.2）


def compute_heat(history: list[dict]) -> int:
    """heat = MAX_HEAT - (最后一条真人消息之后的 bot 发言数)。
    没有真人消息时，按全部 bot 发言数算（防止纯 bot 历史无限接）。确定性，不调 LLM。"""
    last_human = -1
    for i, m in enumerate(history):
        if not m["is_bot"]:
            last_human = i
    tail = history[last_human + 1:] if last_human >= 0 else history
    bot_after = sum(1 for m in tail if m["is_bot"])
    return MAX_HEAT - bot_after


_CN_COUNT = ("零", "一", "两", "三", "四", "五", "六", "七", "八", "九", "十")


def _cn_count(n: int) -> str:
    """提示词里的人数："三个女生""另外两个女生"。十以上用阿拉伯数字。"""
    return _CN_COUNT[n] if 0 <= n < len(_CN_COUNT) else str(n)


def _one_line(s: str) -> str:
    """把正文里的换行/回车折叠成空格，防路人用换行伪造注入文本的行结构（如批次块标题）。"""
    return re.sub(r"\s*[\r\n]+\s*", " ", s)


def _fmt_msg(m: dict, limit: int = 120) -> str:
    """把一条消息格式化成 '说话人（回复X的「…」）：文本'，带引用上下文。
    limit 为文本截断长度：缺省 120，user 原话用 300。"""
    q = ""
    rt = m.get("reply_to_text")
    if rt:
        rt = _one_line(rt)
        rn = BOTS.get(m.get("reply_to_speaker"), m.get("reply_to_speaker") or "")
        who = f"{rn}的" if rn else ""
        q = f"（回复{who}「{rt[:40]}」）"
    return f"{m['speaker']}{q}：{_one_line(m['text'])[:limit]}"


def _build_prompt(history: list[dict], heat: int, exclude: str | None = None, avail=None) -> str:
    """avail：可被点名的 bot（默认全部）；停止/宽限中的 bot 不出现在 who 枚举里。"""
    who_enum = "或".join(f'"{b}"' for b in (avail or BOTS))
    lines = [_fmt_msg(m) for m in history]
    convo = "\n".join(lines) if lines else "（群里还没人说话）"
    n = _cn_count(len(BOTS))
    excl_line = ""
    if exclude:
        excl_line = (f"⚠️ {BOTS.get(exclude, exclude)}({exclude}) 刚说完，这一轮别再选 ta，"
                     f"让另外{_cn_count(len(BOTS) - 1)}个角色接。\n")
    intros = "".join(f"    - {name}({bid})：{INTROS.get(bid, '')}\n" for bid, name in BOTS.items())
    # 两处举例里的人名按名单位置代入；只有两只时 b 取第 1 只
    names = list(BOTS.values()) or [""]
    a, b = names[1 % len(names)], names[2 if len(names) > 2 else 0]
    return (
        f"你是一个 Telegram 群聊的导演，群里有真人 {HUMAN_NAME} 和{n}个角色。\n"
        f"注意：{HUMAN_NAME} 是内部角色标识，不是称呼；你只负责选人，不写台词、不替角色决定怎么称呼。\n\n"
        "【人物设定——判断该谁接时严格遵守】\n"
        f"· {n}个角色各有各的人设，别混：\n"
        f"{intros}\n"
        f"最近的群聊（时间从早到晚）：\n{convo}\n\n"
        f"现在还剩 {heat} 轮接话额度（角色之间每接一句消耗 1，耗尽自然收尾）。\n"
        f"{excl_line}"
        "你每次只点【一个】角色说话；连续的多轮点名就自然形成'你一句我一句'的群聊。像真人闲聊群那样：\n"
        "- 不是每句话都要有人接；没必要接就让它冷一下（speak=false）。\n"
        f"- **话题连续性最重要**：{HUMAN_NAME} 在接谁的话、回应谁，就让那个人继续接。比如{a}起了个话题、{HUMAN_NAME} 说\"我也想\"，那就该让【{a}】接，别莫名换成别人。谁挑起或正被回应的话题，就选谁。\n"
        f"- **看引用**：消息带「回复X的『…』」表示 {HUMAN_NAME} 在针对那句话说。**按这句话的内容和矛头判断该谁接**——不一定是被引用的那个人本人，谁被点到/谁最该回应就选谁（比如引用了{b}的话夸奖、却在损{a}，那{a}更该跳出来）。\n"
        "- **角色之间也能互相接**：不是只围着真人转。一个角色刚说完，别的角色可以附和 / 接梗 / 打趣 / 拌嘴 / 补一句，让话题在角色之间滚起来——只要还有额度，就大胆让角色你来我往地聊，别每轮都急着收。\n"
        "- 只有话题明显转向、或某人被冷落太久该拉进来时才换人；别老让同一个人连说太多，也别乱插队。\n"
        "- 只剩最后 1~2 轮额度时更倾向收尾、别硬接。\n"
        "- 上面的人物设定只是帮你判断【该谁说】（谁起的话题、谁被冷落、谁适合接这个话题），不是让你替角色说话。\n\n"
        "你的职责【只有一件】：决定这一轮要不要有人说、该谁说。**不要替角色想说什么、更不要替角色写台词**——"
        f"说什么内容、用什么语气、怎么称呼{HUMAN_NAME}，全部由被选中的 bot 自己按人设决定，不归你管。\n"
        f'只输出一个 JSON，不要别的：{{"speak": true或false, "who": {who_enum}, '
        '"party": true或false, "party_reason": "简短原因"}。'
        "speak=false 表示这一轮没人说（冷场或该结束了），此时 who 可省略；"
        "party/party_reason 也可省略，省略按 false。\n"
        "party 是线下聚会判定：只有当这一轮所有真人消息都是同一个主人发的，"
        "且群聊内容明确显示线下见面、一起去某处、或已经在现场一起玩时，才填 true；"
        "预约未来、开玩笑、线上聊天一律 false。party=true 时在 party_reason 写一句简短原因。"
    )


def _ask_llm(prompt: str) -> dict | None:
    """四个决策点共用的模型出口：返回 dict（含空 dict）原样给；抛异常或非 dict → None 并打一行原因。
    日志只含分类取值，不含提示词与模型输出。"""
    if not isinstance(prompt, str) or not prompt:
        print("[director] llm_fail reason=bad_prompt", flush=True)
        return None
    try:
        r = call_claude_json(prompt, timeout=30)
    except Exception as e:
        import deepseek_client
        print(f"[director] llm_fail reason={deepseek_client.classify_error(e)}", flush=True)
        return None
    if not isinstance(r, dict):
        print("[director] llm_fail reason=non_dict", flush=True)
        return None
    return r


def _party_fields(r) -> dict:
    """模型输出里的可选聚会判定（INTERFACE 2.5）：缺失/类型不对按 false；不改 speak/who。"""
    if not isinstance(r, dict):
        return {"party": False, "party_reason": ""}
    reason = r.get("party_reason")
    return {"party": r.get("party") is True,
            "party_reason": reason if isinstance(reason, str) else ""}


def decide(chat_id: str = CHAT_ID, history: list[dict] | None = None,
           scene_budget: int | None = None, exclude=None) -> dict:
    """决定这一轮谁说话。返回 {speak, who?, heat, reason?}。只出 who，不产内容（beat）。
    - scene_budget 给定（自主场续轮）：用它当额度，不看 compute_heat（无真人也能接）。
    - exclude：str=刚说完的 bot（续轮不连说）；集合=停止/宽限中的 bot（需求⑤）。命中 → 确定性改选。
    heat<=0 直接不接（确定性，不调 LLM）——这是死循环掐断点。"""
    excl = _as_set(exclude)
    history = _as_hist(read_group_history(chat_id) if history is None else history)
    if not history:
        return {"speak": False, "heat": MAX_HEAT, "reason": "群历史为空", **_party_fields(None)}
    if scene_budget is not None:
        heat = scene_budget
    else:
        heat = compute_heat(history)
        if heat <= 0:
            return {"speak": False, "heat": heat, "reason": f"接话额度耗尽，等{HUMAN_NAME}再开口",
                    **_party_fields(None)}
    avail = [b for b in BOTS if b not in excl]
    if not avail:
        return {"speak": False, "heat": heat, "reason": "可用bot不足", **_party_fields(None)}
    r = _ask_llm(_build_prompt(history, heat, exclude=exclude if isinstance(exclude, str) else None,
                               avail=avail))
    if r is None:
        return {"speak": False, "heat": heat, "reason": "模型调用失败", "llm_failed": True,
                **_party_fields(None)}
    who = (r.get("who") or "").strip()
    if not r.get("speak") or who not in BOTS:
        return {"speak": False, "heat": heat, "reason": "导演判定这轮不接", "raw": r,
                **_party_fields(r)}
    if who in excl:  # 刚说完的人 / 停止者 → 确定性改选
        who = _pick_other(excl)
    return {"speak": True, "who": who, "heat": heat, **_party_fields(r)}


def decide_scene(history: list[dict], budget: int, exclude: str | None = None, busy=()) -> dict:
    """自主场每轮动态判定：让导演读最近几句，判断①这场闲聊聊完没②谁接③最后一句悬着没。
    返回 {"done": bool, "dangling": bool, "who": str|None}。结束由这个判断驱动，不是纯计数。
    exclude=刚说完的人、busy=正在私聊的人：LLM 选中二者之一 → 确定性改选（可为 None）。"""
    busy = set(busy or ())
    excl_set = busy | ({exclude} if exclude else set())
    history = _as_hist(history)
    lines = [f"{m['speaker']}：{m['text']}" for m in history[-6:]]
    convo = "\n".join(lines) if lines else "（没有对话）"
    excl = f"刚说完的是 {BOTS.get(exclude, exclude)}({exclude})，别再选 ta。\n" if exclude else ""
    excl += "".join(f"别选 {BOTS[b]}({b})（正在忙）。\n" for b in BOTS if b in busy)
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    prompt = (
        f"你是一个 Telegram 群闲聊的导演。群里{_cn_count(len(BOTS))}个角色：{roster}，还有真人 {HUMAN_NAME}。\n"
        f"{HUMAN_NAME} 是内部角色标识，你只判断该谁说，不替角色决定怎么称呼。\n"
        f"最近几句：\n{convo}\n\n{excl}"
        f"这场闲聊还剩约 {budget} 轮接话额度（软上限；聊完就停、别硬凑，也别没聊完就掐）。\n"
        "读一下最后几句，判断三件事，**只输出一个 JSON**，别的都不要：\n"
        f'{{"done": true或false, "dangling": true或false, "who": "{"或".join(BOTS)}"}}\n'
        "- done：这场闲聊是不是自然聊完了——有人收了尾/达成一致/开始道晚安/没新东西可聊/气氛散了，就 true；"
        "还热着、有话头没接、有人被问到，就 false。\n"
        "- dangling：最后一句是不是抛了个问题或话头、还没人接（需要有人回应一下才不突兀）。\n"
        "- who：若还要继续(done=false)，谁最适合接下一句；最后一句是问题就让最该接的人接。done=true 且不 dangling 时 who 可省略。\n"
    )
    r = _ask_llm(prompt)
    if r is None:
        return {"done": False, "dangling": False, "who": None, "llm_failed": True}
    who = (r.get("who") or "").strip()
    if who not in BOTS:
        who = None
    if who in excl_set:
        who = _pick_other(excl_set)
    return {"done": bool(r.get("done")), "dangling": bool(r.get("dangling")), "who": who}


# ══════════════════ 运行层：开关 / 状态 / 注入 / 冷场 / 主循环 ══════════════════

def switch_on(chat_id: str = CHAT_ID) -> bool:
    """导演模式开关：MODE_DIR/<chat_id> 文件存在=开。不存在=导演完全不动（现有行为不变）。"""
    return os.path.exists(os.path.join(MODE_DIR, chat_id))


def _state_path(chat_id: str) -> str:
    return os.path.join(STATE_DIR, f"{chat_id}.json")


# 坏状态文件时的"全部已读"起点：远大于任何真实时间戳，历史消息不会被当新消息重判（F-19）
_ALL_READ_TS = 9e18


def _load_state(chat_id: str) -> dict:
    """读导演状态。文件不存在 → {}；解析失败 → 保留坏文件（改名 .corrupt-<ts>）并以
    "全部已读"为起点 + 告警，绝不把最近历史当新消息重判（F-19）。"""
    path = _state_path(chat_id)
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        st = None
    if isinstance(st, dict):
        return st
    try:
        os.replace(path, f"{path}.corrupt-{int(time.time())}")
    except OSError:
        pass
    print(f"[director] state_corrupt chat={chat_id}，坏文件已留档，以全部已读为起点", file=sys.stderr)
    return {"last_ts": _ALL_READ_TS, "last_mid": "999999999999"}


def _save_state(chat_id: str, st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(chat_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())  # F-19：落盘再 rename
    os.replace(tmp, _state_path(chat_id))


def _is_new(m: dict, st: dict) -> bool:
    """按 (ts, msg_id) 判断是否在上次决策点之后。"""
    key = (st.get("last_ts", 0.0), str(st.get("last_mid", "")))
    return (m["ts"], m["msg_id"]) > key


def _mark_decided(st: dict, m: dict) -> None:
    st["last_ts"], st["last_mid"] = m["ts"], m["msg_id"]


def _ensure_worker_alive(bot: str, chat_id: str) -> None:
    """unified session：每个 bot 只有一个 worker（群+私聊同脑），由该 bot 的 dispatcher
    内嵌 worker-manager 托管。这里 POST /ensure_worker 查活+拉起（原子，替代 tmux）。
    dispatcher 不在 = worker 定义上就是死的；照常写 inbox，dispatcher 起来后 drain 兜住。"""
    if _is_stopped(bot):  # 停用的 bot 一律不拉起
        _log_stopped("ensure", bot)
        return
    if os.environ.get("DIRECTOR_NO_SPAWN"):  # 测试模式：不真拉 worker
        return
    import urllib.request
    try:
        # dispatcher 本机接口需口令；读 <CHANNELS_ROOT>/<bot 目录>/.api-token 并带上。
        # 读不到 → 记一行日志并【拒绝该次调用】（fail-closed），绝不放行无口令请求。
        token = read_dispatcher_token(bot, os.path.join(CHANNELS_ROOT, BOT_DIR_NAME.get(bot, bot)))
        if not token:
            print(f"[director] api_token_missing bot={bot}：未读到 .api-token，"
                  f"拒绝本次调用（fail-closed）", flush=True)
            return
        req = urllib.request.Request(
            f"http://127.0.0.1:{BOT_PORTS.get(bot, '')}/ensure_worker", method="POST",
            headers={"X-Api-Token": token})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[director] ensure_worker({bot}) 失败(dispatcher 未起?): {e}", flush=True)


def _batch_human_ts(user_batch) -> str | None:
    """缺陷③b：批内真人消息最新 ts（能转正有限 float 的才算）→ UTC ISO 整秒；无有效 ts → None。永不抛。
    worker 只认生产者正向打的 human_ts 来算"距你们上次说话"，导演注入的合成消息不带它就不会重置计时。"""
    best = 0.0
    for m in user_batch or ():
        try:
            t = float(m.get("ts"))
        except Exception:  # 缺失/None/非数/非 dict：只丢这一条
            continue
        if math.isfinite(t) and t > best:
            best = t
    if best <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(best), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (OverflowError, OSError, ValueError):  # 超出平台时间范围的坏值
        return None


def inject(bot: str, chat_id: str, history: list[dict],
           initiate: bool = False, prev: dict | None = None,
           context: str | None = None, closing: bool = False,
           low_budget: bool = False, user_batch: list[dict] | None = None) -> str:
    """往被选中 bot 的 inbox 写 [director] 消息（.tmp→rename 原子落地）。
    导演只负责"点你说话"，说什么内容完全由 bot 自己按人设决定——不给方向、不写台词。
    prev 给定（自主场续轮）：明确让 ta 先接上一个发言 bot 的那句话。
    context 给定（自主场开场理由，如 jiwen 情绪/朋友圈）：作为起话头的背景。
    closing=True（收尾轮）：把最后一句/问题接住、自然收尾，别再抛新问题——防对话吊在半句上。
    user_batch 给定（user连发了几条）：明确列出让 bot 一起照顾到，别只回最新一条。
    停止的 bot（需求⑤）→ 不写 inbox、不拉起，返回 ""。"""
    if _is_stopped(bot):
        _log_stopped("inject", bot)
        return ""
    # 批次块只认主人原话：user_batch 可能混入其他真人（_norm_row 记为“路人”），
    # 不能把陌生人的话标成“user 说的、优先照做”（提示注入风险）；路人消息仍留在 history 段。
    owner_batch = [m for m in (user_batch or []) if m.get("speaker") == HUMAN_NAME]
    batch_listed = owner_batch[-5:]  # 批次块实际只渲染最后 5 条
    # 同一条消息不得在 history 段与批次块重复注入：msg_id 优先，缺失时用 (speaker,text,ts) 兜底。
    def _msg_key(m: dict) -> tuple:
        return ("id", str(m["msg_id"])) if m.get("msg_id") else ("row", m.get("speaker"), m.get("text"), m.get("ts"))
    batch_keys = {_msg_key(m) for m in batch_listed}
    convo = "\n".join(_fmt_msg(m) for m in history[-8:] if _msg_key(m) not in batch_keys)
    # 主人说了话（哪怕只有一条）→ 原话单独列出；明确的要求优先照做（治"只回最后一条"）。
    batch_block = ""
    if owner_batch:
        listed = "\n".join(f"  - {_fmt_msg(m, 300)}" for m in batch_listed)
        cond = (f"（{HUMAN_NAME} 连着说了这几句，你可以一起回；其中如果有明确的要求，优先照做。）"
                if len(owner_batch) > 1
                else "（其中如果有明确的要求，优先照做；只是闲聊就正常接。）")
        batch_block = f"\n（以下是 {HUMAN_NAME} 刚才说的话（原话）：）\n{cond}\n{listed}\n"
    # 同群多 bot 在场的最小事实信号（排除自己与已停止的）；单 bot 群不输出。
    present_others = [BOTS[b] for b in BOTS if b != bot and not _is_stopped(b)]
    present_block = f"（群里还有 {'、'.join(present_others)} 在场。）\n" if present_others else ""
    if closing and prev is not None:
        prev_name = BOTS.get(prev.get("speaker"), prev.get("speaker") or "群友")
        prev_text = (prev.get("text") or "")[:80]
        head = (f"[director]（这场聊天要收尾了。{prev_name} 说：「{prev_text}」"
                "你可以说最后一句了。）")
        guide = ""  # 收尾轮不附指导（2026-10-10 用户圈定，原"别再抛新问题"句删）
    elif prev is not None:
        prev_name = BOTS.get(prev.get("speaker"), prev.get("speaker") or "群友")
        prev_text = (prev.get("text") or "")[:80]
        head = (f"[director]（群里正在聊天。{prev_name} 说：「{prev_text}」"
                "你可以跟她聊天。）")
        guide = f"你可以接着聊，别只回 {HUMAN_NAME}。"
        if low_budget:  # 临近收尾：往回收，别再起新话头
            guide += "（这场快聊完了，别再起新话题。）"
    elif context:
        head = f"[director]（{context}。你可以跟她们聊起这个。）"
        guide = ""
    elif initiate:
        head = "[director]（群里安静了一阵，你可以说点什么。）"
        guide = ""
    else:
        head = "[director]（群里正在聊天，你可以接话。）"
        guide = ""
    # 关系摘要块：读本 bot 对在场同伴的主观笔记 + 当前心情，拼一段注入（fail-open，任何异常都不阻断群聊）
    rel_block = ""
    if _relations is not None:
        try:
            present = [s for s in {m.get("speaker") for m in history}
                       if s in BOT_DIR_NAME and s != bot]
            mood = None
            if _jiwen_reader:
                try:
                    mood = _jiwen_reader.read(bot, HUMAN_ID, _global_cfg())
                except Exception:
                    mood = None
            rel_block = _relations.inject_snippet(bot, present, mood=mood)
        except Exception:
            rel_block = ""
    convo_line = convo or ("（这条之前没有别的对话）" if batch_block else "（还没人说话）")
    text = (
        f"{head}\n"
        f"{present_block}"
        f"群里最近的对话：\n{convo_line}\n"
        f"{batch_block}\n"
        f"{(rel_block + chr(10)) if rel_block else ''}"
        f"（上面是刚才的聊天，你可以按自己的来。）{guide}\n"
        "（按你平时的语气说话；不要旁白和动作描写，不要频繁发emoji。）"
    )
    # unified inbox：写 <channel>/inbox（不再 per-chat），payload 带 chat_id + scene。
    inbox = os.path.join(CHANNELS_ROOT, BOT_DIR_NAME[bot], "inbox")
    os.makedirs(inbox, exist_ok=True)
    ms = int(time.time() * 1000)
    payload = {
        "text": text, "chat_id": chat_id, "from_id": chat_id,
        "from_username": "director", "sender_username": "director",
        "chat_type": "group", "scene": "group", "is_bot_sender": False,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message_id": str(ms),
    }
    human_ts = _batch_human_ts(user_batch)
    if human_ts:  # 无真人批 → 无此键（不是 null）
        payload["human_ts"] = human_ts
    path = os.path.join(inbox, f"director-{ms}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    _ensure_worker_alive(bot, chat_id)
    return path


def decide_initiate(history: list[dict], exclude=()) -> dict:
    """冷场发起：让 DeepSeek 挑一个 bot 起话题。只发起一条，不给 bot 互聊充能——
    user不回，就没有后续（防止无人时 bot 自嗨烧配额）。
    exclude=正在私聊的 bot：提示词里点名别选；LLM 仍选中 → 确定性改选，全排除 → speak=False。"""
    exclude = set(exclude or ())
    busy_line = "".join(f"别选 {BOTS[b]}({b})（正在忙）。\n" for b in BOTS if b in exclude)
    history = _as_hist(history)
    lines = [f"{m['speaker']}：{m['text'][:100]}" for m in history[-6:]]
    convo = "\n".join(lines) if lines else "（群里很久没人说话）"
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    prompt = (
        f"Telegram 群里有真人 {HUMAN_NAME} 和{_cn_count(len(BOTS))}个角色（{roster}），群里已经安静了一阵。\n"
        f"之前的对话：\n{convo}\n\n"
        "你是导演，只决定要不要让某个女生自然地起个话头、以及选谁——**不要替她想说什么**，"
        "起什么话头由她自己按人设定。不必每次都发起，可以继续安静。\n"
        f"{busy_line}"
        f'只输出 JSON：{{"speak": true或false, "who": "{"/".join(BOTS)}"}}'
    )
    r = _ask_llm(prompt)
    if r is None:
        return {"speak": False, "reason": "模型调用失败", "llm_failed": True}
    who = (r.get("who") or "").strip()
    if not r.get("speak") or who not in BOTS:
        return {"speak": False, "reason": "导演选择继续安静"}
    if who in exclude:
        who = _pick_other(exclude)
        if who is None:
            return {"speak": False, "reason": "可用bot不足"}
    return {"speak": True, "who": who}


# ══════════════════ 自主群聊 scene：额度/触发/节流/记账 ══════════════════

def _local_hour(now: float) -> int:
    return datetime.fromtimestamp(now).hour


def _today_str(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _global_cfg() -> dict:
    if not _config_loader:
        return {}
    try:
        return _config_loader.load_global() or {}
    except Exception:
        return {}


def _quota_ok() -> bool:
    """quota 允许自主开场？外部不可用/异常 → fail-open(True)。测试可 monkeypatch。"""
    if not _quota:
        return True
    try:
        return _quota.check_quota(_global_cfg()) == "ok"
    except Exception:
        return True


def _record_scene_turn() -> None:
    if not _quota:
        return
    try:
        _quota.record_call("scene_turn", getattr(_quota, "SCENE_TURN_WEIGHT", 2))
    except Exception:
        pass


def _last_human_ts(hist: list[dict]) -> float:
    return max((m["ts"] for m in hist if not m["is_bot"]), default=0.0)


def _bot_after_human_count(hist: list[dict]) -> int:
    """最后一条真人消息之后的 bot 发言数（ABS_MAX 硬兜底用，不依赖 state）。"""
    last_human = -1
    for i, m in enumerate(hist):
        if not m["is_bot"]:
            last_human = i
    tail = hist[last_human + 1:] if last_human >= 0 else hist
    return sum(1 for m in tail if m["is_bot"])


# ══════════════════ 缺陷②：私聊忙碌感知（读 dispatcher 的 .last-user marker）══════════════════
_busy_log_at: dict = {}  # (where, bot) → 上次打印时刻；测试可清空


def _throttled(key: tuple, now: float) -> bool:
    """同键 BUSY_LOG_THROTTLE_SEC 内已打过 → True（本次别打）；否则记下本次并返回 False。"""
    last = _busy_log_at.get(key)
    if last is not None and now - last < BUSY_LOG_THROTTLE_SEC:
        return True
    _busy_log_at[key] = now
    return False


def _log_busy(where: str, bot: str, since_min: float, now: float) -> bool:
    if _throttled((where, bot), now):
        return False
    print(f"[director] busy_skip where={where} bot={bot} since_min={int(since_min)}", flush=True)
    return True


def _as_set(x) -> set:
    """排除参数统一转集合：str → {str}（兼容旧的"刚说完的人"单值），dict 取键，None/空 → 空集。"""
    return {x} if isinstance(x, str) else set(x or ())


def _pick_other(exclude: set) -> str | None:
    """BOTS 顺序里第一个不在 exclude 的 bot；全排除 → None。"""
    return next((b for b in BOTS if b not in exclude), None)


def _busy_bots(now: float) -> dict:
    """{bot: since_min}：各 bot 所有私聊 marker 的最大 ts 落在 [now-30min, now+60s] → 忙。
    fail-open：缺文件/坏内容/远未来/过旧 → 不忙（= 改动前行为）；永不抛。"""
    import glob
    busy, found_any = {}, False
    for bot in BOTS:
        best = None
        for path in glob.glob(os.path.join(MARKER_DIR, f"{glob.escape(bot)}-*.last-user")):
            found_any = True
            try:
                with open(path, encoding="utf-8") as f:
                    ts = int(f.read().strip())
            except Exception as e:  # 非整数/空/不可读：只忽略这个文件
                if not _throttled(("marker_bad", bot), now):
                    print(f"[director] busy_marker_bad bot={bot} err={type(e).__name__}", flush=True)
                continue
            best = ts if best is None else max(best, ts)
        if best is not None and -60 <= now - best < PRIVATE_BUSY_MIN * 60:
            busy[bot] = max(0.0, (now - best) / 60)
    if not found_any and not _throttled(("marker_missing", ""), now):
        # ② 静默失效必须可观测：三个 bot 一个 marker 都没有，多半是目录/命名漂移
        print(f"[director] busy_marker_missing dir={MARKER_DIR}", flush=True)
    return busy


def _busy_reason(excl: set, mins: dict) -> str:
    """可用bot<2 的原因文案。mins 里值为 None 的是停止/宽限中的 bot（tick 传入），其余算私聊忙碌；两段各按 BOTS 顺序。"""
    stop = [b for b in BOTS if b in excl and b in mins and mins[b] is None]
    busy = [b for b in BOTS if b in excl and b not in stop]
    parts = ([f"私聊忙碌({','.join(busy)})"] if busy else []) + ([f"已停止({','.join(stop)})"] if stop else [])
    return "或".join(parts) + "可用bot<2"


def _log_excluded(where: str, bot: str, mins: dict, now: float) -> bool:
    """忙者打 busy_skip；停止/宽限中的 bot（mins 值为 None）已由 tick 打 stopped/resumed 行，不重复。"""
    m = mins.get(bot, 0)
    return m is not None and _log_busy(where, bot, m, now)


# ══════════════════ 需求⑤：手动停止的 bot 不被导演拉起、不被替写消息 ══════════════════
_prev_stopped = None     # 上一 tick 的停止集合；None=首个 tick，不判定"启用"转换
_resumed_at: dict = {}   # bot → 检测到启用的时刻（启用宽限用）


def _is_stopped(bot: str) -> bool:
    return bot in _stopped_bots([bot])


def _log_stopped(where: str, bot: str, now: float | None = None) -> None:
    if not _throttled((where, bot), time.time() if now is None else now):
        print(f"[director] stopped_skip where={where} bot={bot}", flush=True)


def _stop_state(now: float) -> tuple[set, set]:
    """每 tick 一次：(stopped, resumed)。停止→启用的 bot 在 RESUME_GRACE_MIN 内仍排除（同停止待遇）。"""
    global _prev_stopped
    # 逐群处理时各群名单不同：把上一 tick 停着的也一起查，免得换群时把"不在本群名单"误判成"刚启用"
    stopped = set(_stopped_bots(set(BOTS) | (_prev_stopped or set())))
    if _prev_stopped is not None:
        for b in _prev_stopped - stopped:
            _resumed_at[b] = now
    for b in stopped:
        _resumed_at.pop(b, None)  # 再次停用 → 宽限记录作废
        _log_stopped("tick", b, now)
    _prev_stopped = stopped
    for b, t in list(_resumed_at.items()):
        if not now - t < RESUME_GRACE_MIN * 60:  # 边界 1800 秒不含
            del _resumed_at[b]
    for b, t in _resumed_at.items():
        if not _throttled(("resumed", b), now):
            # ponytail: 刚启用那一 tick 报 since_min=1 而非 0（验收 test_08 锁定）；其余按整分钟向下取
            print(f"[director] resumed_skip bot={b} since_min={max(1, int((now - t) // 60))}", flush=True)
    return stopped, set(_resumed_at)


def _scene_gates(st: dict, hist: list[dict], now: float, kind: str, busy=()) -> tuple[bool, str]:
    """自主开场七道闸，代码顺序即契约：夜间 → 真人30min → 忙碌 → 距上场冷却 → 同类冷却 → 日限 → 配额（永远最后）。
    返回 (go, reason)。human 触发不过闸。"""
    mins = dict(busy) if isinstance(busy, dict) else {}  # 只供日志取 since_min
    busy = set(busy or ())
    hist = _as_hist(hist)
    lo, hi = NIGHT_SKIP
    if lo <= _local_hour(now) < hi:
        return (False, f"夜间{lo}-{hi}点不打扰")
    lh = _last_human_ts(hist)
    if lh and (now - lh) < HUMAN_QUIET_MIN * 60:
        return (False, "真人30min内说过话(真人主导优先)")
    if len(BOTS) - len(busy & set(BOTS)) < 2:
        for b in BOTS:
            if b in busy:
                _log_excluded("scene_gate", b, mins, now)
        return (False, _busy_reason(busy, mins))
    if now - st.get("last_scene_end_ts", 0) < SCENE_COOLDOWN_MIN * 60:
        return (False, f"距上场不足{SCENE_COOLDOWN_MIN}min")
    kind_last = (st.get("scene_kind_last_ts") or {}).get(kind, 0)
    if now - kind_last < SCENE_KIND_COOLDOWN_H * 3600:
        return (False, f"同类型{kind} 4h内已开过")
    cnt = st.get("scenes_today") or {}
    if cnt.get("date") == _today_str(now) and cnt.get("count", 0) >= SCENES_DAILY_LIMIT:
        return (False, f"今日自主场已达上限{SCENES_DAILY_LIMIT}")
    # 熔断（拍板 7）：当天因模型连续失败收场满 2 场，当天不再开自主场，次日自动恢复
    if (cnt.get("date") == _today_str(now)
            and _int_or_zero(cnt.get("llm_aborts")) >= SCENE_LLM_ABORT_DAILY_MAX):
        return (False, f"今日模型失败收场已达{SCENE_LLM_ABORT_DAILY_MAX}场")
    if not _quota_ok():
        return (False, "quota不足")
    return (True, "ok")


def _list_moments(since_ts: int, since_id):
    """读朋友圈（F-15）：visibility 下推到 SQL、(ts,id) 严格游标。
    老替身只认 (limit, since_ts) 时回退旧签名并自行按 public 过滤。"""
    try:
        return _db.list_moments(limit=5, since_ts=int(since_ts),
                                since_id=since_id, visibility="public") or []
    except TypeError:
        ms = _db.list_moments(limit=5, since_ts=int(since_ts)) or []
        return [m for m in ms if (m.get("visibility") or "public") == "public"]


def _holiday_name(day):
    """取当天的具名节日名（str）或 None。优先 holiday.named_holiday（生产）；
    替身只提供 is_holiday（返回名字或 None）时回退到它（F-16）。"""
    fn = getattr(_holiday, "named_holiday", None)
    if callable(fn):
        return fn(day)
    return _holiday.is_holiday(day)


def _check_triggers(st: dict, hist: list[dict], now: float, busy=()) -> dict | None:
    """按优先级找一个自主开场理由。返回 {kind, opener, context} 或 None。
    各触发独立评估：jiwen 候选忙 → 下探朋友圈/特殊日/冷场（不整轮 None，F-14）。
    jiwen/朋友圈/特殊日 全 try/except fail-open；最后兜底冷场。busy 里的 bot 不当开场人。"""
    mins = dict(busy) if isinstance(busy, dict) else {}  # 只供日志取 since_min
    busy = set(busy or ())
    hist = _as_hist(hist)
    newest_ts = hist[-1]["ts"] if hist else 0

    # 1) jiwen 情绪越阈：合格候选里轮换（F-14），同一 bot 不连着开同一情绪的场
    jiwen_block_cold = False  # 有人有情绪（仅因私聊忙没排上）→ 群不算"冷场"，压住冷场兜底
    if _jiwen_reader:
        qualified = []
        for bot in BOTS:
            try:
                info = _jiwen_reader.read(bot, HUMAN_ID, _global_cfg())
            except Exception:
                info = None
            if info and (info.get("forced") or info.get("description")):
                if bot in busy:  # 想开场的人正在私聊：不换人代开，但继续下探其它触发
                    if mins.get(bot, 0) is not None:  # 停止/宽限中的人（值 None）只跳过
                        _log_busy("jiwen_opener", bot, mins.get(bot, 0), now)
                        jiwen_block_cold = True
                    continue
                qualified.append((bot, info))
        if qualified:
            last = st.get("jiwen_last_opener")
            bot, info = next((q for q in qualified if q[0] != last), qualified[0])
            st["jiwen_last_opener"] = bot  # 轮换：下次换另一个合格 bot
            return {"kind": "jiwen", "opener": bot,
                    "context": f"你现在{info.get('description') or '心里有点动静'}"}

    # 2) 朋友圈事件：上次检查点后有人发了新圈 → 别的 bot 起头搭话
    if _db:
        try:
            since_ts = st.get("last_moment_seen_ts", 0) or 0
            ms = _list_moments(int(since_ts), st.get("last_moment_seen_id"))
            if ms:
                m0 = ms[0]
                poster = m0.get("bot_id", "")
                opener = next((b for b in BOTS if b != poster), None)
                try:
                    seen_ts = int(m0.get("ts", now))
                except (TypeError, ValueError):
                    seen_ts = int(now)
                st["last_moment_seen_ts"] = max(seen_ts, int(since_ts))  # 游标：ts
                seen_id = m0.get("id")
                if seen_id is not None:  # 游标：id（P2 键名 last_moment_seen_id）
                    st["last_moment_seen_id"] = seen_id
                if opener in busy:
                    _log_excluded("moment_opener", opener, mins, now)
                    opener = _pick_other(busy | {poster})
                txt = (m0.get("text") or "")[:60]
                pname = BOTS.get(poster, poster or "群友")
                if opener:  # 没人可用 → 本触发作废，继续下探（检查点照样推进，免得每 tick 重触发）
                    return {"kind": "moment", "opener": opener,
                            "context": f"你刷到 {pname} 刚发的朋友圈：「{txt}」"}
        except Exception:
            pass

    # 3) 特殊日期：只认具名节日（_holiday_name 返回节日名/None），按日期记"今天已开过"（F-16）
    if _holiday:
        try:
            from datetime import date as _date
            name = _holiday_name(_date.fromtimestamp(now))
            if isinstance(name, str) and name.strip() and st.get("special_date_day") != _today_str(now):
                opener = next(iter(BOTS), None)  # 名单第一只
                if opener in busy:
                    _log_excluded("special_opener", opener, mins, now)
                    opener = _pick_other(busy)
                if opener:
                    st["special_date_day"] = _today_str(now)
                    return {"kind": "special_date", "opener": opener,
                            "context": f"今天是{name.strip()}"}
        except Exception:
            pass

    # 4) 冷场兜底：距最后一条消息与上次发起都超过 INITIATE_MIN
    if jiwen_block_cold:  # 有情绪但人忙：群不算冷场，不给冷场兜底（其它触发上面已评估）
        return None
    gap = INITIATE_MIN * 60
    if now - newest_ts > gap and now - st.get("last_initiate_ts", 0) > gap:
        return {"kind": "cold_gap", "opener": None, "context": ""}
    return None


def _open_scene(st: dict, now: float, kind: str, opener: str) -> None:
    st["scene"] = {"active": True, "kind": kind, "budget": SCENE_TURNS,
                   "last_speaker": opener, "opened_ts": now,
                   "next_turn_after": 0.0, "fail_count": 0}


def _int_or_zero(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _mark_pending(scene: dict, bot: str, path: str, now: float) -> None:
    """记下本轮回点名的 bot 与 inbox 文件，供超时无人应时记账/清理（F-17）。"""
    if bot and path:
        scene["pending_bot"] = bot
        scene["pending_inbox"] = path
        scene["pending_since"] = now


def _clear_pending(scene: dict) -> None:
    for k in ("pending_bot", "pending_inbox", "pending_since"):
        scene.pop(k, None)


def _drop_inbox(path: str | None) -> None:
    """删掉未被消费的点名 inbox 文件（超时清理，F-17）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _close_scene(st: dict, now: float, llm_abort: bool = False,
                 kind_override: str | None = None) -> None:
    """收场。llm_abort=True（模型连续失败）：不占当天名额，只记 llm_aborts；冷却照常生效（拍板 7）。
    kind_override（F-18：真人插话打断记 human）：不占日限，但更新场间与同类冷却时间戳。"""
    sc = st.get("scene") or {}
    kind = kind_override or sc.get("kind", "cold_gap")
    sc["active"] = False
    _clear_pending(sc)
    st["scene"] = sc
    st["last_scene_end_ts"] = now
    kl = st.get("scene_kind_last_ts") or {}
    kl[kind] = sc.get("opened_ts", now)
    st["scene_kind_last_ts"] = kl
    day = _today_str(now)
    cnt = st.get("scenes_today") or {}
    if cnt.get("date") != day:
        cnt = {"date": day, "count": 0, "llm_aborts": 0}
    cnt["count"] = _int_or_zero(cnt.get("count"))
    cnt["llm_aborts"] = _int_or_zero(cnt.get("llm_aborts"))
    if kind != "human":
        if llm_abort:
            cnt["llm_aborts"] += 1
        else:
            cnt["count"] += 1
    st["scenes_today"] = cnt


def _prewarm_all_workers(chat_id: str) -> None:
    """开场时把三个 bot 的 worker 都预热，避免续轮冷启动、被点无人应。"""
    for b in BOTS:
        if _is_stopped(b):
            _log_stopped("prewarm", b)
            continue
        _ensure_worker_alive(b, chat_id)


# ══════════════════ 群聊信号 → 主动私聊（DM）══════════════════
# 真人在群里说了值得单独关心的话（情绪低落/身体不适/被冷落），让最合适的一个 bot
# 主动切私聊关心他。三层闸门(夜间/冷却/日限)+高门槛 LLM 判定+bot 自己有沉默权。
DM_COOLDOWN_MIN = 120    # 两次主动 DM 最短间隔（全局，物理防轰炸）
DM_DAILY_LIMIT = 2       # 每天最多主动 DM 次数
DM_NIGHT_SKIP = (1, 8)   # 夜间[1,8)点不主动私聊
PARTY_OPEN_COOLDOWN_MS = 30 * 60 * 1000  # 导演两次自动开聚会的最短间隔（按群，存 director state）


def _dm_state(st: dict, now: float) -> dict:
    """从 director state 里取/重置当天的 DM 记账（单进程写，无并发）。"""
    dm = st.get("dm") or {}
    if dm.get("day") != _today_str(now):
        dm = {"day": _today_str(now), "count": 0, "last_ts": 0.0, "last_bot": ""}
    st["dm"] = dm
    return dm


def _is_owner_msg(m: dict) -> bool:
    """主人消息判定：规范行看 speaker，原始行看 from_id。
    两者都缺或都不匹配 → False（fail-closed）；生产消息经 _as_hist 后必有 speaker。"""
    return m.get("speaker") == HUMAN_NAME or str(m.get("from_id", "")) == HUMAN_ID


# ══════════════════ 线下聚会（PLAN-party-camera CBL-2）══════════════════
# 只有本轮 new_msgs 里所有真人消息都是 owner（至少一条）、模型 party=true、名单 ≥2、该群没有有效状态、
# 且过了 30 分钟冷却，导演才写 source=director 的聚会状态；从不自动关闭。
# owner 的唯一来源是 access.json.allowFrom[0]（INTERFACE 2.5）。

def _party_note(reason) -> str:
    """party_reason → note：换行折叠成空格，截断到 80 字（INTERFACE 2.5）。"""
    if not isinstance(reason, str):
        return ""
    return reason.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()[:80]


def _party_owner(members):
    """本轮名单第一只的频道目录里 access.json 的 allowFrom[0]；读不出/缺失回 None（fail-closed）。
    没有群清单时退回旧名单第一只的频道目录。"""
    if members:
        rel = members[0].get("dir")
        if not isinstance(rel, str) or not rel:
            return None
        target = os.path.join(CHANNELS_ROOT, rel, "access.json")
    else:
        bot = next(iter(BOTS), None)
        if bot is None:
            return None
        target = os.path.join(CHANNELS_ROOT, BOT_DIR_NAME.get(bot, bot), "access.json")
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    allow = data.get("allowFrom") if isinstance(data, dict) else None
    if isinstance(allow, list) and allow and isinstance(allow[0], str) and allow[0]:
        return allow[0]
    return None


def _maybe_open_party(chat_id: str, new_msgs: list[dict], r: dict, now: float,
                      members, st: dict) -> bool:
    """导演自动开聚会：全部闸门通过才写文件；写失败只记日志。返回本轮是否新开了状态。
    条件 1（安全审计）：本轮所有真人消息都必须是 owner（至少一条）——路人先发诱导内容、
    owner 随后只说一句，不能把这一轮当作 owner 的聚会意图。"""
    if r.get("party") is not True:
        return False
    humans = [m for m in new_msgs if not m.get("is_bot")]
    if not humans or any(not _is_owner_msg(m) for m in humans):
        return False
    if _party_owner(members) != HUMAN_ID:
        if not _throttled(("owner_mismatch", chat_id), now):
            print(f"[director] owner_mismatch chat={chat_id}", flush=True)
        return False
    now_ms = int(now * 1000)
    if now_ms - _int_or_zero(st.get("party_last_open_ms")) < PARTY_OPEN_COOLDOWN_MS:
        return False
    if party_state.is_active(chat_id):
        return False
    try:
        party_state.open_party(chat_id, "director", exclude=[], note=_party_note(r.get("party_reason")),
                               now_ms_value=now_ms)
    except (party_state.StateWriteError, ValueError):
        print(f"[director] party_write_failed chat={chat_id}", flush=True)
        return False
    st["party_last_open_ms"] = now_ms
    print(f"[director] party_open chat={chat_id} source=director", flush=True)
    return True


def decide_dm(human_msgs: list[dict], hist: list[dict], exclude=()) -> dict:
    """独立 DeepSeek 判定：真人这几句里有没有'值得某个 bot 单独私聊关心'的强信号。
    高门槛——只有真闺蜜会忍不住私戳的程度才 true。返回 {dm, who?, reason?}。
    exclude=停止/宽限中的 bot：不进 who 枚举，LLM 仍选中 → 确定性改选。"""
    # 只认主人原话：路人（其他真人）的发言不能当“user 说的”送进 DM 判定。
    human_msgs = [m for m in human_msgs if _is_owner_msg(m)]
    if not human_msgs:
        return {"dm": False}
    exclude = _as_set(exclude)
    avail = [b for b in BOTS if b not in exclude]
    if not avail:
        return {"dm": False, "reason": "可用bot不足"}
    lines = "\n".join(f"{HUMAN_NAME}：{m['text']}" for m in human_msgs[-5:])
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    who_enum = "或".join(f'"{b}"' for b in avail)
    prompt = (
        f"群里角色：{roster}。真人 {HUMAN_NAME} 刚在群里说：\n{lines}\n\n"
        "判断：这里有没有'值得某个角色单独私聊他关心一下'的强信号？"
        "**门槛要高**——只有像真闺蜜会忍不住私戳的程度才算："
        "明确的情绪低落/难过、身体不适、遇到重要的事却在群里没被接住。"
        "日常闲聊、玩笑、普通提问、寒暄一律 false。\n"
        f"若 true，从 {who_enum} 里选**最适合私聊他的那一个**（和他关系/这个话题最搭的）。\n"
        '只输出 JSON：{"dm": true或false, "who": "…", "reason": "一句话为什么"}'
    )
    r = _ask_llm(prompt)
    if r is None:
        return {"dm": False, "llm_failed": True}
    who = (r.get("who") or "").strip()
    if not r.get("dm") or who not in BOTS:
        return {"dm": False}
    if who in exclude:
        who = _pick_other(exclude)  # avail 非空 → 必有人
    return {"dm": True, "who": who, "reason": (r.get("reason") or "")[:80]}


def inject_dm(bot: str, snippet: str, now: float) -> str:
    """给 bot 的私聊 inbox 写一条 [group-dm-nudge]。**先拉活 worker 再写**（防冷启动 race：
    worker 启动期 drainInbox 会静默吞掉先写的 inbox，self-initiate 踩过 23h 零送达）。"""
    if _is_stopped(bot):  # 需求⑤：停止的 bot 不拉起、不替它写私聊
        _log_stopped("dm", bot, now)
        return ""
    _ensure_worker_alive(bot, HUMAN_ID)  # 必须先于写 inbox
    inbox = os.path.join(CHANNELS_ROOT, BOT_DIR_NAME.get(bot, bot), "inbox")
    os.makedirs(inbox, exist_ok=True)
    ms = int(now * 1000)
    text = (f"[group-dm-nudge]（你看到 {HUMAN_NAME} 在群里说：「{snippet}」"
            "你可以私聊他。）")
    payload = {
        "text": text, "chat_id": HUMAN_ID, "from_id": HUMAN_ID,
        "from_username": "director", "sender_username": "director",
        "chat_type": "private", "scene": "private", "is_bot_sender": False,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message_id": str(ms),
    }
    path = os.path.join(inbox, f"dm-nudge-{ms}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def maybe_dm(st: dict, new_msgs: list[dict], hist: list[dict], now: float, exclude=()) -> dict | None:
    """真人在群里说话后，判断要不要让某个 bot 主动私聊他。闸门全过才调 LLM。
    改动 st['dm']，调用方负责 _save_state。"""
    # 兼容规范行（speaker）与原始行（from_id）：只认主人；路人与其他真人仍排除。
    humans = [m for m in new_msgs if not m["is_bot"] and _is_owner_msg(m)]
    if not humans:
        return None
    dm = _dm_state(st, now)
    lo, hi = DM_NIGHT_SKIP
    if lo <= _local_hour(now) < hi:
        return None                                    # 夜间不打扰
    if dm["count"] >= DM_DAILY_LIMIT:
        return None                                    # 今日额度用尽
    if now - dm.get("last_ts", 0) < DM_COOLDOWN_MIN * 60:
        return None                                    # 冷却期内，连 LLM 都不调
    if not _quota_ok():
        return None
    # 主动找你聊关着的 bot 不替它私聊（PLAN-bot-features D2）：并进排除集，模型只在开着的里面挑，全关时不调模型。
    # 开关读的是导演要往里写私聊的那个频道目录（与 inject_dm 同一个），读法同 bot_features.read（读不出按关）。
    off = {b for b in BOTS if not _feature_on(b, "proactive")}
    r = decide_dm(humans, hist, exclude=_as_set(exclude) | off)
    if r.get("llm_failed"):  # 失败视为"不私聊"，只记退避，不连累已得出的接话结论
        _llm_record_failure(st, now, "decide_dm")
        return None
    if not r.get("dm"):
        return None
    who = r["who"]
    if who == dm.get("last_bot") and dm["count"] > 0:
        return None                                    # 别老是同一个 bot 连着私聊
    snippet = (humans[-1]["text"] or "")[:60]
    path = inject_dm(who, snippet, now)
    if not path:  # 入口硬闸拦下（停止）：不记账
        return None
    dm.update({"count": dm["count"] + 1, "last_ts": now, "last_bot": who})
    st["dm"] = dm
    return {"who": who, "reason": r.get("reason"), "inbox": path}


def _llm_fails(st: dict) -> int:
    llm = st.get("llm")
    return _int_or_zero(llm.get("fail_count")) if isinstance(llm, dict) else 0


def _llm_record_failure(st: dict, now: float, where: str) -> None:
    st["llm"] = retry_backoff.on_failure(st.get("llm"), now, base=LLM_BACKOFF_BASE_SEC,
                                         cap=LLM_BACKOFF_CAP_SEC, reason=where)


def _port_str(v) -> str | None:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).isdigit():
        return None
    return str(int(v)) if 0 < int(v) < 65536 else None


def _roster_skip(bot: str, reason: str, now: float) -> None:
    if not _throttled(("roster_skip", bot, reason), now):
        print(f"[director] roster_skip bot={bot} reason={reason}", flush=True)


def _load_roster(chat_id: str, now: float):
    """有群清单时这个群这一轮的成员 [{bot, name, intro, port, dir, username}]（按 bot_id 排序）；
    没有群清单 → None（旧名单兜底）；群号不在清单 → []。群清单坏了 → GroupConfigError（run 记日志退避）。
    成员 = 群聊开关开着、access.json 的 groups 里有这个群号、且 intro 非空的 bot。
    频道目录必须在 CHANNELS_ROOT 之内（导演只往自己的频道根里写）、dispatcher 端口必须合法。"""
    from adapters import group_config
    groups = group_config.load()
    if groups is None:
        return None
    if group_config.find(groups, chat_id) is None or _config_loader is None:
        return []
    import bots_registry
    root = os.path.realpath(CHANNELS_ROOT)
    ports = {b["id"]: b["port"] for b in bots_registry.bots()}
    out = []
    for cfg in bot_features.bots_with("groups"):   # 群聊关着的不算成员
        bot = cfg["_bot_id"]
        channel = bot_features.channel_dir(bot, cfg)   # 全仓唯一的取法，与管理台写的是同一目录
        if not channel:
            _roster_skip(bot, "channel_dir_missing", now)
            continue
        real = os.path.realpath(channel)
        try:
            with open(os.path.join(real, "access.json"), encoding="utf-8") as f:
                access = json.load(f)
        except Exception:
            _roster_skip(bot, "access_unreadable", now)
            continue
        entry = (access.get("groups") if isinstance(access, dict) else None) or {}
        entry = entry.get(str(chat_id)) if isinstance(entry, dict) else None
        intro = entry.get("intro") if isinstance(entry, dict) else None
        if not isinstance(intro, str) or not intro.strip():
            continue  # 不在这个群，或在群里但没写介绍 → 不算成员
        if real != root and not real.startswith(root + os.sep):
            _roster_skip(bot, "dir_outside_channels_root", now)
            continue
        port = _port_str(cfg.get("dispatcher_port")) or _port_str(ports.get(bot))
        if port is None:
            _roster_skip(bot, "bad_dispatcher_port", now)
            continue
        name = cfg.get("display_name")
        user = cfg.get("telegram_username")
        out.append({"bot": bot, "name": name if isinstance(name, str) and name else bot,
                    "intro": intro.strip(), "port": port,
                    "dir": os.path.relpath(real, root),
                    "username": user if isinstance(user, str) and user else None})
    return sorted(out, key=lambda m: m["bot"])


def _apply_roster(members) -> None:
    """把五张名单表整体换成这个群的成员。逐群处理时每个群的 tick 开头都重换一次。"""
    global BOTS, BOT_BY_USERNAME, BOT_PORTS, BOT_DIR_NAME, INTROS, _roster_applied
    BOTS = {m["bot"]: m["name"] for m in members}
    BOT_BY_USERNAME = {m["username"]: m["bot"] for m in members if m["username"]}
    BOT_PORTS = {m["bot"]: m["port"] for m in members}
    BOT_DIR_NAME = {m["bot"]: m["dir"] for m in members}
    INTROS = {m["bot"]: m["intro"] for m in members}
    _roster_applied = True


def _feature_on(bot: str, key: str) -> bool:
    """导演读这只的开关：频道目录走 bot_features.channel_dir（与管理台、命令行同一取法，审查必修 4）。
    认不出目录、或解析出来的目录不在导演自己的频道根内（旧名单兜底模式、配置来自别处）时，
    退回旧名单的 CHANNELS_ROOT/<目录名>，不能读空。读法同 bot_features.read（读不出按关）。"""
    bot_dir = bot_features.channel_dir(bot)
    if bot_dir is not None:
        root = os.path.realpath(CHANNELS_ROOT)
        real = os.path.realpath(bot_dir)
        if real != root and not real.startswith(root + os.sep):
            bot_dir = None
    if bot_dir is None:
        bot_dir = os.path.join(CHANNELS_ROOT, BOT_DIR_NAME.get(bot, bot))
    return bot_features.read(bot_dir)["features"][key]


def _drop_groups_off() -> list:
    """没有群清单时退回的旧名单同样按群聊开关过滤（PLAN-bot-features D2；有群清单时 _load_roster 只认 bots_with("groups")）：
    群聊关着的从五张名单表里拿掉，不点名、不往它的收件箱写，返回拿掉了谁。拿掉过就记作换过名单，下一轮先由
    _restore_legacy 退回完整的旧名单再按当时的开关重滤，重新打开的能回来；一只都没拿掉时五张表与 _roster_applied 都不动。"""
    global BOTS, BOT_BY_USERNAME, BOT_PORTS, BOT_DIR_NAME, INTROS, _roster_applied
    off = [b for b in BOTS if not _feature_on(b, "groups")]
    if off:
        BOTS = {b: v for b, v in BOTS.items() if b not in off}
        BOT_BY_USERNAME = {u: b for u, b in BOT_BY_USERNAME.items() if b not in off}
        BOT_PORTS = {b: v for b, v in BOT_PORTS.items() if b not in off}
        BOT_DIR_NAME = {b: v for b, v in BOT_DIR_NAME.items() if b not in off}
        INTROS = {b: v for b, v in INTROS.items() if b not in off}
        _roster_applied = True
    return off


def _restore_legacy() -> None:
    """没有群清单：换成过群配置名单才退回旧名单；从没换过就一张表都不动（旧测试改的表照常生效）。"""
    global BOTS, BOT_BY_USERNAME, BOT_PORTS, BOT_DIR_NAME, INTROS, _roster_applied
    if _roster_applied:
        BOTS, BOT_BY_USERNAME, BOT_PORTS, BOT_DIR_NAME, INTROS = (dict(t) for t in _LEGACY_ROSTER)
        _roster_applied = False


def _chat_ids() -> list:
    """run 每轮要处理的群：有群清单走清单里的群（可为空），没有走旧名单兜底的 CHAT_ID。"""
    from adapters import group_config
    groups = group_config.load()
    return [CHAT_ID] if groups is None else [g["chat_id"] for g in groups]


def tick(chat_id: str = CHAT_ID, now: float | None = None) -> dict:
    """主循环单步（纯函数化，便于离线测试）。返回 {"action": ...} 记录本步做了什么。"""
    now = now or time.time()
    if not switch_on(chat_id):
        return {"action": "off"}
    # 名单每轮现读：有群清单按这个群的成员走；不足两只这一轮不开场（限流记一行）
    members = _load_roster(chat_id, now)
    if members is None:
        _restore_legacy()
        if _drop_groups_off() and len(BOTS) < 2:   # 拿掉群聊关着的之后不足两只：与有群清单时同样不开场
            if not _throttled(("roster_short", chat_id), now):
                print(f"[director] roster_short chat={chat_id} n={len(BOTS)}", flush=True)
            return {"action": "roster_short", "n": len(BOTS)}
    elif len(members) < 2:
        if not _throttled(("roster_short", chat_id), now):
            print(f"[director] roster_short chat={chat_id} n={len(members)}", flush=True)
        return {"action": "roster_short", "n": len(members)}
    else:
        _apply_roster(members)
    st = _load_state(chat_id)
    hist = _as_hist(_read_hist(chat_id, n=HISTORY_N))
    if not hist:
        return {"action": "idle"}
    newest = hist[-1]
    new_msgs = [m for m in hist if _is_new(m, st)]
    # 缺陷②：私聊忙碌的 bot（dict 只给日志用 since_min）。只用于"无新消息"的续轮/开场路径；
    # 真人新消息分支（decide→inject、maybe_dm）逐字不变。
    busy = _busy_bots(now)
    # 需求⑤：停止与启用宽限中的 bot。与 busy 不同，真人新消息分支（decide/maybe_dm）也必须排除它们。
    stopped, resumed = _stop_state(now)
    avoid = stopped | resumed
    excl = dict(busy)
    excl.update({b: None for b in avoid})  # 值 None = 停止/宽限：文案归"已停止"段、不打 busy_skip

    # F-32：新消息批的最后一条是 bot 发言 → 该 bot 不能自己接自己，并入确定性排除集
    if new_msgs and new_msgs[-1]["is_bot"]:
        _self_bot = new_msgs[-1].get("speaker")
        if _self_bot in BOTS:
            avoid = avoid | {_self_bot}
            excl[_self_bot] = None

    # 急停：新消息里user说了停止词 → 锁 LOCK_MIN 分钟
    for m in new_msgs:
        if not m["is_bot"] and any(w in m["text"] for w in HARD_STOP_WORDS):
            st["lock_until"] = now + LOCK_MIN * 60
            _mark_decided(st, newest)
            _save_state(chat_id, st)
            return {"action": "lock", "until": st["lock_until"]}
    if now < st.get("lock_until", 0):
        if new_msgs:  # 锁期内消息只记账不决策
            _mark_decided(st, newest)
            _save_state(chat_id, st)
        return {"action": "locked"}

    if new_msgs:
        # 等下一轮 tick 再决策。只对"刚到的"消息防抖：未来时间戳（时钟偏差/坏值）不等——
        # 否则坏值会让导演一直 debounce 到时钟追上，期间 decide/maybe_dm 全停（验收 test_08b/test_16 的合成历史即此形状）
        scene = st.get("scene") or {}
        has_human_new = any(not m["is_bot"] for m in new_msgs)
        # 模型退避（I-7）：这批消息会走到 decide 且退避未到期 → 不调模型；
        # 连续失败满 LLM_GIVEUP_FAILS 次才记账放弃这批消息。放在防抖之前：模型不可用时等一拍没有意义
        if not (scene.get("active") and not has_human_new) and not retry_backoff.is_due(st.get("llm"), now):
            if _llm_fails(st) < LLM_GIVEUP_FAILS:
                return {"action": "llm_backoff"}
            _mark_decided(st, newest)
            _save_state(chat_id, st)
            return {"action": "pass", "reason": "模型不可用"}
        if 0 <= now - newest["ts"] < DEBOUNCE_SEC:
            return {"action": "debounce"}

        # 自主场进行中，且这批新消息全是 bot（无真人插话）→ 记账+定节奏。
        # 只扣预算/记谁说完/设 pace，真正点下一个 bot 放到下面"无新消息"分支（隔 PACE 秒），防刷屏。
        if scene.get("active") and not has_human_new:
            if now - scene.get("opened_ts", now) > SCENE_MAX_AGE_MIN * 60:  # 场次超绝对寿命→收场
                _mark_decided(st, newest)
                _close_scene(st, now)
                _save_state(chat_id, st)
                return {"action": "scene_end", "reason": f"场次超时({SCENE_MAX_AGE_MIN}min)"}
            for m in new_msgs:  # 每条新 bot 发言扣 1 轮预算，记住谁刚说
                if m["is_bot"]:
                    scene["budget"] = scene.get("budget", 0) - 1
                    scene["last_speaker"] = BOT_BY_USERNAME.get(
                        m["from_username"], scene.get("last_speaker"))
                    _clear_pending(scene)  # 被点名的 bot 已回应 → 撤下点名记账（F-17）
            _mark_decided(st, newest)
            # transcript 硬顶（不依赖 state，最后防线）→ 立刻收场，不给收尾
            if _bot_after_human_count(hist) > ABS_MAX:
                _close_scene(st, now)
                _save_state(chat_id, st)
                return {"action": "scene_end", "reason": "transcript硬顶"}
            # 收尾轮的回复已回来（closing_done）→ 真结束（自然结束时预算可能还>0）
            if scene.get("closing_done"):
                _close_scene(st, now)
                _save_state(chat_id, st)
                return {"action": "scene_end", "reason": "收尾完成"}
            scene["next_turn_after"] = now + random.randint(PACE_MIN, PACE_MAX)  # 隔一会儿再点下一个
            st["scene"] = scene
            _save_state(chat_id, st)
            return {"action": "scene_wait", "budget": scene["budget"]}

        # 真人消息 / 无活跃场：现有行为完全不变（人来了就重置 scene，回归真人主导）
        if has_human_new and scene.get("active"):
            # F-18：真人插话打断自主场也走收场（kind=human：不占日限，但更新冷却时间戳）
            _close_scene(st, now, kind_override="human")
        r = decide(chat_id, history=hist, exclude=avoid)
        if r.get("llm_failed"):
            _llm_record_failure(st, now, "decide")
            if _llm_fails(st) >= LLM_GIVEUP_FAILS:
                _mark_decided(st, newest)
                _save_state(chat_id, st)
                return {"action": "pass", "heat": r.get("heat"), "reason": "模型连续失败"}
            _save_state(chat_id, st)  # 消息保持未决，退避到期后重试
            return {"action": "llm_fail", "heat": r.get("heat")}
        st["llm"] = retry_backoff.cleared()
        _mark_decided(st, newest)  # 决策过就记账，同一条消息永不二次决策
        # 聚会有效期间不主动私聊；导演本轮新开了聚会同样按有效处理（INTERFACE 2.5）
        _party_on = party_state.is_active(chat_id)
        _party_opened = _maybe_open_party(chat_id, new_msgs, r, now, members, st)
        _dm = None if (_party_on or _party_opened) else maybe_dm(
            st, new_msgs, hist, now, exclude=avoid)  # 顺带判断要不要让某人主动私聊他
        _save_state(chat_id, st)
        if r.get("speak"):
            _batch = [m for m in new_msgs if not m["is_bot"]]  # 用户这一批连发的消息
            path = inject(r["who"], chat_id, hist, user_batch=_batch)
            out = {"action": "inject", "who": r["who"],
                   "heat": r.get("heat"), "inbox": path}
            if _dm:
                out["dm"] = _dm
            return out
        out = {"action": "pass", "heat": r.get("heat"), "reason": r.get("reason")}
        if _dm:
            out["dm"] = _dm
        return out

    # ── 无新消息 ──
    scene = st.get("scene") or {}

    # 自主场续轮：到点就让【导演读最后几句】动态判定——聊完了没 / 谁接 / 最后一句悬着没。
    # 结束由判断驱动（不是纯计数）；预算(SCENE_TURNS)与 ABS_MAX/寿命 只是软/硬上限兜底。
    if scene.get("active"):
        # 收尾轮已发过（不管收尾 bot 回没回，给过机会）→ 结束
        if scene.get("closing_done"):
            _close_scene(st, now)
            _save_state(chat_id, st)
            return {"action": "scene_end", "reason": "收尾完成"}
        if (now - scene.get("opened_ts", now) > SCENE_MAX_AGE_MIN * 60
                or _bot_after_human_count(hist) > ABS_MAX):
            _close_scene(st, now)
            _save_state(chat_id, st)
            return {"action": "scene_end", "reason": "场次超时/硬顶"}
        if now < scene.get("next_turn_after", 0):
            return {"action": "scene_pace"}  # 还没到点，等 pace / 等被点 bot 发言
        # F-17：上轮点名无人应（超时）→ 记 fail_count 并清理未消费的点名文件
        if scene.get("pending_bot") and now - scene.get("pending_since", now) > TURN_TIMEOUT:
            scene["fail_count"] = _int_or_zero(scene.get("fail_count")) + 1
            _drop_inbox(scene.get("pending_inbox"))
            _clear_pending(scene)
            st["scene"] = scene
            _save_state(chat_id, st)
            return {"action": "scene_point_timeout", "fail_count": scene["fail_count"]}
        if len(BOTS) - len(set(excl) & set(BOTS)) < 2:  # 可用 bot < 2：场撑不起来，直接收场不点名
            for b in BOTS:
                if b in excl:
                    _log_excluded("scene_close", b, excl, now)
            _close_scene(st, now)
            _save_state(chat_id, st)
            return {"action": "scene_end", "reason": _busy_reason(set(excl), excl)}
        for b in BOTS:  # 忙者本轮被排除在点名之外（限流日志，S2 可观测）
            if b in excl:
                _log_excluded("scene_turn", b, excl, now)
        if not retry_backoff.is_due(st.get("llm"), now):
            return {"action": "llm_backoff"}
        budget = scene.get("budget", 0)
        last_bot = next((m for m in reversed(hist) if m["is_bot"]), None)
        v = decide_scene(hist, budget, exclude=scene.get("last_speaker"), busy=excl)
        if v.get("llm_failed"):
            _llm_record_failure(st, now, "decide_scene")
            scene_fails = _int_or_zero(scene.get("fail_count")) + 1
            if scene_fails >= SCENE_LLM_FAIL_MAX:  # 本场连续失败满 3 次 → 收场，不占名额
                _close_scene(st, now, llm_abort=True)
                _save_state(chat_id, st)
                return {"action": "scene_end", "reason": "模型连续失败"}
            scene["fail_count"] = scene_fails
            st["scene"] = scene
            _save_state(chat_id, st)
            return {"action": "llm_fail", "scene_fail_count": scene_fails}
        st["llm"] = retry_backoff.cleared()
        scene["fail_count"] = 0
        # 兜底改选：不连说、不点忙者/停止者（可用≥2 保证非 None）
        fallback = _pick_other({scene.get("last_speaker")} | set(excl)) or _pick_other(set())
        # 导演判定聊完了，或预算软顶到了 → 该收尾
        if v["done"] or budget <= 0:
            if v["dangling"]:  # 最后一句悬着（问题/话头没接）→ 加 1 轮把它接住再结束
                who = v["who"] or fallback
                scene["closing_done"] = True
                scene["next_turn_after"] = now + TURN_TIMEOUT
                st["scene"] = scene
                _save_state(chat_id, st)
                path = inject(who, chat_id, hist, prev=last_bot, closing=True)
                _record_scene_turn()
                return {"action": "scene_close", "who": who,
                        "budget": budget, "inbox": path}
            _close_scene(st, now)  # 已自然收尾，最后一句不悬 → 直接结束
            _save_state(chat_id, st)
            return {"action": "scene_end",
                    "reason": ("导演判定聊完" if v["done"] else "预算软顶")}
        # 继续：点导演选的人接话
        who = v["who"] or fallback
        scene["next_turn_after"] = now + TURN_TIMEOUT  # 宽限等 ta 发言；发言后续轮分支改回 pace
        path = inject(who, chat_id, hist, prev=last_bot, low_budget=(budget <= 2))
        _mark_pending(scene, who, path, now)  # F-17：记点名，超时无人应可记账/清理
        st["scene"] = scene
        _save_state(chat_id, st)
        _record_scene_turn()
        return {"action": "scene_turn", "who": who, "budget": budget, "inbox": path}

    # 无活跃场 → 找一个触发理由开一整场自主群聊（过节流闸）
    if not retry_backoff.is_due(st.get("llm"), now):
        return {"action": "llm_backoff"}  # 退避期间不开自主场；_check_triggers 只读，跳过不漏定时动作
    trig = _check_triggers(st, hist, now, excl)
    if trig:
        kind = trig["kind"]
        if kind == "cold_gap":  # 冷场无论开不开都记时间戳，避免每 tick 重复触发
            st["last_initiate_ts"] = now
        go, reason = _scene_gates(st, hist, now, kind, excl)
        if not go:
            _save_state(chat_id, st)
            return {"action": "scene_skip", "kind": kind, "reason": reason}
        opener = trig.get("opener")
        context = trig.get("context") or None
        if not opener:  # 冷场：让 decide_initiate 挑一个人起头
            ri = decide_initiate(hist, exclude=excl)
            if ri.get("llm_failed"):
                _llm_record_failure(st, now, "decide_initiate")
                _save_state(chat_id, st)  # last_initiate_ts 已记，冷场不会每 tick 重触发
                return {"action": "initiate_pass", "reason": "模型调用失败"}
            st["llm"] = retry_backoff.cleared()
            if not ri.get("speak"):
                _save_state(chat_id, st)
                return {"action": "initiate_pass"}
            opener = ri["who"]
        _open_scene(st, now, kind, opener)
        st["scene"]["next_turn_after"] = now + TURN_TIMEOUT  # 等 opener 发言
        _prewarm_all_workers(chat_id)  # 三个 worker 都预热，续轮不冷启动
        path = inject(opener, chat_id, hist,
                      initiate=(kind == "cold_gap" and not context), context=context)
        _mark_pending(st["scene"], opener, path, now)  # F-17：开场也是点名
        _save_state(chat_id, st)
        _record_scene_turn()
        return {"action": "scene_open", "kind": kind, "who": opener,
                "inbox": path, "scene_budget": SCENE_TURNS}
    return {"action": "idle"}


def run(chat_id: str | None = None) -> None:
    """常驻主循环（平台服务 / launchd / 计划任务拉活）。chat_id 给定只跑这一个群；不给 → 每轮现读群清单逐群跑
    （没有群清单走旧名单兜底的 CHAT_ID）。任一群抛异常整轮按原退避处理（目前只有一个群）。"""
    try:
        first = [chat_id] if chat_id else _chat_ids()
        print(f"[director] 启动，chat={','.join(first)}，"
              f"开关={','.join('开' if switch_on(c) else '关' for c in first)}", flush=True)
    except Exception as e:  # 群清单坏了也不能让常驻进程起不来：循环里照常记日志退避
        print(f"[director] 启动时读群清单失败: {type(e).__name__}: {e}", flush=True)
    loop_fails = 0  # tick 连续抛异常次数：间隔 2、4、8…封顶 300 秒，正常返回即归零（拍板 9）
    while True:
        try:
            for cid in ([chat_id] if chat_id else _chat_ids()):
                r = tick(cid)
                if r["action"] not in ("idle", "off", "debounce", "locked", "roster_short",
                                       "scene_pace", "scene_wait", "scene_skip", "llm_backoff"):
                    print(f"[director] {json.dumps(r, ensure_ascii=False)}", flush=True)
            loop_fails = 0
        except Exception as e:
            import traceback
            tail = traceback.format_exc().strip().splitlines()[-2:]
            print(f"[director] tick 异常: {type(e).__name__}: {e} | {' / '.join(tail)}", flush=True)
            loop_fails += 1
            time.sleep(retry_backoff.next_delay(loop_fails, LOOP_BACKOFF_BASE_SEC, LOOP_BACKOFF_CAP_SEC))
            continue
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    import sys
    if "--run" in sys.argv:
        run()
    elif "--roster" in sys.argv:  # 只读：打印每个群这一轮的名单，供上线前核对（不写任何东西）
        for cid in _chat_ids():
            ms = _load_roster(cid, time.time())
            print(json.dumps({"chat_id": cid, "members": ms}, ensure_ascii=False))
    else:  # 默认只看一步决策，不写任何东西之外的副作用
        hist = read_group_history()
        print(f"读到 {len(hist)} 条去重后群历史，当前 heat={compute_heat(hist)}")
        print(json.dumps(decide(history=hist), ensure_ascii=False, indent=2))
