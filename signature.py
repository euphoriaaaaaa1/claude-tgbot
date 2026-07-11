"""个性签名（QQ 风格）—— bot 自决要不要换。

调用时机：每次心跳通过 prefilter 后调一次（不论 judge speak 与否）。
省钱机制：心情没波动 + 距上次更新 < 24h + 无新事件 → 跳过 LLM。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import quota
from claude_cli import call_claude_json


SIG_PROMPT = """你是 {persona}。

当前心情：{mood_label}（{mood_factors}）
最近发生：{recent}
现在的个性签名：「{current_sig}」

像 QQ 个性签名那样，**自己决定**现在要不要换：
- 喜欢的歌词/电影台词/书里看到的金句
- 一句吐槽 / 调侃 / 心情碎片
- 一个最近一直在想的画面 / 词
- 5-30 字，**不是事件叙述**，是一句"挂在那里"的话

如果当前签名还合心情、没什么想换的 → 不换。

只输出 JSON（不要 markdown）：
{{"new_sig": "..." 或 null, "why": "为什么换/不换，10字内"}}
"""


def maybe_update_signature(bot_id: str, bot_cfg: dict,
                            mood: float, mood_factors: list,
                            sporadic: dict | None,
                            wildcard, world,
                            min_interval_hours: int = 24,
                            force: bool = False) -> str | None:
    """心跳里调用。返回新签名（如果换了）或 None。

    force=True 时跳过省钱前置筛 + prompt 要求必须给新签名（用于 bootstrap）。
    """
    profile = db.get_profile(bot_id)
    cur_sig = profile.get("signature") or bot_cfg.get("bio") or ""
    last_update = profile.get("sig_updated_at") or 0
    hours_since = (time.time() - last_update) / 3600 if last_update else 999
    db_has_sig = bool(profile.get("signature"))

    # bootstrap：DB 还没存过签名时，强制更新一次
    if not db_has_sig:
        force = True

    # 省钱前置筛：分两层 cooldown
    # - 完全无 trigger：24h cooldown（min_interval_hours）
    # - 有 trigger（mood 波动 / sporadic / wildcard / special_date）：仍要 4h 软 cooldown
    #   旧版 trigger 命中就直接绕过 cooldown → mood 多数时间在 <0.3 或 >0.7 → trigger
    #   几乎一直 True → 每次心跳都调 LLM。实测 5h 17 次调用，0 次实际更新 DB（LLM
    #   决定签名不需要换），纯浪费 quota。
    has_trigger = (
        sporadic is not None
        or (wildcard is not None)
        or (world and world.is_special_date)
        or mood < 0.30 or mood > 0.70   # 心情波动大才换
    )
    SOFT_COOLDOWN_HOURS = 4
    if not force:
        if not has_trigger and hours_since < min_interval_hours:
            return None
        if has_trigger and hours_since < SOFT_COOLDOWN_HOURS:
            return None

    # 调 LLM
    mood_label = _mood_label(mood)
    recent = _recent_desc(sporadic, wildcard, world)

    prompt = SIG_PROMPT.format(
        persona=bot_cfg.get("persona_summary", ""),
        mood_label=mood_label,
        mood_factors=", ".join(mood_factors[:3]),
        recent=recent,
        current_sig=cur_sig or "(还没设)",
    )
    if force:
        prompt += "\n\n注意：你必须给一个新签名（即使心情平稳，也按人格挑一句喜欢的话），不能返回 null。"
    # 走 claude_cli 自动分流：sonnet 模式 → 真 Claude haiku，deepseek 模式 → 直连 HTTP
    result = None
    last_err = None
    for attempt in range(2):
        try:
            result = call_claude_json(prompt, timeout=30, model="haiku")
            break
        except Exception as e:
            last_err = e
            continue
    if result is None:
        sys.stderr.write(f"[{bot_id}] 签名更新失败（2 次重试）：{last_err}\n")
        return None
    try:
        quota.record_call("signature", 1)
        new_sig = result.get("new_sig")
        if not new_sig or not isinstance(new_sig, str):
            return None
        new_sig = new_sig.strip()
        if not new_sig or new_sig == cur_sig or len(new_sig) > 50:
            return None
        db.set_signature(bot_id, new_sig)
        return new_sig
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] 签名更新失败：{e}\n")
        return None


def _mood_label(m):
    if m >= 0.70: return "心情不错"
    if m >= 0.55: return "心情还行"
    if m >= 0.40: return "心情平稳"
    if m >= 0.25: return "有点低落"
    return "情绪低落"


def _recent_desc(sp, wc, w):
    parts = []
    if sp: parts.append(f"{sp.get('name','')} - {sp.get('effect','')[:30]}")
    if wc: parts.append(f"今日触动：{wc.card[:40]}")
    if w and w.is_special_date:
        di = w.date_info
        sp_name = di.get("festival") or di.get("lunar_festival") or di.get("solar_term")
        if sp_name: parts.append(f"特殊日：{sp_name}")
    return " / ".join(parts) or "无特别事件"
