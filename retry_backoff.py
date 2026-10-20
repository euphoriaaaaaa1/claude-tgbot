"""共用的退避与失败计数纯函数（无 I/O、无状态）。

退避片段是四键 dict：fail_count / next_retry_at / last_error / halted。
收敛器、关系反思、导演各自持有片段并决定存放位置，这里只算数。
"""
import math


def _check_number(name, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是 int/float，得到 {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限数")


def _check_base_cap(base, cap):
    _check_number("base", base)
    _check_number("cap", cap)
    if base <= 0:
        raise ValueError("base 必须大于 0")
    if cap < base:
        raise ValueError("cap 不得小于 base")


def _clean_fail_count(state):
    n = state.get("fail_count") if isinstance(state, dict) else None
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return 0
    return n


def next_delay(fail_count, base, cap):
    """第 fail_count 次失败后的等待秒数：base * 2**(n-1)，封顶 cap，恒为 float。"""
    if isinstance(fail_count, bool) or not isinstance(fail_count, int):
        raise TypeError("fail_count 必须是 int")
    _check_base_cap(base, cap)
    if fail_count <= 0:
        return 0.0
    # 指数位足够大时直接封顶，避免 2**n 溢出成 OverflowError
    if fail_count - 1 >= 64:
        return float(cap)
    return float(min(cap, base * 2 ** (fail_count - 1)))


def on_failure(state, now, *, base, cap, max_failures=None, reason=""):
    """记一次失败，返回新的四键片段；不改入参，入参其他键不带出。"""
    _check_number("now", now)
    _check_base_cap(base, cap)
    if max_failures is not None:
        if isinstance(max_failures, bool) or not isinstance(max_failures, int):
            raise TypeError("max_failures 必须是 int 或 None")
        if max_failures < 1:
            raise ValueError("max_failures 必须 >= 1")
    n = _clean_fail_count(state) + 1
    halted = max_failures is not None and n >= max_failures
    return {
        "fail_count": n,
        "next_retry_at": None if halted else now + next_delay(n, base, cap),
        "last_error": reason if isinstance(reason, str) else str(reason),
        "halted": halted,
    }


def is_due(state, now):
    """是否可以再试：停止 → 否；next_retry_at 脏值 → 是；否则看时间。"""
    _check_number("now", now)
    if not isinstance(state, dict):
        return True
    if state.get("halted") is True:
        return False
    at = state.get("next_retry_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return True
    return now >= at


def cleared():
    return {"fail_count": 0, "next_retry_at": None, "last_error": None, "halted": False}
