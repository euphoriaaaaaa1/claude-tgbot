"""读 bot 最近的 assistant 出站消息（反重复用）。

数据源不是引擎自己的 heartbeat_log（那是注入情境，不是真实输出），
而是 worker session jsonl（bot 实际发出去的消息）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chat_history


def recent_assistant_messages(bot_channel_path: str, days: int = 3, limit: int = 10) -> list[str]:
    return chat_history.get_recent_assistant_messages(bot_channel_path, days=days, limit=limit)


def recent_topic_collision(recent_msgs: list[str], situation, wildcard, world, hours: int = 2) -> bool:
    """模糊判断：若情境的关键词已在最近 N 小时被 bot 提过，视为话题碰撞。

    对接到 prefilter——若返回 True 就 SKIP，避免重复同一话题。
    """
    if not recent_msgs:
        return False

    # 收集情境关键词
    kw = []
    if situation.sporadic:
        name = situation.sporadic.get("name", "") or situation.sporadic.get("event_name", "")
        if name:
            kw.append(name)
    if situation.hobby and situation.hobby.get("name"):
        kw.append(situation.hobby["name"])
    if wildcard and wildcard.card:
        # 取 wildcard 卡里的前 6 字作模糊匹配
        kw.append(wildcard.card[:6])
    if world and world.matched_news:
        for n in world.matched_news[:1]:
            kw.append(n["title"][:8])

    if not kw:
        return False

    # 在最近消息里看
    recent_text = " ".join(recent_msgs)
    for k in kw:
        if k and k in recent_text:
            return True
    return False


def event_likely_mentioned(bot_channel_path: str, event: dict, hours: int = 24) -> bool:
    """读最近 N 小时 assistant 消息，模糊匹配事件关键词。"""
    if not event:
        return False
    msgs = chat_history.get_recent_assistant_messages(bot_channel_path, days=2, limit=30)
    keywords = event.get("keywords", []) or [event.get("name", "")]
    text = " ".join(msgs)
    for kw in keywords:
        if kw and kw in text:
            return True
    return False
