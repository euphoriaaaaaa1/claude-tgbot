"""L1 真实世界数据流。

包含：
- 真实日期 / 农历 / 二十四节气 / 节日
- 天气（OpenWeather 免 key 接口或 wttr.in）
- RSS 命中（按 bot interest_keywords 过滤）
- 历史上的今天（wikipedia 公共 API）

每个子项独立 try/except，单源失败不影响整体。
"""
import os
import sys
import math
import hashlib
import requests
import feedparser
from datetime import datetime
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db


# 二十四节气名（按公历近似）
SOLAR_TERMS = {
    1: ["小寒", "大寒"], 2: ["立春", "雨水"], 3: ["惊蛰", "春分"],
    4: ["清明", "谷雨"], 5: ["立夏", "小满"], 6: ["芒种", "夏至"],
    7: ["小暑", "大暑"], 8: ["立秋", "处暑"], 9: ["白露", "秋分"],
    10: ["寒露", "霜降"], 11: ["立冬", "小雪"], 12: ["大雪", "冬至"],
}
SOLAR_TERM_DAYS = {  # 月份 → [上旬节气日, 下旬节气日]，简化用 6 和 21 近似
    1: [6, 20], 2: [4, 19], 3: [6, 21], 4: [5, 20], 5: [6, 21], 6: [6, 21],
    7: [7, 23], 8: [8, 23], 9: [8, 23], 10: [8, 24], 11: [8, 22], 12: [7, 22],
}

# 公历节日
SOLAR_FESTIVALS = {
    (1, 1): "元旦", (2, 14): "情人节", (3, 8): "妇女节", (3, 12): "植树节",
    (4, 1): "愚人节", (5, 1): "劳动节", (5, 4): "青年节",
    (6, 1): "儿童节", (7, 1): "建党节", (8, 1): "建军节",
    (9, 10): "教师节", (10, 1): "国庆节",
    (12, 24): "平安夜", (12, 25): "圣诞节",
}


@dataclass
class WorldSnapshot:
    date_info: dict = field(default_factory=dict)
    weather: dict | None = None
    rss_items: list = field(default_factory=list)
    matched_news: list = field(default_factory=list)
    on_this_day: list = field(default_factory=list)

    @property
    def is_special_date(self) -> bool:
        di = self.date_info
        return bool(di.get("festival") or di.get("solar_term") or di.get("lunar_festival"))


def collect_world(cfg: dict, now: datetime) -> WorldSnapshot:
    snap = WorldSnapshot()
    snap.date_info = _collect_date_info(now)
    snap.weather = _try(_fetch_weather, cfg.get("city", ""))
    snap.rss_items = _try(_fetch_rss, cfg.get("rss_feeds", [])) or []
    snap.matched_news = _filter_by_interest(snap.rss_items, cfg.get("interest_keywords", []))
    snap.on_this_day = _try(_fetch_on_this_day, now) or []
    return snap


# ─── 日期/节气/节日 ─────────────────────────────────────────
def _collect_date_info(now: datetime) -> dict:
    info = {
        "weekday": now.strftime("%A"),
        "weekday_zh": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "date": now.strftime("%Y-%m-%d"),
        "lunar": _try(_lunar_str, now),
        "solar_term": _check_solar_term(now),
        "festival": SOLAR_FESTIVALS.get((now.month, now.day)),
        "lunar_festival": _try(_check_lunar_festival, now),
    }
    return info


def _check_solar_term(now: datetime) -> str | None:
    days = SOLAR_TERM_DAYS.get(now.month, [])
    names = SOLAR_TERMS.get(now.month, [])
    if not days or not names:
        return None
    # 节气当天或前后 1 天都返回（强相关）
    for i, d in enumerate(days):
        if abs(now.day - d) <= 1:
            return names[i]
    return None


def _lunar_str(now: datetime) -> str:
    try:
        import lunardate
        ld = lunardate.LunarDate.fromSolarDate(now.year, now.month, now.day)
        return f"农历{_chinese_month(ld.month)}{_chinese_day(ld.day)}"
    except Exception:
        return None


def _chinese_month(m: int) -> str:
    names = ["", "正月", "二月", "三月", "四月", "五月", "六月",
             "七月", "八月", "九月", "十月", "冬月", "腊月"]
    return names[m] if 1 <= m <= 12 else f"{m}月"


def _chinese_day(d: int) -> str:
    if d == 10:
        return "初十"
    if d == 20:
        return "二十"
    if d == 30:
        return "三十"
    tens = d // 10
    units = d % 10
    if tens == 0:
        return f"初{['', '一','二','三','四','五','六','七','八','九'][units]}"
    if tens == 1:
        return f"十{['', '一','二','三','四','五','六','七','八','九'][units]}"
    if tens == 2:
        return f"廿{['', '一','二','三','四','五','六','七','八','九'][units]}"
    return f"{tens}十{units}"


def _check_lunar_festival(now: datetime) -> str | None:
    try:
        import lunardate
        ld = lunardate.LunarDate.fromSolarDate(now.year, now.month, now.day)
        lf = {(1, 1): "春节", (1, 15): "元宵节", (5, 5): "端午节",
              (7, 7): "七夕", (8, 15): "中秋节", (9, 9): "重阳节",
              (12, 8): "腊八节"}
        return lf.get((ld.month, ld.day))
    except Exception:
        return None


# ─── 天气 ──────────────────────────────────────────────────
def _fetch_weather(city: str) -> dict | None:
    if not city:
        return None
    # 用 wttr.in 免 key
    try:
        r = requests.get(f"https://wttr.in/{city}?format=j1", timeout=8,
                         headers={"User-Agent": "claudebotlife/1.0"})
        r.raise_for_status()
        data = r.json()
        cur = data.get("current_condition", [{}])[0]
        main = cur.get("weatherDesc", [{}])[0].get("value", "")
        return {
            "main": main,
            "temp": float(cur.get("temp_C", 0)),
            "humidity": float(cur.get("humidity", 0)),
            "feels_like": float(cur.get("FeelsLikeC", 0)),
            "desc_zh": _weather_zh(main),
        }
    except Exception:
        return None


def _weather_zh(main: str) -> str:
    m = main.lower()
    if "rain" in m or "drizzle" in m:
        return "下雨"
    if "thunder" in m:
        return "雷阵雨"
    if "snow" in m:
        return "下雪"
    if "clear" in m or "sun" in m:
        return "晴"
    if "cloud" in m or "overcast" in m:
        return "多云"
    if "fog" in m or "mist" in m:
        return "雾"
    return main


# ─── RSS ───────────────────────────────────────────────────
def _fetch_rss(feeds: list[str]) -> list[dict]:
    items = []
    for url in feeds[:6]:
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent": "claudebotlife/1.0"})
            if r.status_code != 200:
                continue
            parsed = feedparser.parse(r.content)
            for entry in parsed.entries[:5]:
                title = (entry.get("title", "") or "").strip()
                summary = (entry.get("summary", "") or "").strip()[:200]
                if not title:
                    continue
                h = hashlib.md5((url + title).encode()).hexdigest()
                if db.is_news_seen(h):
                    continue
                items.append({
                    "title": title,
                    "summary": summary,
                    "source": url,
                    "hash": h,
                })
        except Exception:
            continue
    return items[:20]


def _filter_by_interest(items: list[dict], keywords: list[str]) -> list[dict]:
    if not keywords:
        return items[:3]  # 没配兴趣词就返回前 3 条
    matched = []
    for item in items:
        text = (item["title"] + " " + item["summary"]).lower()
        for kw in keywords:
            if kw.lower() in text:
                matched.append(item)
                db.mark_news_seen(item["hash"])
                break
    return matched[:3]


# ─── 历史上的今天 ──────────────────────────────────────────
def _fetch_on_this_day(now: datetime) -> list[dict]:
    """用 wikipedia 公共 API。"""
    try:
        url = (f"https://api.wikimedia.org/feed/v1/wikipedia/zh/onthisday/events/"
               f"{now.month:02d}/{now.day:02d}")
        r = requests.get(url, timeout=8,
                         headers={"User-Agent": "claudebotlife/1.0"})
        r.raise_for_status()
        data = r.json()
        events = data.get("events", [])[:3]
        return [{"year": e.get("year"), "text": e.get("text", "")[:100]} for e in events]
    except Exception:
        return []


# ─── 工具 ──────────────────────────────────────────────────
def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None
