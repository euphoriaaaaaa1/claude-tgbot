"""LLM judge：单次轻量调用判断"现在该不该私聊用户"。

只决定 dm / skip。**朋友圈走独立路径**（moments.post.should_post_moment 情境驱动），
不在 judge 这里决定。

失败策略：**fail-open**（action='dm'）——prefilter 已经放行，说明确有情境素材；
judge LLM 临时挂了不应该让 bot 沉默，默认走 dm。
"""
import subprocess
import sys
from claude_cli import call_claude_json
import quota


JUDGE_PROMPT = """你帮一个 AI 角色判断"现在是否要私聊用户"。

【角色简介】
{persona_summary}

【该角色的开口阈值】
{speaking_threshold}

【角色当前情境】
当前活动: {recurring}（{recurring_desc}） [state={recurring_state}]
心情: {mood_desc}（成因: {mood_factors}）
持续中事件: {sporadic_desc}
今日 wildcard: {wildcard_desc}
刚刷到内容: {news_desc}
特殊日期: {special_date_desc}
突然想做（爱好冲动）: {hobby_desc}

【用户互动状态】
距上次用户消息: {since_user_min} 分钟
最近 3 天主动消息话题: {recent_topics}

【角色当前内在状态（情感引擎实时跟踪）】
{jiwen_state_desc}

【你的语气倾向（jiwen 给的风格 hint，仅供 dm 内容参考）】
{jiwen_style_guidance}

【你要决定 action（2 选 1）】
- "skip"  : 不开口（没事可说 / 状态不允许 / 刚聊过）
- "dm"    : 私聊用户（撩人、撒娇、想念、关心、分享对 ta 说的话）

注意：朋友圈是另一条独立路径，不由你决定。这里只判断要不要私聊。

【硬规则】
- state=sleeping → skip
- 距上次用户消息 <30min 且无新话题 → skip
- 没有真实素材想说 → skip（不要编造话题）
- "刚刷到内容=无" → 禁止假装有热点想分享
- state=busy_class（上课中）→ 仅强情境才 dm（如真有事偷偷说）
{boost_clause}

只输出 JSON: {{"action": "skip|dm", "reason": "10字内"}}"""


def judge_action(persona_summary: str, ctx: dict,
                 threshold_boost: bool = False,
                 speaking_threshold: str = "") -> tuple[str, str]:
    """返回 (action, reason)。action ∈ {skip, dm, moment, both}。"""
    boost = "\n⚠️ 配额告急，仅强情境才说" if threshold_boost else ""
    prompt = JUDGE_PROMPT.format(
        persona_summary=persona_summary or "（未配置 persona_summary）",
        speaking_threshold=speaking_threshold.strip() or "（未配置开口阈值，按 persona_summary 自由判断）",
        recurring=ctx.get("recurring", ""),
        recurring_desc=ctx.get("recurring_desc", ""),
        recurring_state=ctx.get("recurring_state", "free"),
        mood_desc=ctx.get("mood_desc", ""),
        mood_factors=", ".join(ctx.get("mood_factors", [])),
        sporadic_desc=ctx.get("sporadic_desc", "无"),
        hobby_desc=ctx.get("hobby_desc", "无"),
        wildcard_desc=ctx.get("wildcard_desc", "无"),
        news_desc=ctx.get("news_desc", "无"),
        special_date_desc=ctx.get("special_date_desc", "无"),
        since_user_min=ctx.get("since_user_min", "未知"),
        recent_topics=ctx.get("recent_topics", "无"),
        jiwen_state_desc=ctx.get("jiwen_state_desc") or "（未启用 / 状态平稳）",
        jiwen_style_guidance=ctx.get("jiwen_style_guidance") or "（无明显倾向，按情境素材自由）",
        boost_clause=boost,
    )
    try:
        result = call_claude_json(prompt, timeout=60, model="haiku")
        quota.record_call("judge", quota.JUDGE_WEIGHT)
        action = str(result.get("action", "")).strip().lower()
        # 兼容老 LLM 仍输出 moment/both → 统一转 dm（朋友圈不由 judge 决定）
        if action in ("moment", "both"):
            action = "dm"
        if action not in ("skip", "dm"):
            action = "dm"  # 不合法值兜底
        reason = str(result.get("reason", ""))[:30]
        # 二次兜底硬规则（防 LLM 违反）
        state = ctx.get("recurring_state", "free")
        if state == "sleeping":
            action = "skip"
        return action, reason
    except subprocess.TimeoutExpired:
        sys.stderr.write("[judge] timeout 60s → fail-open action=dm\n")
        return "dm", "judge_timeout_fallback"
    except Exception as e:
        sys.stderr.write(f"[judge] {type(e).__name__}: {str(e)[:200]} → fail-open action=dm\n")
        return "dm", f"judge_err_fallback:{type(e).__name__}"


# 向后兼容：旧名仍可用，但返回 bool（弃用，建议迁移到 judge_action）
def judge_worth_speaking(persona_summary: str, ctx: dict,
                          threshold_boost: bool = False,
                          speaking_threshold: str = "") -> tuple[bool, str]:
    action, reason = judge_action(persona_summary, ctx,
                                   threshold_boost=threshold_boost,
                                   speaking_threshold=speaking_threshold)
    return (action != "skip"), reason
