#!/usr/bin/env python3
"""手动给某 bot 触发一条朋友圈（绕过心跳静默期/上限/judge）。

用法：python3 force_moment.py <bot_id>

注意：仍走真实 LLM 调用，会消耗配额。
"""
import sys
import os
from datetime import datetime
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_loader
import db
from generators import situation as sit_mod
from generators import world as world_mod
from generators import wildcard as wc_mod
from generators.mood import mood_at


Situation = namedtuple("Situation", ["recurring", "sporadic", "hobby"])
Recurring = namedtuple("Recurring", ["name", "description", "state"])


def main():
    """
    用法：
      force_moment.py <bot_id>                  - 用真实情境（没素材不发，跟生产一样）
      force_moment.py <bot_id> --inject-hobby   - 测试用，从 yml 随机抽一个爱好注入
    """
    if len(sys.argv) < 2:
        print("usage: force_moment.py <bot_id> [--inject-hobby]", file=sys.stderr)
        sys.exit(1)

    bot_id = sys.argv[1]
    inject_hobby = "--inject-hobby" in sys.argv
    cfg = config_loader.load_bot(bot_id)
    cfg["_bot_id"] = bot_id
    g = config_loader.load_global()
    now = datetime.now()

    # 收集真实情境（和心跳同源）
    sit = sit_mod.collect(bot_id, now, cfg)
    world = world_mod.collect_world(cfg, now)
    wildcard = wc_mod.pick_today_wildcard(bot_id, now)
    mood, mood_factors = mood_at(now, bot_id, sit, world, cfg)

    # --inject-hobby: 测试模式，从 yml 随机抽一个长期爱好注入
    if inject_hobby and not sit.hobby:
        import secrets
        hobbies = cfg.get("personal_hobbies", [])
        if hobbies:
            picked = secrets.SystemRandom().choice(hobbies)
            picked = {**picked, "kind": "long_term"}
            sit = Situation(sit.recurring, sit.sporadic, picked)
            print(f"  [测试] 注入长期爱好: {picked.get('name')} - {picked.get('effect')}")

    # --inject-obsession: 测试模式，从 DB 短期痴迷里随机抽一个注入
    if "--inject-obsession" in sys.argv and not sit.hobby:
        obs = db.list_active_obsessions(bot_id)
        if obs:
            import secrets
            picked = secrets.SystemRandom().choice(obs)
            picked_dict = {
                "name": picked["name"], "effect": picked["effect"],
                "kind": "obsession", "theme": picked.get("theme"),
            }
            sit = Situation(sit.recurring, sit.sporadic, picked_dict)
            print(f"  [测试] 注入短期痴迷: [{picked.get('theme')}] {picked['name']}")

    # 直接强制发，绕过 silence/limit
    g_moments = (g.get("moments") or {}).copy()
    g_moments["silence_threshold_minutes"] = 0    # 关静默
    g_force = {**g, "moments": g_moments}
    cfg_force = {**cfg, "moments_daily_limit": 999}  # 关上限

    print(f"\n--- {bot_id} 当前情境 ---")
    print(f"活动: {sit.recurring.name} ({sit.recurring.description})")
    print(f"事件: {sit.sporadic and sit.sporadic.get('name')}")
    print(f"爱好: {sit.hobby and sit.hobby.get('name')}")
    print(f"卡片: {wildcard and wildcard.card}")
    print(f"心情: {mood:.2f} {mood_factors}")
    print(f"特殊日: {world.is_special_date if world else False}")
    print(f"新闻命中: {len(world.matched_news) if world else 0}")

    from moments.post import maybe_post_moment
    mid = maybe_post_moment(bot_id, cfg_force, now, sit, world, wildcard,
                            mood, mood_factors, 999, g_force)

    if mid:
        m = db.get_moment(mid)
        print(f"\n✅ 朋友圈 id={mid} ({m['visibility']})")
        print(f"   {m['text']}")
    else:
        print("\n⚠️ 未生成（可能 LLM 输出空 / 触发独白闸 / 无触发种类）")


if __name__ == "__main__":
    main()
