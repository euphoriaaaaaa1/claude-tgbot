"""L4 因果心情曲线。

不是纯数学曲线——心情有因可查：
  base 0.5 + 弱昼夜节律 + 持续中事件 mood_delta + 天气 + 节日 + 疲倦 + 微随机
返回 (心情值 [0,1], 影响因子说明列表)，喂给 LLM 时附带说明，避免凭空假"今天不开心"。
"""
import math
from datetime import datetime, time as dtime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rng


def mood_at(now: datetime, bot_id: str, situation, world, cfg) -> tuple[float, list[str]]:
    factors = []
    base = 0.5

    # 1. 弱昼夜节律（±0.05）
    daily = 0.05 * math.sin(2 * math.pi * (now.hour - 6) / 24)
    base += daily
    factors.append(f"昼夜{daily:+.2f}")

    # 2. 持续中事件 mood_delta
    if situation.sporadic and situation.sporadic.get("mood_delta"):
        d = float(situation.sporadic["mood_delta"])
        base += d
        factors.append(f"{situation.sporadic['name']}{d:+.2f}")

    # 3. 天气
    if world and world.weather:
        d = _weather_mood_delta(world.weather)
        if d:
            base += d
            factors.append(f"天气{d:+.2f}")

    # 4. 节日
    if world and world.date_info.get("festival"):
        base += 0.10
        factors.append("节日+0.10")

    # 5. 疲倦累积
    sp = compute_sleep_pressure(now, cfg)
    if sp > 0.05:
        base -= sp
        factors.append(f"疲倦-{sp:.2f}")

    # 6. 微随机（"今天就是有点低气压"）
    n = rng.uniform(-0.05, 0.05)
    base += n

    return _clamp(base, 0, 1), factors


def _weather_mood_delta(weather: dict) -> float:
    """根据天气结构推测心情影响。weather 期望含 'main' 字段（OpenWeather 风格）。"""
    if not weather:
        return 0.0
    main = (weather.get("main") or "").lower()
    if "rain" in main or "drizzle" in main or "thunder" in main:
        return -0.10
    if "snow" in main:
        return 0.05  # 下雪有点新鲜感
    if "clear" in main:
        return 0.05
    if "cloud" in main:
        return -0.02
    # 闷热（temp>32 + humidity>70）
    if weather.get("temp", 0) > 32 and weather.get("humidity", 0) > 70:
        return -0.05
    return 0.0


def compute_sleep_pressure(now: datetime, cfg) -> float:
    """距上一次 sleep_hours 结束多久 / 16，封顶 0.20。"""
    sleep_end = _last_sleep_end(now, cfg.get("sleep_hours", []))
    if sleep_end is None:
        return 0.0
    delta = now - sleep_end
    if delta.total_seconds() < 0:
        return 0.0
    hours_awake = delta.total_seconds() / 3600
    return min(hours_awake / 16 * 0.20, 0.20)


def _last_sleep_end(now: datetime, sleep_hours: list[str]) -> datetime | None:
    """从 sleep_hours 列表（如 ['00:00-07:30']）算"上次睡眠结束时间"。
    取最近的一个 sleep_hours 区间结束时间（今天的或昨天的）。
    """
    if not sleep_hours:
        return None
    candidates = []
    for spec in sleep_hours:
        try:
            start_s, end_s = spec.split("-")
            end_t = _parse_time(end_s)
        except Exception:
            continue
        # 今天的结束时间
        end_today = now.replace(hour=end_t.hour, minute=end_t.minute,
                                 second=0, microsecond=0)
        # 昨天的结束时间
        end_yesterday = end_today - timedelta(days=1)
        if end_today <= now:
            candidates.append(end_today)
        candidates.append(end_yesterday)
    if not candidates:
        return None
    # 取最近的（今天的优先；今天的 sleep 还没结束就用昨天的）
    valid = [c for c in candidates if c <= now]
    return max(valid) if valid else None


def _parse_time(s: str) -> dtime:
    s = s.strip()
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
