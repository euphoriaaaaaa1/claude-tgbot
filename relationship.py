#!/usr/bin/env python3
"""关系数值系统：每个 bot 一份 relationship.json，四维 0-100。

- affection 好感：整体喜欢/亲近程度
- trust     信任：愿意交心/卸下防备的程度（决定关系阶段解锁）
- desire    nsfw：亲密欲望被撩起的程度（没到就不主动露骨、亲密要铺垫）
- energy    精力：体力/兴致（低=累/困→对亲密没兴致，会拒绝或敷衍）→ 治"随叫随到"

数值由 jiwen tick 的裁判(deepseek_delta)按对话涨跌 + energy 随时段漂移。
describe() 把数值翻成给 worker 的行为提示，注入到回复上下文里，让 bot 按当前状态拿捏。

纯函数为主，便于离线测试；文件 IO 独立。
"""
from __future__ import annotations
import json
import os
import time
import hashlib
import random as _random
from datetime import datetime

try:
    import rng  # 真随机源(chance/uniform)——性欲阵发尖峰用
except Exception:  # pragma: no cover
    import random as rng  # type: ignore  # 兜底：标准库(chance 不存在时下方自处理)

STATS = ("affection", "trust", "desire", "energy")

# 陌生人阶段(如陈露露)：几乎白纸，欲望锁 0，精力满
DEFAULTS_STRANGER = {"affection": 5, "trust": 5, "desire": 0, "energy": 85}
# 已确立亲密关系(老 bot)：好感信任高，欲望中性(要被撩才起)，精力满
DEFAULTS_ESTABLISHED = {"affection": 90, "trust": 88, "desire": 35, "energy": 85}


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, float(v)))


def _path(bot_dir: str) -> str:
    return os.path.join(os.path.expanduser("~/.claude/channels"), bot_dir, "relationship.json")


def default_stats(profile: str = "established") -> dict:
    base = DEFAULTS_STRANGER if profile == "stranger" else DEFAULTS_ESTABLISHED
    return dict(base)


def load(bot_dir: str, profile: str = "established") -> dict:
    """读 relationship.json；不存在则返回该 profile 的默认(不落盘)。"""
    try:
        with open(_path(bot_dir), encoding="utf-8") as f:
            d = json.load(f)
        return {k: _clamp(d.get(k, default_stats(profile)[k])) for k in STATS}
    except Exception:
        return default_stats(profile)


def save(bot_dir: str, stats: dict) -> None:
    """写回数值 + 派生的 stage/prompt_snippet(供 worker 读)。原子落地。"""
    s = {k: _clamp(stats.get(k, 0)) for k in STATS}
    stage, snippet = describe(s)
    out = {**s, "stage": stage, "prompt_snippet": snippet, "updated_ts": int(time.time())}
    p = _path(bot_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def apply_delta(stats: dict, delta: dict) -> dict:
    """把裁判算出的涨跌应用到数值(clamp 0-100)。"""
    return {k: _clamp(stats.get(k, 0) + float(delta.get(k, 0))) for k in STATS}


# ─── energy：昼夜基线 + 当日活动消耗/恢复 + 随机(fable 调研) ──────────
# 关键结论：夜晚是 NSFW 主场，精力要托高，只有后半夜 3-6 点真低谷。
def _circadian_base(h: int) -> float:
    """昼夜基线曲线(晚上托高、午后 dip、后半夜唯一硬低谷)。"""
    if 0 <= h < 3:   return 48.0   # 前半夜转低但仍可为
    if 3 <= h < 6:   return 30.0   # 后半夜=唯一硬低谷(真该睡)
    if 6 <= h < 8:   return 55.0   # 刚醒渐入状态
    if 8 <= h < 12:  return 82.0   # 上午清爽峰
    if 12 <= h < 14: return 68.0   # 午饭
    if 14 <= h < 16: return 60.0   # 午后困顿 dip
    if 16 <= h < 19: return 72.0   # 回升
    if 19 <= h < 23: return 80.0   # 清醒维持区 = NSFW 主场，最高托位
    return 64.0                    # 23-24 前半夜


# 当前活动对精力的消耗/恢复(复用 situation 的 activity.state)
_STATE_ENERGY_MOD = {
    "free": 0.0, "busy_other": -8.0, "busy_work": -15.0,
    "busy_class": -15.0, "sleeping": 40.0,
}


def _block_noise(now: datetime, amp: float = 8.0, salt: str = "") -> float:
    """按 bot+日期+4小时块 做种子的慢变随机(窗口内稳定,±amp)——'今天莫名有点累/状态不错'。
    salt=bot 标识 → 每个 bot 各自随机，不再四个 bot 集体同步(否则全一样)。"""
    seed = int(hashlib.md5(f"{salt}-{now.date().isoformat()}-{now.hour // 4}".encode()).hexdigest()[:8], 16)
    return _random.Random(seed).uniform(-amp, amp)


def _event_energy_mod(event: dict | None) -> float:
    """生病/例假等突发事件额外扣精力——先用 mood_delta*30 当代理(只扣不加)。"""
    if not event:
        return 0.0
    try:
        return min(0.0, float(event.get("mood_delta", 0)) * 30.0)
    except (TypeError, ValueError):
        return 0.0


def energy_target(now: datetime, activity_state: str | None = None,
                  event: dict | None = None, salt: str = "") -> float:
    """精力目标 = 昼夜基线 + 当前活动消耗/恢复 + 突发事件 + 随机扰动(按 bot 各自随机)。"""
    v = _circadian_base(now.hour)
    v += _STATE_ENERGY_MOD.get(activity_state or "free", 0.0)
    v += _event_energy_mod(event)
    v += _block_noise(now, salt=salt)
    return _clamp(v)


def drift_energy(stats: dict, now: datetime, mins: float,
                 activity_state: str | None = None, event: dict | None = None,
                 salt: str = "") -> dict:
    """向 target 漂移：**恢复慢(0.18/min≈休息2h回大半)、消耗快(0.40/min)**——有过程，不瞬间吸附。"""
    cur = stats.get("energy", 80)
    tgt = energy_target(now, activity_state, event, salt=salt)
    rate = 0.18 if tgt > cur else 0.40
    step = min(abs(tgt - cur), rate * mins)
    return {**stats, "energy": _clamp(cur + step * (1 if tgt > cur else -1))}


def _rng_chance(p: float) -> bool:
    try:
        return rng.chance(p)      # type: ignore[attr-defined]
    except Exception:
        return _random.random() < p


def _rng_uniform(lo: float, hi: float) -> float:
    try:
        return rng.uniform(lo, hi)
    except Exception:
        return _random.uniform(lo, hi)


def drift_desire(stats: dict, now: datetime, mins: float,
                 affection: float = 50.0, event: dict | None = None,
                 profile: str = "established") -> dict:
    """nsfw：情境压制 + 阵发自发尖峰(治'突然想要') + 平时向基线冷却。不写死、有随机。"""
    des = stats.get("desire", 0)
    # 1) 情境压制：例假/生病在场 → 压顶并略降
    if event and any(k in (event.get("name", "") or "") for k in ("例假", "生病", "感冒", "痛经", "姨妈")):
        return {**stats, "desire": _clamp(min(des, 35.0) - 5.0)}
    # 2) 阵发自发尖峰：夜里概率略高、后半夜低、陌生人几乎不自发、好感越高越易起
    h = now.hour
    night = 1.6 if (h >= 19 or h < 1) else (0.4 if 3 <= h < 6 else 1.0)
    rel = 1.2 if profile != "stranger" else 0.25
    aff = 0.6 + affection / 100.0
    p_spike = min(0.30, 0.0015 * mins * night * rel * aff)  # 按分钟计，日均约 1~2 次
    if _rng_chance(p_spike):
        return {**stats, "desire": _clamp(des + _rng_uniform(15.0, 30.0))}
    # 3) 平时向基线冷却(余温稍留)
    baseline = 15.0
    if des > baseline:
        des = max(baseline, des - 0.12 * mins)
    return {**stats, "desire": _clamp(des)}


# ─── 数值 → 给 worker 的行为提示 ──────────────────────────────────
def _trust_stage(trust: float) -> str:
    if trust < 25:  return "陌生防备"
    if trust < 45:  return "客气试探"
    if trust < 65:  return "愿意多聊"
    if trust < 82:  return "亲近暧昧"
    return "交心亲密"


def describe(stats: dict) -> tuple[str, str]:
    """返回 (阶段名, 注入给 worker 的行为提示)。"""
    aff, tru, des, ene = (stats.get(k, 0) for k in STATS)
    stage = _trust_stage(tru)
    lines = [f"【当前关系状态】好感{aff:.0f}/信任{tru:.0f}/nsfw{des:.0f}/精力{ene:.0f}，处于「{stage}」阶段。按此拿捏分寸："]

    # 信任阶段门槛
    if tru < 25:
        lines.append("- 还很生分：只客气聊表面，不交心、不主动暴露私事，绝不亲密。")
    elif tru < 45:
        lines.append("- 客气试探：可以多聊几句、透露一点点自己，但保持距离，别热络。")
    elif tru < 65:
        lines.append("- 愿意多聊：能分享私事和心境，语气软一些，但还没到暧昧。")
    elif tru < 82:
        lines.append("- 亲近暧昧：暧昧、调情可以解锁，称呼软化；但仍矜持，别一步到位。")
    else:
        lines.append("- 交心亲密：可以完全放开、亲密无间。")

    # nsfw门槛
    if des < 30:
        lines.append(f"- nsfw低({des:.0f})：亲密欲望没起来，**不主动露骨**；对方直接要亲密会觉得突兀，需要先被撩、有情境才可能。")
    elif des < 60:
        lines.append(f"- nsfw中({des:.0f})：有点意思了，但要铺垫、要对方continue撩，别自己冲太快。")
    else:
        lines.append(f"- nsfw高({des:.0f})：亲密欲望上来了，可以顺势推进。")

    # 精力门槛(治随叫随到)——拒绝要"温柔+补偿"，别冷脸；偶尔拒才真实，别常态拒
    if ene < 25:
        lines.append(f"- 精力很低({ene:.0f})：真的累/困。对方要亲密大概率**温柔婉拒并给补偿**"
                     "('今天累坏了…抱抱我明天好不好''困得睁不开眼了，改天补给你'），撒娇拒绝、别冷脸、别硬配合。")
    elif ene < 50:
        lines.append(f"- 精力偏低({ene:.0f})：有点乏。亲密兴致不高，可能半推半就、希望简短温柔点，别自己太主动。")
    # 精力充足(≥50)：不因精力拒；正常时段该配合就配合，别没事找借口拒（常态拒最扫兴）

    lines.append("（这些是你此刻的真实状态，不是必须照做的剧本——结合人设自然表现，别把数值念出来；"
                 "拒绝也要像真人一样偶尔、温柔、留余地，不是每次都拒。）")
    return stage, "\n".join(lines)
