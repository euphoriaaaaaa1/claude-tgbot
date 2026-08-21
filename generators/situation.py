"""L3a/3b/3c 情境采样：常驻活动、突发事件、爱好冲动。

- L3a 常驻活动：必然发生（人设的固定生活节奏），按时间表查
- L3b 突发事件：真随机触发（SystemRandom），可持续多小时；状态门控
- L3c 爱好冲动：仅 free 时段触发，真随机，间隔 ≥4h
"""
import os
import sys
import yaml
from datetime import datetime, time as dtime, timedelta
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rng
import db


WEEKDAY_MAP = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class Activity:
    name: str
    description: str
    state: str  # "free" | "busy_class" | "busy_work" | "busy_other" | "sleeping"
    # 学期状态，取 school_calendar 的四个常量之一。末位带默认值：既有的
    # Activity(name=..., description=..., state=...) 三参数构造照常可用。
    term: str = "in_session"


@dataclass
class SituationContext:
    recurring: Activity
    sporadic: dict | None = None  # 数据库行 dict 或 events.yaml 项
    hobby: dict | None = None     # configs/<bot>.yml 的 personal_hobbies 项


# ─── L3a 常驻活动 ──────────────────────────────────────────
_NO_KEY = object()
_BREAK_TERMS = ("summer_break", "winter_break")   # 寒暑假共用一套 break 作息

# 作息表配置错误的进程内去重：键为 (bot 标识, 字段名)。带 bot 标识是为了让多 bot
# 同进程时各自都报得出来。只打 bot 标识 + 字段名——configs 里有 token 和 chat_id，
# 绝不回显配置值。
_WARNED_SCHEDULE: set[tuple[str, str]] = set()


def _warn_schedule(cfg: dict, field: str, detail: str) -> None:
    """同一 bot 的同一处作息表配置错误只报一次，防每 tick 刷屏。"""
    bot_id = str(cfg.get("id") or "?")
    key = (bot_id, field)
    if key in _WARNED_SCHEDULE:
        return
    _WARNED_SCHEDULE.add(key)
    sys.stderr.write("[situation] %s: %s %s\n" % (bot_id, field, detail))


def _current_term(cfg: dict, now: datetime) -> str:
    """当前学期状态。求值出任何岔子都退回 in_session 走老路径——
    新功能不能把主链路弄崩（崩了整个 tick 会退化成 FALLBACK，bot 全哑）。"""
    try:
        import school_calendar
        return school_calendar.term_state(cfg, now.date())
    except Exception:
        return "in_session"


def _schedule_block(ra: dict, key: str, cfg: dict) -> list | None:
    """取一个作息块。键缺失静默返回 None；值不是 list 则报一次警后同样当缺失。"""
    value = ra.get(key, _NO_KEY)
    if value is _NO_KEY:
        return None
    if isinstance(value, list):
        return value
    _warn_schedule(cfg, "recurring_activities.%s" % key, "不是列表，该作息块已忽略")
    return None


def _pick_schedule(cfg: dict, now: datetime, term: str) -> list:
    """按学期状态挑作息表；挑不到就返回空列表，由调用方降级。"""
    ra = cfg.get("recurring_activities", {})
    if not isinstance(ra, dict):
        _warn_schedule(cfg, "recurring_activities", "不是字典，整块作息表已忽略")
        return []
    if term in _BREAK_TERMS:
        # 缺 break 不替补 weekend：各 bot 的 weekend 描述都字面含"周末"，暑假的
        # 周五套上去只是把一种错话换成另一种，宁可降级到中性的"自由时间/睡觉中"
        return _schedule_block(ra, "break", cfg) or []
    if term == "early_return":
        early = _schedule_block(ra, "early_return", cfg)
        if early:
            return early   # 缺专用作息才替补下面的 weekday/weekend
    # 法定节假日（春节/五一/国庆/清明 等含调休）走 weekend
    # holiday.is_workday 内部三级 fallback：chinese_calendar → timor.tech 缓存 → weekday()
    import holiday
    weekday_key = "weekday" if holiday.is_workday(now.date()) else "weekend"
    return _schedule_block(ra, weekday_key, cfg) or []


def get_current_recurring(cfg: dict, now: datetime) -> Activity:
    """根据时间表查当前 bot 应该在做的活动。

    先定学期状态，据此挑作息表（见 _pick_schedule）；选中一张表后不再回头翻别的键。
    sleep_hours 优先：未在 schedule 命中时，先看是否在 sleep 时段，
    避免凌晨/睡前/起床前等空隙被错误 fallback 成"自由时间"。
    """
    term = _current_term(cfg, now)
    for activity in _pick_schedule(cfg, now, term):
        # 条目不是 dict（yml 写歪了）就当不命中，继续看下一条，不连累整个 tick
        if not isinstance(activity, dict):
            continue
        if _time_in_range(now.time(), activity.get("when", "")):
            return Activity(
                name=activity.get("name", "未知"),
                description=activity.get("description", ""),
                state=activity.get("state", "free"),
                term=term,
            )
    # fallback 前先检查 sleep_hours
    import config_loader
    if config_loader.in_sleep_hours(cfg, now):
        return Activity(name="睡觉中", description="在睡觉，被吵醒会迷糊",
                        state="sleeping", term=term)
    return Activity(name="自由时间", description="没什么特别的事", state="free", term=term)


# ─── L3b 突发事件（真随机）─────────────────────────────────
def _adjust_cycle_event_prob(event: dict, bot_id: str, now: datetime,
                              base_prob: float) -> float:
    """周期事件（events.yaml 标 cycle_days）按距上次时间调整概率。

    保证一月一次：
      - phase < 0.85（不到 ~24 天）→ 0（绝对不重复来）
      - phase 0.85-1.0（24-28 天）→ base（自然窗口）
      - phase 1.0-1.2（28-33 天）→ 0.5（该来还没来，催）
      - phase > 1.2（>33 天）→ 1.0（必触发，防永远不来）
    """
    cycle_days = event.get("cycle_days")
    if not cycle_days:
        return base_prob   # 非周期事件，原样
    last_started = db.get_last_event_started(bot_id, event["name"])
    if last_started is None:
        return base_prob   # 首次出现，允许 base 概率
    days_since = (now.timestamp() - last_started) / 86400
    phase = days_since / cycle_days
    if phase < 0.85:
        return 0.0
    elif phase < 1.0:
        return base_prob
    elif phase < 1.2:
        return 0.5
    else:
        return 1.0


def get_or_sample_sporadic(bot_id: str, now: datetime, cfg: dict) -> dict | None:
    # 优先返回持续中的
    ongoing = db.get_ongoing_event(bot_id, now)
    if ongoing:
        return ongoing

    # 距上次掷 >= 6h 才掷新的
    if db.hours_since_last_roll(bot_id) < 6:
        return None
    db.update_last_roll(bot_id, now)

    current_state = get_current_recurring(cfg, now).state
    enabled_categories = set(cfg.get("event_categories", []))

    events = _load_sporadic_events()
    for event in events:
        # 类别启用判断
        if event.get("category") and event["category"] not in enabled_categories:
            continue
        # 状态门控
        allowed = event.get("allowed_activity_states", ["any"])
        if "any" not in allowed and current_state not in allowed:
            continue
        # 时间约束
        if not _matches_constraints(event.get("constraints"), now):
            continue
        # 周期事件按距上次时间调整概率（例假等）
        prob = _adjust_cycle_event_prob(event, bot_id, now,
                                         float(event.get("probability_per_roll", 0)))
        # 真随机掷骰
        if rng.chance(prob):
            duration_hours = event.get("duration_hours", [0, 0])
            if event.get("duration") == "instant" or not duration_hours:
                expires_at = now + timedelta(minutes=1)  # 瞬时事件，下次心跳就过期
            else:
                lo, hi = duration_hours
                expires_at = now + timedelta(hours=rng.uniform(lo, hi))
            db.create_ongoing_event(bot_id, event, expires_at=expires_at)
            return {
                "event_name": event["name"],
                "name": event["name"],
                "effect": event.get("effect", ""),
                "mood_delta": event.get("mood_delta", 0.0),
                "started_at": int(now.timestamp()),
            }
    return None


# ─── L3c 爱好冲动（真随机）─────────────────────────────────
# 长期爱好（yml personal_hobbies）+ 短期痴迷（current_obsessions 表）
# 触发顺序：先短期（高概率，0.20/条），没命中再尝试长期（yml 各自 0.03-0.06）
OBSESSION_PROB_PER_ROLL = 0.20    # 短期痴迷单次掷骰概率


def sample_hobby_if_free(bot_id: str, now: datetime, cfg: dict) -> dict | None:
    if get_current_recurring(cfg, now).state != "free":
        return None
    # 默认 4h hobby cooldown，bot 可在 yml 覆盖（如 bot2 反差人妻独处反复想亲密 → 1h）
    cooldown_hours = float(cfg.get("hobby_cooldown_hours", 4))
    if db.hours_since_last_hobby(bot_id) < cooldown_hours:
        return None

    # 1) 优先：短期痴迷（DB current_obsessions，未过期的）
    obsessions = db.list_active_obsessions(bot_id)
    for ob in obsessions:
        if rng.chance(OBSESSION_PROB_PER_ROLL):
            db.log_hobby(bot_id, ob["name"], now)
            return {
                "name": ob["name"],
                "effect": ob["effect"],
                "kind": "obsession",
                "theme": ob.get("theme"),
            }

    # 2) Fallback：长期爱好（yml personal_hobbies）
    for hobby in cfg.get("personal_hobbies", []):
        if rng.chance(float(hobby.get("probability_per_roll", 0))):
            db.log_hobby(bot_id, hobby.get("name", ""), now)
            return {
                "name": hobby.get("name", ""),
                "effect": hobby.get("effect", ""),
                "kind": "long_term",
            }
    return None


# ─── 统一入口 ──────────────────────────────────────────────
def collect(bot_id: str, now: datetime, cfg: dict) -> SituationContext:
    return SituationContext(
        recurring=get_current_recurring(cfg, now),
        sporadic=get_or_sample_sporadic(bot_id, now, cfg),
        hobby=sample_hobby_if_free(bot_id, now, cfg),
    )


# ─── 工具 ──────────────────────────────────────────────────
_EVENTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "events.yaml",
)


def _load_sporadic_events() -> list[dict]:
    if not os.path.exists(_EVENTS_PATH):
        return []
    with open(_EVENTS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, list) else []


def _parse_time(s: str) -> dtime:
    s = s.strip()
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _time_in_range(t: dtime, range_str: str) -> bool:
    """range_str 形如 '08:00-12:00'。支持跨午夜（22:00-04:00）。"""
    if not range_str:
        return False
    try:
        start_s, end_s = range_str.split("-")
        start = _parse_time(start_s)
        end = _parse_time(end_s)
    except Exception:
        return False
    if start <= end:
        return start <= t <= end
    # 跨午夜
    return t >= start or t <= end


def _matches_constraints(c: dict | None, now: datetime) -> bool:
    if not c:
        return True
    # days: ["mon","tue",...]
    if "days" in c:
        if WEEKDAY_MAP[now.weekday()] not in c["days"]:
            return False
    # hours: "23:00-04:00"
    if "hours" in c:
        if not _time_in_range(now.time(), c["hours"]):
            return False
    # weekday_only: "weekday" / "weekend"
    if "weekday_only" in c:
        wo = c["weekday_only"]
        is_wd = now.weekday() < 5
        if wo == "weekday" and not is_wd:
            return False
        if wo == "weekend" and is_wd:
            return False
    return True
