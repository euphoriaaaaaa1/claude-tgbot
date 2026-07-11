#!/usr/bin/env python3
"""每周给每个 bot 抽 1-2 个新的短期痴迷（current_obsessions）。

每周日跑一次（在 cleanup 03:00 plist 里 piggyback，或单独 cron）。
- 删除已过期 obsessions
- 给每个 enabled bot 用 LLM 按人设生成 1-2 个新的（默认持续 14 天）

用法：python3 refresh_obsessions.py
"""
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config_loader
import quota
from claude_cli import call_claude_json


OBSESSION_PROMPT = """你帮一个 AI 角色生成本周的"短期痴迷"——ta 这阵子突然迷上的事/在追的内容/最近的小爱好。

【角色】
{persona}

【现有的长期爱好】（不要重复这些）
{long_term_list}

【角色已有的短期痴迷】（不要重复）
{current_list}

【今日】{date}（{weekday_zh}）

请生成 2 条新的"短期痴迷"。要求：
- 每条都是**具体**的事（不要"想看剧"这种空话，要"在追《XX》"）
- 符合角色人设和当下季节/月份
- 持续 1-3 周内 ta 会反复想做/聊到的
- theme 是 1-2 字的主题分类（剧/书/烹饪/旅游/游戏/穿搭/收藏/学习等）

严格只输出 JSON 数组，不要 markdown：
[{{"name": "在追《XX》", "effect": "今晚还想看一集；嘴里念叨某角色", "theme": "剧"}}, ...]
"""


WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def refresh_for_bot(bot_cfg: dict) -> int:
    bot_id = bot_cfg["_bot_id"]
    persona = bot_cfg.get("persona_summary", "(未提供人设摘要)")

    long_term = bot_cfg.get("personal_hobbies", [])
    long_list = "; ".join(h.get("name", "") for h in long_term) or "(无)"

    current = db.list_active_obsessions(bot_id)
    current_list = "; ".join(o["name"] for o in current) or "(无)"

    now = datetime.now()
    prompt = OBSESSION_PROMPT.format(
        persona=persona,
        long_term_list=long_list,
        current_list=current_list,
        date=now.strftime("%Y-%m-%d"),
        weekday_zh=WEEKDAY_ZH[now.weekday()],
    )

    try:
        # JSON 列表（claude_cli call_claude_json 默认期望 dict，需要手动）
        from claude_cli import call_claude
        raw = call_claude(prompt, timeout=90, model="sonnet")
        quota.record_call("obsession_gen", 1)
        items = _parse_json_array(raw)
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] obsession 生成失败：{e}\n")
        return 0

    n = 0
    for item in items[:2]:  # 每个 bot 最多 2 条
        name = (item.get("name") or "").strip()
        effect = (item.get("effect") or "").strip()
        theme = (item.get("theme") or "").strip()
        if not name or not effect:
            continue
        db.add_obsession(bot_id, name, effect, theme, duration_days=14)
        print(f"  [{bot_id}] +{name} ({theme}) - {effect[:40]}")
        n += 1
    return n


def _parse_json_array(raw: str) -> list:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"): s = s[4:]
        s = s.strip().rstrip("`").strip()
    if "[" in s and "]" in s:
        s = s[s.index("["): s.rindex("]") + 1]
    data = json.loads(s)
    return data if isinstance(data, list) else []


def main():
    db.cleanup_expired_obsessions()
    bots = config_loader.list_enabled_bots()
    print(f"为 {len(bots)} 个 bot 刷新短期痴迷...")
    total = 0
    for cfg in bots:
        total += refresh_for_bot(cfg)
    print(f"总计新增 {total} 条短期痴迷")


if __name__ == "__main__":
    main()
