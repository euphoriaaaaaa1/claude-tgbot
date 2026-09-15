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
import json
import time
import random
import subprocess
from datetime import datetime, timezone

from claude_cli import call_claude_json  # 走 DeepSeek 快通道，不烧 Claude 配额
import chat_history  # session_uuid：确定性定位 worker 会话，别靠 mtime 猜
from bots_registry import disabled_ids_safe  # 需求⑤：停用判定唯一入口（configs/<bot>.yml enabled:false，fail-open 在其内部）

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

CHAT_ID = os.environ.get("DIRECTOR_CHAT_ID", "")  # 你的 Telegram 群 id（负数），从环境变量配置
MAX_HEAT = 5  # 一条真人消息后，角色之间最多接 MAX_HEAT 轮（你一句我一句），额度耗尽自然停
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
# 缺陷②：群 transcript 看不到私聊。dispatcher 每条真人私聊都写 <MARKER_DIR>/<bot目录名>-<chat>.last-user（int 秒），
# 30min 内私聊过的 bot 视为"忙"：不当开场人、不被续轮点名；可用 bot < 2 不开场/收场。
PRIVATE_BUSY_MIN = 30
MARKER_DIR = os.path.expanduser(
    os.environ.get("DIRECTOR_MARKER_DIR") or "~/.claude/dispatcher/.self-initiate-state")
BUSY_LOG_THROTTLE_SEC = 300  # busy_skip/stopped_skip/resumed_skip 等同键日志限流
RESUME_GRACE_MIN = 30        # 需求⑤：bot 启用后宽限期内仍排除，防补发停用期积压的开场理由（边界不含）

# ── 运行层参数（全部可环境变量覆盖，测试指到假目录，零碰生产）──
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

# bot_id → channel 目录名 与 dispatcher 端口（拉活 worker 用）
BOT_DIR_NAME = {"bot1": "bot1", "bot2": "bot2", "bot3": "bot3"}  # bot_id → channel 目录名，按你的部署改
BOT_PORTS = {"bot1": "17801", "bot2": "17802", "bot3": "17803"}  # 各 bot dispatcher 端口

# 急停口令（用户在群里说了就锁 LOCK_MIN 分钟）。保守词表，避免"别停"之类误伤。
HARD_STOP_WORDS = ("别聊了", "闭嘴", "安静一下", "都别说", "停停停", "别刷了")

# bot 的 Telegram username → 内部 bot_id。角色人设不在导演里配置，
# 导演只负责"选谁说"，说什么由各 bot 自己的设定决定。
BOT_BY_USERNAME = {
    "@your_bot1_username": "bot1",
    "@your_bot2_username": "bot2",
    "@your_bot3_username": "bot3",
}  # 各 bot 的 Telegram username → bot_id
BOTS = {"bot1": "角色一", "bot2": "角色二", "bot3": "角色三"}  # bot_id → 角色显示名
HUMAN_ID = os.environ.get("DIRECTOR_HUMAN_ID", "")  # 你的 Telegram user_id
HUMAN_NAME = os.environ.get("DIRECTOR_HUMAN_NAME", "主人")  # 你在群里的称呼

_GT_DIR = os.environ.get(
    "DIRECTOR_GT_DIR", os.path.expanduser("~/.claude/channels/group_transcripts"))


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def read_group_history(chat_id: str = CHAT_ID, n: int = 12) -> list[dict]:
    """按 message_id 去重读群历史（transcript 每条被多个 bot 各记一遍），时间正序取最后 n。

    返回 [{msg_id, ts, text, is_bot, from_username, speaker}]，speaker = bot_id 或 '用户'。
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
            is_bot = str(o.get("is_bot", "")).lower() == "true"
            uname = o.get("from_username") or ""
            if is_bot:
                speaker = BOT_BY_USERNAME.get(uname, uname or "某bot")
            else:
                speaker = HUMAN_NAME if str(o.get("from_id", "")) == HUMAN_ID else "路人"
            rt_from = o.get("reply_to_from") or ""
            seen[mid] = {
                "msg_id": mid,
                "ts": _parse_ts(o.get("ts", "")),
                "text": (o.get("text") or "").strip(),
                "is_bot": is_bot,
                "from_username": uname,
                "speaker": speaker,
                # 引用回复：这条消息是"回复"谁的哪句话
                "reply_to_text": (o.get("reply_to_text") or "").strip(),
                "reply_to_speaker": BOT_BY_USERNAME.get(rt_from, rt_from) if rt_from else "",
            }
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


def _fmt_msg(m: dict) -> str:
    """把一条消息格式化成 '说话人（回复X的「…」）：文本'，带引用上下文。"""
    q = ""
    rt = m.get("reply_to_text")
    if rt:
        rn = BOTS.get(m.get("reply_to_speaker"), m.get("reply_to_speaker") or "")
        who = f"{rn}的" if rn else ""
        q = f"（回复{who}「{rt[:40]}」）"
    return f"{m['speaker']}{q}：{m['text'][:120]}"


def _build_prompt(history: list[dict], heat: int, exclude: str | None = None, avail=None) -> str:
    """avail：可被点名的 bot（默认全部）；已停止/启用宽限中的 bot 不出现在 who 枚举里。"""
    lines = [_fmt_msg(m) for m in history]
    convo = "\n".join(lines) if lines else "（群里还没人说话）"
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    who_enum = "或".join(f'"{b}"' for b in (avail or BOTS)) or '"bot1"'
    excl_line = ""
    if exclude:
        excl_line = f"⚠️ {BOTS.get(exclude, exclude)}({exclude}) 刚说完，这一轮别再选 ta，让别的角色接。\n"
    return (
        f"你是一个 Telegram 群聊的导演。群里有真人 {HUMAN_NAME} 和若干角色。\n\n"
        "【当前在场角色】\n"
        f"{roster}\n"
        f"（每个角色的性格、说话风格、怎么称呼 {HUMAN_NAME}，都由各角色自己的设定决定——"
        "你不用知道、也不要替 ta 们设定。你只判断这一轮该谁开口。）\n\n"
        f"最近的群聊（时间从早到晚）：\n{convo}\n\n"
        f"现在还剩 {heat} 轮接话额度（角色之间每接一句消耗 1，耗尽自然收尾）。\n"
        f"{excl_line}"
        "你每次只点【一个】角色说话；连续的多轮点名就自然形成'你一句我一句'的群聊。像真人闲聊群那样：\n"
        "- 不是每句话都要有人接；没必要接就让它冷一下（speak=false）。\n"
        f"- **话题连续性最重要**：{HUMAN_NAME} 在接谁的话、回应谁，就让那个人继续接。谁挑起或正被回应的话题，就选谁，别莫名换人。\n"
        f"- **看引用**：消息带「回复X的『…』」表示 {HUMAN_NAME} 在针对那句话说。按这句话的内容和矛头判断该谁接——不一定是被引用的那个人本人，谁被点到/谁最该回应就选谁。\n"
        f"- **角色之间也能互相接**：不是只围着 {HUMAN_NAME} 转。一个角色刚说完，别的角色可以附和 / 接梗 / 打趣 / 拌嘴 / 补一句，让话题在 ta 们之间滚起来——只要还有额度，就大胆你来我往地聊，别每轮都急着收。\n"
        "- 只有话题明显转向、或某人被冷落太久该拉进来时才换人；别老让同一个人连说太多，也别乱插队。\n"
        "- 只剩最后 1~2 轮额度时更倾向收尾、别硬接。\n\n"
        "你的职责【只有一件】：决定这一轮要不要有人说、该谁说。**不要替 ta 想说什么、更不要替 ta 写台词**——"
        f"说什么内容、用什么语气、怎么称呼 {HUMAN_NAME}，全部由被选中的 bot 自己按人设决定，不归你管。\n"
        f'只输出一个 JSON，不要别的：{{"speak": true或false, "who": {who_enum}}}。'
        "speak=false 表示这一轮没人说（冷场或该结束了），此时 who 可省略。"
    )


def decide(chat_id: str = CHAT_ID, history: list[dict] | None = None,
           scene_budget: int | None = None, exclude=None) -> dict:
    """决定这一轮谁说话。返回 {speak, who?, heat, reason?}。只出 who，不产内容（beat）。
    - scene_budget 给定（自主场续轮）：用它当额度，不看 compute_heat（无真人也能接）。
    - exclude：str=刚说完的 bot（续轮不连说）；集合=已停止/启用宽限中的 bot（需求⑤）。命中 → 确定性改选。
    heat<=0 直接不接（确定性，不调 LLM）——这是死循环掐断点。"""
    excl = _as_set(exclude)
    if history is None:
        history = read_group_history(chat_id)
    if not history:
        return {"speak": False, "heat": MAX_HEAT, "reason": "群历史为空"}
    if scene_budget is not None:
        heat = scene_budget
    else:
        heat = compute_heat(history)
        if heat <= 0:
            return {"speak": False, "heat": heat, "reason": "接话额度耗尽，等用户再开口"}
    avail = [b for b in BOTS if b not in excl]
    if not avail:
        return {"speak": False, "reason": "可用bot不足"}  # INTERFACE §9.2 形状（无 heat 键）
    r = call_claude_json(_build_prompt(history, heat, exclude=exclude if isinstance(exclude, str) else None,
                                       avail=avail), timeout=30) or {}
    who = (r.get("who") or "").strip()
    if not r.get("speak") or who not in BOTS:
        return {"speak": False, "heat": heat, "reason": "导演判定这轮不接", "raw": r}
    if who in excl:  # 刚说完的人 / 停用者 → 确定性改选
        who = _pick_other(excl)
    return {"speak": True, "who": who, "heat": heat}


def decide_scene(history: list[dict], budget: int, exclude: str | None = None, busy=()) -> dict:
    """自主场每轮动态判定：让导演读最近几句，判断①这场闲聊聊完没②谁接③最后一句悬着没。
    返回 {"done": bool, "dangling": bool, "who": str|None}。结束由这个判断驱动，不是纯计数。
    exclude=刚说完的人、busy=正在私聊/已停止的人：LLM 选中二者之一 → 确定性改选（可为 None）。"""
    busy = set(busy or ())
    excl_set = busy | ({exclude} if exclude else set())
    lines = [f"{m['speaker']}：{m['text']}" for m in history[-6:]]
    convo = "\n".join(lines) if lines else "（没有对话）"
    excl = f"刚说完的是 {BOTS.get(exclude, exclude)}({exclude})，别再选 ta。\n" if exclude else ""
    excl += "".join(f"别选 {BOTS[b]}({b})（正在忙）。\n" for b in BOTS if b in busy)
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    who_enum = "或".join(f'"{b}"' for b in BOTS) or '"bot1"'
    prompt = (
        f"你是一个 Telegram 群闲聊的导演。群里有若干角色（{roster}）和真人 {HUMAN_NAME}。\n"
        f"最近几句：\n{convo}\n\n{excl}"
        f"这场闲聊还剩约 {budget} 轮接话额度（软上限；聊完就停、别硬凑，也别没聊完就掐）。\n"
        "读一下最后几句，判断三件事，**只输出一个 JSON**，别的都不要：\n"
        f'{{"done": true或false, "dangling": true或false, "who": {who_enum}}}\n'
        "- done：这场闲聊是不是自然聊完了——有人收了尾/达成一致/开始道晚安/没新东西可聊/气氛散了，就 true；"
        "还热着、有话头没接、有人被问到，就 false。\n"
        "- dangling：最后一句是不是抛了个问题或话头、还没人接（需要有人回应一下才不突兀）。\n"
        "- who：若还要继续(done=false)，谁最适合接下一句；最后一句是问题就让最该接的人接。done=true 且不 dangling 时 who 可省略。\n"
    )
    r = call_claude_json(prompt, timeout=30) or {}
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


def _load_state(chat_id: str) -> dict:
    try:
        with open(_state_path(chat_id), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(chat_id: str, st: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(chat_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
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
    if _is_stopped(bot):  # 需求⑤：停用的 bot 一律不拉起
        _log_stopped("ensure", bot)
        return
    if os.environ.get("DIRECTOR_NO_SPAWN"):  # 测试模式：不真拉 worker
        return
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{BOT_PORTS[bot]}/ensure_worker", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[director] ensure_worker({bot}) 失败(dispatcher 未起?): {e}", flush=True)


def _batch_human_ts(user_batch) -> str | None:
    """缺陷③b：批内真人消息最新 ts（能转成正有限 float 的才算）→ UTC ISO 整秒；无有效 ts → None。永不抛。
    worker 只认生产者正向打的 human_ts 来算"距你们上次说话"，导演注入的合成消息不带它就不会重置计时。"""
    best = 0.0
    for m in user_batch or ():
        try:
            t = float(m.get("ts"))
        except Exception:  # 缺失/None/非数/非 dict：只丢这一条
            continue
        if best < t < float("inf"):  # NaN 比较恒假，自然被排除
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
    user_batch 给定（用户连发了几条）：明确列出让 bot 一起照顾到，别只回最新一条。
    停用的 bot（需求⑤）：不写 inbox、不拉起，返回 ""。"""
    if _is_stopped(bot):  # 入口硬闸：不依赖上游有没有排除
        _log_stopped("inject", bot)
        return ""
    convo ="\n".join(_fmt_msg(m) for m in history[-8:])
    # 用户连发多条 → 明确列出，要求一次回复里都照顾到（治"只回最后一条"）。
    batch_block = ""
    if user_batch and len(user_batch) > 1:
        listed = "\n".join(f"  - {_fmt_msg(m)}" for m in user_batch[-5:])
        batch_block = (f"\n【{HUMAN_NAME}刚连着说了这几句，别只挑最新一条回——在你这一条回复里把它们都照顾到】：\n"
                       f"{listed}\n")
    if closing and prev is not None:
        prev_name = BOTS.get(prev.get("speaker"), prev.get("speaker") or "群友")
        prev_text = (prev.get("text") or "")[:80]
        head = (f"[director]（这场聊天要收尾了。{prev_name} 刚说：「{prev_text}」"
                "——你来说**最后一句**：把 ta 这句/这个问题自然接住、答一下，顺势把话题轻轻收掉。）")
        guide = ("这是这场的最后一句：**必须接住上面那句、别让它悬着**；"
                 "别再抛新问题、别起新话题，说完这场就自然结束。")
    elif prev is not None:
        prev_name = BOTS.get(prev.get("speaker"), prev.get("speaker") or "群友")
        prev_text = (prev.get("text") or "")[:80]
        head = (f"[director]（群里正在聊天。{prev_name} 刚说：「{prev_text}」"
                "——现在轮到你，先接 ta 这句：附和/接梗/打趣/拌嘴/追问都行，接完可以自然延伸。）")
        guide = f"顺着上面的话接，别只顾着回{HUMAN_NAME}——和群里其他角色你一句我一句地聊起来。"
        if low_budget:  # 临近收尾：往回收，别再起新话头
            guide += "（这场快聊完了，往收尾方向走，别再抛新问题或新话题。）"
    elif context:
        head = f"[director]（{context}。你想跟群里其他角色聊聊，自然地起个话头。）"
        guide = "按这个由头起个话头，别硬邀请别人；说说你自己此刻的状态/想法就行。"
    elif initiate:
        head = "[director]（群里安静了一阵，现在轮到你——想说点什么就自然地在群里起个话头）"
        guide = "该跟群里其他角色开个话头、聊起来；没合适的就说说自己此刻在干嘛。"
    else:
        head = "[director]（群里正在聊天，现在轮到你说话了）"
        guide = f"该回应{HUMAN_NAME}就回应、该跟群里其他角色接话/拌嘴就接、没合适的就起个新话头。"
    text = (
        f"{head}\n"
        f"群里最近的对话：\n{convo or '（还没人说话）'}\n"
        f"{batch_block}\n"
        f"自己读一下上面聊到哪了，然后【完全按你自己的人设、语气、记忆】决定说什么、怎么称呼{HUMAN_NAME}——"
        f"{guide}"
        "像真人发微信那样 1~2 句短句，口语化；不要旁白/动作描写；不要 @ 人。"
        "群闺蜜聊天本来就爱斗图——情绪上来了就按你人设发个**表情包/emoji/颜文字**"
        "（表情包按 CLAUDE.md 规则用空 text 的 reply 发，别配字）；"
        "自拍/生图这类重的想发再发、别每句都发，自然点。"
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
    用户不回，就没有后续（防止无人时 bot 自嗨烧配额）。
    exclude=正在私聊/已停止的 bot：提示词里点名别选；LLM 仍选中 → 确定性改选，全排除 → speak=False。"""
    exclude = set(exclude or ())
    busy_line = "".join(f"别选 {BOTS[b]}({b})（正在忙）。\n" for b in BOTS if b in exclude)
    lines = [f"{m['speaker']}：{m['text'][:100]}" for m in history[-6:]]
    convo = "\n".join(lines) if lines else "（群里很久没人说话）"
    roster = "、".join(f"{bid}={name}" for bid, name in BOTS.items())
    who_enum = "或".join(f'"{b}"' for b in BOTS) or '"bot1"'
    prompt = (
        f"Telegram 群里有真人 {HUMAN_NAME} 和若干角色（{roster}），群里已经安静了一阵。\n"
        f"之前的对话：\n{convo}\n\n"
        "你是导演，只决定要不要让某个角色自然地起个话头、以及选谁——**不要替 ta 想说什么**，"
        "起什么话头由 ta 自己按人设定。不必每次都发起，可以继续安静。\n"
        f"{busy_line}"
        f'只输出 JSON：{{"speak": true或false, "who": {who_enum}}}'
    )
    r = call_claude_json(prompt, timeout=30) or {}
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


def _pick_other(exclude: set) -> str | None:
    """BOTS 顺序里第一个不在 exclude 的 bot；全排除 → None。"""
    return next((b for b in BOTS if b not in exclude), None)


def _busy_bots(now: float) -> dict:
    """{bot: since_min}：该 bot 所有私聊 marker 的最大 ts 距 now 在 [-60s, 30min) → 忙。
    marker 名是 BOT_DIR_NAME（dispatcher 按 bot 目录名写）。fail-open：缺文件/坏内容/远未来/过旧 → 不忙；永不抛。"""
    import glob
    busy, found_any = {}, False
    for bot in BOTS:
        best = None
        pattern = os.path.join(MARKER_DIR, f"{glob.escape(BOT_DIR_NAME.get(bot, bot))}-*.last-user")
        for path in glob.glob(pattern):
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
        # ② 静默失效必须可观测：一个 marker 都没有，多半是目录/命名漂移
        print(f"[director] busy_marker_missing dir={MARKER_DIR}", flush=True)
    return busy


def _busy_reason(excl: set, mins: dict) -> str:
    """可用bot<2 的原因文案。mins 值为 None 的是已停止/启用宽限中的 bot，其余算私聊忙碌；两段各按 BOTS 顺序。"""
    stop = [b for b in BOTS if b in excl and b in mins and mins[b] is None]
    busy = [b for b in BOTS if b in excl and b not in stop]
    parts = ([f"私聊忙碌({','.join(busy)})"] if busy else []) + ([f"已停止({','.join(stop)})"] if stop else [])
    return "或".join(parts) + "可用bot<2"


def _log_excluded(where: str, bot: str, mins: dict, now: float) -> bool:
    """忙者打 busy_skip；已停止/宽限中的 bot（mins 值为 None）另有 stopped/resumed 行，不重复。"""
    m = mins.get(bot, 0)
    return m is not None and _log_busy(where, bot, m, now)


# ══════════════════ 需求⑤：手动停用的 bot 不被导演拉起、不被替写消息 ══════════════════
# 判定源：configs/<bot>.yml enabled:false，经 bots_registry.disabled_ids_safe()（fail-open 在其内部）。
_prev_stopped = None     # 上一 tick 的停用集合；None=首个 tick，不判定"启用"转换
_resumed_at: dict = {}   # bot → 检测到启用的时刻（启用宽限用）


def _as_set(x) -> set:
    """排除参数统一转集合：str → {str}（兼容旧的"刚说完的人"单值），dict 取键，None/空 → 空集。"""
    return {x} if isinstance(x, str) else set(x or ())


def _is_stopped(bot: str) -> bool:
    return bot in disabled_ids_safe()


def _log_stopped(where: str, bot: str, now: float | None = None) -> None:
    if not _throttled((where, bot), time.time() if now is None else now):
        print(f"[director] stopped_skip where={where} bot={bot}", flush=True)


def _stop_state(now: float) -> tuple[set, set]:
    """每 tick 一次：(stopped, resumed)。停用→启用的 bot 在 RESUME_GRACE_MIN 内仍排除（同停用待遇），防补发停用期积压的开场理由。"""
    global _prev_stopped
    stopped = disabled_ids_safe() & set(BOTS)
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
            # 裁决 D1：公开锁定用例要求刚启用那一 tick 报 since_min=0（私有锁定的是"至少 1"），整分钟向下取
            print(f"[director] resumed_skip bot={b} since_min={int((now - t) // 60)}", flush=True)
    return stopped, set(_resumed_at)


def _scene_gates(st: dict, hist: list[dict], now: float, kind: str, busy=()) -> tuple[bool, str]:
    """自主开场七道闸，代码顺序即契约：夜间 → 真人30min → 忙碌/停止 → 距上场冷却 → 同类冷却 → 日限 → 配额（永远最后）。
    返回 (go, reason)。human 触发不过闸。busy 可为 set/dict/tuple/None（dict 的值只供日志取 since_min）。"""
    mins = dict(busy) if isinstance(busy, dict) else {}
    busy = set(busy or ())
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
    if not _quota_ok():
        return (False, "quota不足")
    return (True, "ok")


def _check_triggers(st: dict, hist: list[dict], now: float, busy=()) -> dict | None:
    """按优先级找一个自主开场理由。返回 {kind, opener, context} 或 None。
    jiwen/朋友圈/特殊日 全 try/except fail-open；最后兜底冷场。busy 里的 bot 不当开场人。"""
    mins = dict(busy) if isinstance(busy, dict) else {}  # 只供日志取 since_min；值 None=已停止/宽限
    busy = set(busy or ())
    newest_ts = hist[-1]["ts"] if hist else 0

    # 1) jiwen 情绪越阈：任一 bot forced/描述非空 → 由 ta 起头
    # ponytail: 想开场的人正在私聊 → 本 tick 不再下探朋友圈/特殊日/冷场（INTERFACE §2.6：该 bot 忙且无其它 jiwen 触发 → idle）
    jiwen_busy_skipped = False
    if _jiwen_reader:
        for bot in BOTS:
            try:
                info = _jiwen_reader.read(bot, HUMAN_ID, _global_cfg())
            except Exception:
                info = None
            if info and (info.get("forced") or info.get("description")):
                if bot in busy:  # forced 的人正在私聊：本来就不该去群，不换人代开
                    if mins.get(bot, 0) is not None:  # 已停止/宽限中的人只跳过，不压住别的触发（裁决 D2）
                        _log_busy("jiwen_opener", bot, mins.get(bot, 0), now)
                        jiwen_busy_skipped = True
                    continue
                return {"kind": "jiwen", "opener": bot,
                        "context": f"你现在{info.get('description') or '心里有点动静'}"}
    if jiwen_busy_skipped:
        return None

    # 2) 朋友圈事件：上次检查点后有人发了新圈 → 别的 bot 起头搭话
    if _db:
        try:
            last_seen = st.get("last_moment_seen_ts", 0)
            ms = _db.list_moments(limit=5, since_ts=int(last_seen)) or []
            ms = [m for m in ms if (m.get("visibility") or "public") == "public"]
            if ms:
                m0 = ms[0]
                poster = m0.get("bot_id", "")
                opener = next((b for b in BOTS if b != poster), "bot3")
                st["last_moment_seen_ts"] = max(int(m0.get("ts", now)), int(last_seen))
                if opener in busy:
                    _log_excluded("moment_opener", opener, mins, now)
                    opener = _pick_other(busy | {poster})
                txt = (m0.get("text") or "")[:60]
                pname = BOTS.get(poster, poster or "群友")
                if opener:  # 没人可用 → 本触发作废、继续下探（检查点照样推进，免得每 tick 重触发）
                    return {"kind": "moment", "opener": opener,
                            "context": f"你刷到 {pname} 刚发的朋友圈：「{txt}」"}
        except Exception:
            pass

    # 3) 特殊日期：当天一次
    if _holiday:
        try:
            from datetime import date as _date
            d = _date.fromtimestamp(now)
            if _holiday.is_holiday(d):
                opener = next(iter(BOTS), None)
                if opener in busy:
                    _log_excluded("special_opener", opener, mins, now)
                    opener = _pick_other(busy)
                if opener:
                    return {"kind": "special_date", "opener": opener,
                            "context": "今天是个特别的日子"}
        except Exception:
            pass

    # 4) 冷场兜底：距最后一条消息与上次发起都超过 INITIATE_MIN
    gap = INITIATE_MIN * 60
    if now - newest_ts > gap and now - st.get("last_initiate_ts", 0) > gap:
        return {"kind": "cold_gap", "opener": None, "context": ""}
    return None


def _open_scene(st: dict, now: float, kind: str, opener: str) -> None:
    st["scene"] = {"active": True, "kind": kind, "budget": SCENE_TURNS,
                   "last_speaker": opener, "opened_ts": now,
                   "next_turn_after": 0.0, "fail_count": 0}


def _close_scene(st: dict, now: float) -> None:
    sc = st.get("scene") or {}
    kind = sc.get("kind", "cold_gap")
    sc["active"] = False
    st["scene"] = sc
    st["last_scene_end_ts"] = now
    kl = st.get("scene_kind_last_ts") or {}
    kl[kind] = sc.get("opened_ts", now)
    st["scene_kind_last_ts"] = kl
    day = _today_str(now)
    cnt = st.get("scenes_today") or {}
    if cnt.get("date") != day:
        cnt = {"date": day, "count": 0}
    if kind != "human":
        cnt["count"] = cnt.get("count", 0) + 1
    st["scenes_today"] = cnt


def _prewarm_all_workers(chat_id: str) -> None:
    """开场时把三个 bot 的 worker 都预热，避免续轮冷启动、被点无人应。"""
    for b in BOTS:
        if _is_stopped(b):  # 需求⑤：停用的不预热
            _log_stopped("prewarm", b)
            continue
        _ensure_worker_alive(b, chat_id)


def tick(chat_id: str = CHAT_ID, now: float | None = None) -> dict:
    """主循环单步（纯函数化，便于离线测试）。返回 {"action": ...} 记录本步做了什么。"""
    now = now or time.time()
    if not switch_on(chat_id):
        return {"action": "off"}
    st = _load_state(chat_id)
    hist = _read_hist(chat_id, n=HISTORY_N)
    if not hist:
        return {"action": "idle"}
    newest = hist[-1]
    new_msgs = [m for m in hist if _is_new(m, st)]
    # 缺陷②：私聊忙碌的 bot（dict 值只给日志用 since_min）。只用于"无新消息"的续轮/开场路径；
    # 真人新消息分支（decide→inject）不看 busy（BRIEF ②）。
    busy = _busy_bots(now)
    # 需求⑤：停用与启用宽限中的 bot。与 busy 不同，真人新消息分支（decide）也必须排除它们。
    stopped, resumed = _stop_state(now)
    avoid = stopped | resumed
    excl = dict(busy)
    excl.update({b: None for b in avoid})  # 值 None = 停用/宽限：文案归"已停止"段、不打 busy_skip

    # 急停：新消息里用户说了停止词 → 锁 LOCK_MIN 分钟
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
        if now - newest["ts"] < DEBOUNCE_SEC:
            return {"action": "debounce"}  # 等下一轮 tick 再决策

        scene = st.get("scene") or {}
        has_human_new = any(not m["is_bot"] for m in new_msgs)

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
            scene["active"] = False
            st["scene"] = scene
        r = decide(chat_id, history=hist, exclude=avoid)
        _mark_decided(st, newest)  # 决策过就记账，同一条消息永不二次决策
        _save_state(chat_id, st)
        if r.get("speak"):
            _batch = [m for m in new_msgs if not m["is_bot"]]  # 用户这一批连发的消息
            path = inject(r["who"], chat_id, hist, user_batch=_batch)
            return {"action": "inject", "who": r["who"],
                    "heat": r.get("heat"), "inbox": path}
        return {"action": "pass", "heat": r.get("heat"), "reason": r.get("reason")}

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
        budget = scene.get("budget", 0)
        last_bot = next((m for m in reversed(hist) if m["is_bot"]), None)
        v = decide_scene(hist, budget, exclude=scene.get("last_speaker"), busy=excl)
        # 兜底改选：不连说、不点忙者/停止者（可用≥2 保证非 None）
        fallback = _pick_other({scene.get("last_speaker")} | set(excl)) or "bot3"
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
        st["scene"] = scene
        _save_state(chat_id, st)
        path = inject(who, chat_id, hist, prev=last_bot, low_budget=(budget <= 2))
        _record_scene_turn()
        return {"action": "scene_turn", "who": who, "budget": budget, "inbox": path}

    # 无活跃场 → 找一个触发理由开一整场自主群聊（过节流闸）
    seen0 = st.get("last_moment_seen_ts")
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
            if not ri.get("speak"):
                _save_state(chat_id, st)
                return {"action": "initiate_pass"}
            opener = ri["who"]
        _open_scene(st, now, kind, opener)
        st["scene"]["next_turn_after"] = now + TURN_TIMEOUT  # 等 opener 发言
        _save_state(chat_id, st)
        _prewarm_all_workers(chat_id)  # 三个 worker 都预热，续轮不冷启动
        path = inject(opener, chat_id, hist,
                      initiate=(kind == "cold_gap" and not context), context=context)
        _record_scene_turn()
        return {"action": "scene_open", "kind": kind, "who": opener,
                "inbox": path, "scene_budget": SCENE_TURNS}
    if st.get("last_moment_seen_ts") != seen0:  # 朋友圈触发因无人可用作废，但检查点已推进：落盘，免得每 tick 重触发
        _save_state(chat_id, st)
    return {"action": "idle"}


def run(chat_id: str = CHAT_ID) -> None:
    """常驻主循环。launchd KeepAlive 拉活。"""
    print(f"[director] 启动，chat={chat_id}，开关={'开' if switch_on(chat_id) else '关'}", flush=True)
    while True:
        try:
            r = tick(chat_id)
            if r["action"] not in ("idle", "off", "debounce", "locked",
                                   "scene_pace", "scene_wait", "scene_skip"):
                print(f"[director] {json.dumps(r, ensure_ascii=False)}", flush=True)
        except Exception as e:
            print(f"[director] tick 异常: {e}", flush=True)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    import sys
    if "--run" in sys.argv:
        run()
    else:  # 默认只看一步决策，不写任何东西之外的副作用
        hist = read_group_history()
        print(f"读到 {len(hist)} 条去重后群历史，当前 heat={compute_heat(hist)}")
        print(json.dumps(decide(history=hist), ensure_ascii=False, indent=2))
