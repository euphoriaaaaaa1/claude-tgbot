"""节假日判定（含调休）。

策略：chinese_calendar 库为主（预编译国务院数据），库没有的年份读 timor.tech
持久化缓存，缓存也没有则按周几。is_workday/is_holiday 纯离线、永不抛；
唯一联网入口是 refresh_years()（主动消息链路每次调一下，或 CLI --refresh）。
所有 caller 应通过本模块的 is_workday()，不要直接用 chinese_calendar——
避免 2027 年库未更新时全代码崩。

缓存：~/.claude/dispatcher/.holiday-cache.json
  {"2027": {"2027-01-01": true, ...},            # 值=是否放假（true=放假，false=调休上班）；年份键只在拉到非空表时写入
   "_neg": {"2027": {"kind": "empty"|"net_fail", "at": epoch, "until": epoch}}}
源：https://timor.tech/api/holiday/year/<year>
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime

import platform_lock


CACHE_PATH = os.path.expanduser("~/.claude/dispatcher/.holiday-cache.json")
BACKUP_PATH = CACHE_PATH + ".backup"
LOCK_PATH = CACHE_PATH + ".lock"
DEFAULT_PROXY = "http://127.0.0.1:7897"
FETCH_TIMEOUT_SEC = 15
NEG_TTL_EMPTY_SEC = 86400
NEG_TTL_NET_FAIL_SEC = 3600


class HolidayApiError(Exception):
    """timor.tech 接口返回 code≠0。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _load_cache() -> dict:
    """优先 cache，损坏/顶层非字典时读 backup；都不行按空字典。永不抛。"""
    for path in (CACHE_PATH, BACKUP_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _save_cache(d: dict):
    """cache 与 backup 各自走 <path>.tmp.<pid> + os.replace 原子替换。

    任一步 OSError 原样抛给调用方（refresh_years 据此记 net_fail）；残留 tmp 尽力删。
    """
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    payload = json.dumps(d, ensure_ascii=False, indent=2)
    for path in (CACHE_PATH, BACKUP_PATH):
        tmp = path + ".tmp.%d" % os.getpid()
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _year_table(cache: dict, year: int) -> dict | None:
    """该年的非空表；键不存在、不是字典或为空表都视为没有。"""
    t = cache.get(str(year)) if isinstance(cache, dict) else None
    return t if isinstance(t, dict) and t else None


def _neg_until(cache: dict, year: int) -> int | None:
    """该年负缓存的 until（epoch 秒）；没有或格式坏返回 None。"""
    try:
        return int(cache["_neg"][str(year)]["until"])
    except Exception:
        return None


def _fetch_year_from_api(year: int, proxy: str = DEFAULT_PROXY) -> dict:
    """从 timor.tech 拉一年的节假日数据。

    返回 {date_str: is_holiday_bool}（True=放假；False=调休上班）。timor.tech 返回结构：
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
    with opener.open(req, timeout=FETCH_TIMEOUT_SEC) as r:
        data = json.load(r)
    code = data.get("code")
    if code != 0:
        raise HolidayApiError(code)
    holiday_map = {}
    for k, v in (data.get("holiday") or {}).items():
        try:
            mm, dd = k.split("-")
            ds = f"{year}-{int(mm):02d}-{int(dd):02d}"
        except Exception:
            continue
        if v.get("holiday"):
            name = (v.get("name") or "").strip()
            # 具名节日存名字（供 is_holiday 判"具名"）；无名的放假日照旧存 True
            holiday_map[ds] = name if name else True
        else:
            holiday_map[ds] = False  # 调休上班
    return holiday_map


def _lib_covers(year: int) -> bool:
    """chinese_calendar 是否有该年数据（无则 is_workday 抛 NotImplementedError）。"""
    try:
        import chinese_calendar
        chinese_calendar.is_workday(date(year, 1, 1))
        return True
    except Exception:
        return False


def is_workday(d: date) -> bool:
    """是否工作日（含调休）。纯离线：chinese_calendar 库 → 缓存 → weekday()。永不抛。"""
    # 1. chinese_calendar 库
    try:
        import chinese_calendar
        return chinese_calendar.is_workday(d)
    except Exception:
        pass

    # 2. 缓存（只读、不加锁；表值 True=放假，False=调休上班）
    try:
        table = _year_table(_load_cache(), d.year)
        if table is not None:
            ds = d.strftime("%Y-%m-%d")
            if ds in table:
                return not bool(table[ds])
    except Exception:
        pass

    # 3. 不在表内 / 无数据 → 按周几
    return d.weekday() < 5


def is_holiday(d: date) -> bool:
    """是否节假日（含调休带来的休息日）。保留 bool 语义（接口 C 锁定）。"""
    return not is_workday(d)


def named_holiday(d: date):
    """当天是否为【具名节日】。是 → 返回节日名（str，如 "中秋"）；否则 → None。

    供 director 判"具名节日"（F-16）：普通工作日、普通周末、调休上班、以及查得到放假但
    无节日名的日子都返回 None——周末不再被当成特殊日。纯离线、永不抛、永不联网：
    先查缓存（具名节日的表值是名字字符串），再查 chinese_calendar 的 get_holiday_detail。
    与 is_holiday 分开，避免改动 is_holiday 的 bool 契约（接口 C 依赖它）。
    """
    ds = d.strftime("%Y-%m-%d")
    # 1. 缓存：具名节日的表值是名字字符串；无名假日/调休是 True/False
    try:
        table = _year_table(_load_cache(), d.year)
        if table is not None and ds in table:
            v = table[ds]
            return v if (isinstance(v, str) and v) else None
    except Exception:
        pass
    # 2. chinese_calendar 库：get_holiday_detail → (是否放假, 节日名)
    try:
        import chinese_calendar
        is_hol, name = chinese_calendar.get_holiday_detail(d)
        if is_hol and isinstance(name, str) and name:
            return name
    except Exception:
        pass
    # 3. 无数据 / 无节日名 → 不是具名节日
    return None


def _classify(cache: dict, pending: list, now_i: int, results: dict) -> list:
    """按缓存把待处理年份记成 cached / neg_skip，返回还需联网的年份。"""
    todo = []
    for y in pending:
        if _year_table(cache, y) is not None:
            results[y] = "cached"
            continue
        until = _neg_until(cache, y)
        if until is not None and now_i < until:
            results[y] = "neg_skip"
        else:
            todo.append(y)
    return todo


def _merge_and_save(todo: list, fetched: dict, neg: dict) -> None:
    """锁内重读文件并合并后落盘，不以内存态为准。OSError 原样抛。

    合并规则：年份键以非空者为准（本次拉到的表 > 别的进程刚写的表 > 空键删除）；
    同年 _neg 取 until 大者；本次写入年份键则删同年 _neg。
    """
    disk = _load_cache()
    merged = dict(disk)
    disk_neg = disk.get("_neg")
    merged_neg = dict(disk_neg) if isinstance(disk_neg, dict) else {}
    for y in todo:
        key = str(y)
        if y in fetched:
            merged[key] = fetched[y]
            merged_neg.pop(key, None)
        elif _year_table(disk, y) is None:
            merged.pop(key, None)   # 旧代码遗留的空表年份键，趁本次落盘删掉
    for y, (kind, at, until) in neg.items():
        if y in fetched:
            continue
        old = _neg_until(disk, y)
        if old is not None and old >= until:
            continue                # 别的进程已写了更晚的负缓存，取大者
        merged_neg[str(y)] = {"kind": kind, "at": at, "until": until}
    if merged_neg:
        merged["_neg"] = merged_neg
    else:
        merged.pop("_neg", None)
    _save_cache(merged)


def _save_failed(exc: BaseException, years: list, results: dict) -> None:
    """落盘失败契约：需落盘的年份一律 net_fail，stderr 恰好一行，负缓存不写。"""
    sys.stderr.write("[holiday] save failed: %s\n" % type(exc).__name__)
    for y in years:
        results[y] = "net_fail"


def refresh_years(years: list, now: float | None = None) -> dict:
    """唯一联网入口：把库没有的年份拉进缓存。永不抛。

    每年一个结果码：lib | cached | fetched | neg_skip | empty | net_fail（见接口 C）。
    整段持 LOCK_PATH 的非阻塞 flock，拿不到锁所有待处理年份 neg_skip、不联网不写盘。
    年份参数 int() 不了的跳过；意外异常时还没出结果的年份一律 net_fail。
    """
    results = {}
    try:
        _refresh(years, now, results)
    except Exception as e:
        sys.stderr.write("[holiday] refresh failed: %s\n" % type(e).__name__)
        try:
            for y in years:
                try:
                    results.setdefault(int(y), "net_fail")
                except Exception:
                    continue
        except Exception:
            pass
    return results


def _refresh(years: list, now, results: dict) -> None:
    """refresh_years 的主体；结果写进 results，异常交给外层兜底。"""
    pending = []
    for y in years:
        try:
            y = int(y)
        except (TypeError, ValueError, OverflowError):
            continue
        if _lib_covers(y):
            results[y] = "lib"
        else:
            pending.append(y)
    if not pending:
        return
    now_i = int(time.time() if now is None else now)
    # 跨平台非阻塞锁（T03 platform_lock）：拿不到 → 所有待处理年份 neg_skip，不联网不写盘。
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        lock_cm = platform_lock.file_lock(LOCK_PATH, blocking=False)
        lock_cm.__enter__()
    except platform_lock.LockBusy:
        results.update({y: "neg_skip" for y in pending})
        return
    except (platform_lock.LockError, OSError) as e:
        _save_failed(e, _classify(_load_cache(), pending, now_i, results), results)
        return
    try:
        todo = _classify(_load_cache(), pending, now_i, results)
        fetched, neg = {}, {}
        for y in todo:
            try:
                table = _fetch_year_from_api(y)
            except HolidayApiError:
                table = {}
            except Exception:
                results[y] = "net_fail"
                neg[y] = ("net_fail", now_i, now_i + NEG_TTL_NET_FAIL_SEC)
                continue
            if table:
                results[y] = "fetched"
                fetched[y] = table
            else:
                results[y] = "empty"
                neg[y] = ("empty", now_i, now_i + NEG_TTL_EMPTY_SEC)
        if fetched or neg:
            try:
                _merge_and_save(todo, fetched, neg)
            except OSError as e:
                _save_failed(e, todo, results)
    finally:
        lock_cm.__exit__(None, None, None)


def _cli(argv: list) -> int:
    """python3 holiday.py --refresh <year>：stdout 一行结果码。

    退出码 0：lib / cached / fetched / neg_skip（neg_skip 附 _neg.until 本地时间或 locked）；
    退出码 1：empty / net_fail；用法错误 2。
    """
    if len(argv) != 2 or argv[0] != "--refresh" or not argv[1].isdigit():
        sys.stderr.write("usage: holiday.py --refresh <year>\n")
        return 2
    year = int(argv[1])
    code = refresh_years([year])[year]
    if code == "neg_skip":
        until = _neg_until(_load_cache(), year)
        when = datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S") if until else "locked"
        print("%s %s" % (code, when))
    else:
        print(code)
    return 0 if code in ("lib", "cached", "fetched", "neg_skip") else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
