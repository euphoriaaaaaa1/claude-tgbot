"""调用次数配额（5 小时滚动窗口）。

Max 订阅按"消息数 / 5h 窗口"限额。这里不精确知道 Anthropic 的具体阈值，
用保守的可配置上限。每种调用按 weight 计入：
- judge          weight=1   (轻量 Sonnet 调用)
- worker_trigger weight=5   (写 inbox 触发完整 worker session)
- moment_text    weight=1
- moment_image   weight=2   (NovelAI 调用经 claude --print，含 skill 加载)
- wildcard_gen   weight=1
- memory_compact weight=2

总计：~30 加权调用 / 5h 窗口默认上限。
"""
import time
import db

DEFAULT_MAX_CALLS_PER_5H = 30

WORKER_TRIGGER_WEIGHT = 5
JUDGE_WEIGHT = 1
WILDCARD_GEN_WEIGHT = 1
MEMORY_COMPACT_WEIGHT = 2
MOMENT_TEXT_WEIGHT = 1
MOMENT_IMAGE_WEIGHT = 2
SCENE_TURN_WEIGHT = 2   # 自主群聊每轮接话（热 worker 上的短回复，比冷启动 worker_trigger 便宜）


def check_quota(global_cfg: dict) -> str:
    """返回 'ok' | 'warn' | 'over'。"""
    cap = (global_cfg or {}).get("max_calls_per_5h", DEFAULT_MAX_CALLS_PER_5H)
    window_start = int(time.time()) - 5 * 3600
    weighted = db.sum_call_weight_since(window_start)
    if weighted >= cap:
        return "over"
    if weighted >= cap * 0.85:
        return "warn"
    return "ok"


def record_call(kind: str, weight: int):
    """记录一次调用消耗。"""
    db.insert_call_log(int(time.time()), kind, weight)


def current_usage(global_cfg: dict) -> dict:
    """返回当前 5h 窗口的使用情况。给 status.sh 用。"""
    cap = (global_cfg or {}).get("max_calls_per_5h", DEFAULT_MAX_CALLS_PER_5H)
    window_start = int(time.time()) - 5 * 3600
    used = db.sum_call_weight_since(window_start)
    return {
        "cap": cap,
        "used_weighted": used,
        "remaining": max(0, cap - used),
        "percent": round(used / cap * 100, 1) if cap else 0,
    }
