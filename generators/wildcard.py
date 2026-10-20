"""L2 当日 wildcard 卡片采样。

每天 0:15 由 daily-wildcard.py 生成；这里只负责"当日卡池里挑一张未用的"。
不是每次都中——按时间均匀分布，避免一开始就用完。
"""
import os
import sys
from datetime import datetime
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rng
import db


@dataclass
class WildcardPick:
    card_id: int
    card: str
    emotion: str


def pick_today_wildcard(bot_id: str, now: datetime) -> WildcardPick | None:
    date_str = now.strftime("%Y-%m-%d")
    cards = db.get_today_wildcards(bot_id, date_str)
    unused = [c for c in cards if not c["used"]]
    if not unused:
        return None
    # 按"剩余卡数"控制触发概率：剩余 5 张 → 每次触发概率 1/5 * 0.3 = 6%
    # 这样一天大概用 1-3 张
    # 触发率：今天还没用过任一张 → 高概率触发；已用过 → 低概率
    used_today = len([c for c in cards if c["used"]])
    threshold = 0.6 if used_today == 0 else 0.15
    if not rng.chance(threshold):
        return None
    pick = rng.choice(unused)
    db.mark_wildcard_used(bot_id, date_str, pick["card_id"])
    return WildcardPick(card_id=pick["card_id"], card=pick["card"], emotion=pick["emotion"])
