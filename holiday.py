"""节假日判定（含调休）。

策略：chinese_calendar 库为主（预编译国务院数据），库失效时联网查 timor.tech
API 兜底 + 持久化缓存。所有 caller 应通过本模块的 is_workday()，不要直接用
chinese_calendar——避免 2027 年库未更新时全代码崩。

缓存：~/.claude/dispatcher/.holiday-cache.json
源：https://timor.tech/api/holiday/year/<year>
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request
from datetime import date


CACHE_PATH = os.path.expanduser("~/.claude/dispatcher/.holiday-cache.json")
BACKUP_PATH = CACHE_PATH + ".backup"
DEFAULT_PROXY = "http://127.0.0.1:7897"


def _load_cache() -> dict:
    """优先 cache，损坏时读 backup（防三层全失败时五一上课 bug 回归）"""
    for path in (CACHE_PATH, BACKUP_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _save_cache(d: dict):
    """原子写 cache 同时写一份 backup（用于 cache 损坏时兜底）"""
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.rename(tmp, CACHE_PATH)
    # 同步 backup（任意一份完整即可救场）
    try:
        import shutil
        shutil.copy(CACHE_PATH, BACKUP_PATH)
    except Exception:
        pass


def _fetch_year_from_api(year: int, proxy: str = DEFAULT_PROXY) -> dict:
    """从 timor.tech 拉一年的节假日数据。

    返回 {date_str: is_holiday_bool}。timor.tech 返回结构：
      data["holiday"]["01-01"] = {"holiday": true, "name": "元旦", ...}
    只列入了**节假日和调休**两类日期，普通工作日和周末不在返回里。
    """
    url = f"https://timor.tech/api/holiday/year/{year}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        )
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=15) as r:
        data = json.load(r)
    holiday_map = {}
    if data.get("code") == 0:
        for k, v in (data.get("holiday") or {}).items():
            try:
                mm, dd = k.split("-")
                ds = f"{year}-{int(mm):02d}-{int(dd):02d}"
                holiday_map[ds] = bool(v.get("holiday"))
            except Exception:
                continue
    return holiday_map


def _ensure_year_in_cache(year: int) -> dict:
    """确保某年数据在缓存里。返回该年 holiday_map，失败返 None。"""
    cache = _load_cache()
    year_str = str(year)
    if year_str in cache:
        return cache[year_str]
    try:
        m = _fetch_year_from_api(year)
        cache[year_str] = m
        _save_cache(cache)
        sys.stderr.write(f"[holiday] 已缓存 {year} 节假日 ({len(m)} 项)\n")
        return m
    except Exception as e:
        sys.stderr.write(f"[holiday] 拉 {year} 失败：{e}\n")
        return None


def is_workday(d: date) -> bool:
    """是否工作日（含调休）。

    优先级：chinese_calendar 库 → timor.tech 缓存 → weekday() 兜底。
    """
    # 1. chinese_calendar 库
    try:
        import chinese_calendar
        return chinese_calendar.is_workday(d)
    except Exception:
        pass

    # 2. timor.tech 缓存
    year_cache = _ensure_year_in_cache(d.year)
    if year_cache is not None:
        ds = d.strftime("%Y-%m-%d")
        if ds in year_cache:
            # cache 标识 holiday=True 即假期，否则调休工作日
            return not year_cache[ds]
        # 不在 cache 里 → 普通日子，按 weekday 判
        return d.weekday() < 5

    # 3. 库 + 网络全失效 → weekday() 兜底
    return d.weekday() < 5


def is_holiday(d: date) -> bool:
    """是否节假日（含调休带来的休息日）"""
    return not is_workday(d)
