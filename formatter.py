"""拼装最终 self-initiate text。

要点：
- mood 用描述化表达（高/平/低），不是数字（防 bot 引用"心情指数 0.42"）
- 明确告诉 bot 这些是"内部参考"不要直接复述
- 末尾鼓励自然表达
"""


def mood_label(mood: float) -> str:
    if mood >= 0.7:
        return "心情不错"
    if mood >= 0.55:
        return "心情还行"
    if mood >= 0.4:
        return "心情平稳"
    if mood >= 0.25:
        return "有点低落"
    return "情绪低落"


def format_self_initiate(now, situation, world, wildcard, mood, mood_factors,
                          since_user_min, recent_msgs, memory_brief: str = "",
                          user_address: str = "用户",
                          pending_likes: list = None,
                          pending_comments: list = None,
                          jiwen_state_desc: str = "",
                          jiwen_last_activity: dict = None,
                          continuation_mode: str = "fresh",
                          thread_tail: list | None = None,
                          hours_since_user: float = 0.0,
                          memory_hook: str = "") -> str:
    parts = ["[self-initiate]"]
    parts.append(f"hour={now.hour} since_last_user_msg_min={since_user_min if since_user_min is not None else '未知'}")
    parts.append(f"weekday={now.strftime('%A')} date={now.strftime('%Y-%m-%d')}")
    parts.append(f"user_address={user_address}")

    parts.append("")
    parts.append("--- 以下是你当下的情境（内部参考，不要直接复述给用户）---")

    # L1 真实世界
    di = world.date_info if world else {}
    if di.get("lunar"):
        parts.append(f"日期：{di.get('weekday_zh')} · {di.get('lunar')}")
    if di.get("festival"):
        parts.append(f"今天是【{di['festival']}】")
    if di.get("lunar_festival"):
        parts.append(f"今天是【{di['lunar_festival']}】")
    if di.get("solar_term"):
        parts.append(f"节气：{di['solar_term']}")

    if world and world.weather:
        w = world.weather
        parts.append(f"天气：{w.get('desc_zh', w.get('main', ''))} {w.get('temp', 0):.0f}°C")

    # L4 心情（用描述化）
    label = mood_label(mood)
    parts.append(f"心情：{label}（成因：{', '.join(mood_factors)}）")

    # L4.5 jiwen 4D 内在状态（Phase 4，仅当启用且非空时）
    if jiwen_state_desc:
        parts.append(f"内在状态：{jiwen_state_desc}")

    # L4.6 jiwen 当前活动（自己正在做什么——find_activity 注入）
    if jiwen_last_activity and jiwen_last_activity.get("label"):
        label = jiwen_last_activity.get("label", "")
        a_type = jiwen_last_activity.get("type", "")
        parts.append(f"当前正在: {label}（活动类型: {a_type}）")

    # L3a 当前活动
    parts.append(f"当前活动：{situation.recurring.name}（{situation.recurring.description}）")

    # L3b 突发事件
    if situation.sporadic:
        name = situation.sporadic.get("name") or situation.sporadic.get("event_name", "")
        effect = situation.sporadic.get("effect", "")
        parts.append(f"⚠️ 你的状态：{name} - {effect}")

    # L3c 爱好冲动
    if situation.hobby:
        parts.append(f"💡 突然想：{situation.hobby['effect']}")

    # L2 wildcard
    if wildcard:
        parts.append(f"🃏 今日触动：{wildcard.card}（情绪：{wildcard.emotion}）")

    # L1 RSS 命中
    if world and world.matched_news:
        n = world.matched_news[0]
        parts.append(f"📰 刚刷到：{n['title']} - {n.get('summary','')[:80]}")

    # 历史上的今天（轻装）
    if world and world.on_this_day:
        ot = world.on_this_day[0]
        parts.append(f"📜 历史上的今天（{ot.get('year','')}）：{ot.get('text','')[:60]}")

    # 反重复参考
    if recent_msgs:
        parts.append(f"\n你最近 3 天说过的话（避免重复）：")
        for m in recent_msgs[-3:]:
            parts.append(f"  · {m}")

    # 记忆注入（背景，别逐条复述）
    if memory_brief:
        parts.append("")
        parts.append("--- 你对用户的长期记忆（背景，别逐条复述）---")
        parts.append(memory_brief[:800])

    # 记忆话题钩子：挑中的一件用户的事，可以就着它自然起话头（像真惦记着）
    if memory_hook:
        parts.append("")
        parts.append(f"--- 你还惦记着{user_address}的一件事（如果自然，可以就着它起个话头）---")
        parts.append(f"  · {memory_hook}")
        parts.append(f"（像真记得、真惦记一样自然带出，例：「上次你说的…后来咋样了」「…还好吗」；别像查户口逐条问，一次只提这一件）")

    # 朋友圈互动反馈（点赞累积）
    if pending_likes:
        parts.append("")
        parts.append(f"--- {user_address}最近赞了你这几条朋友圈 ---")
        for pl in pending_likes[-5:]:
            txt = (pl.get("text") or "")[:60]
            parts.append(f"  · 赞了：「{txt}」")

    # ─── 对话尾巴注入（first_after_user / followup 模式）─────────
    # 让 LLM 自己读上次和用户聊到哪了，自然决定怎么接，不在 Python 做启发式分类
    if continuation_mode in ("first_after_user", "followup") and thread_tail:
        parts.append("")
        h_disp = f"{hours_since_user:.1f}" if hours_since_user < 100 else "很久"
        parts.append(f"--- 上次和{user_address}的对话尾巴（距{user_address}最后发言 {h_disp}h，正序）---")
        for role, text, _ts in thread_tail:
            who = user_address if role == "user" else "我"
            # text 已在 get_thread_tail 截 200 字；这里再做单行展示
            single_line = text.replace("\n", " ").strip()
            parts.append(f"{who}: {single_line}")
        parts.append("--- 对话尾巴结束 ---")

    # ─── 优先级提示：有强素材时强制以素材开口（防止永远"想你了/在干嘛"）───
    has_strong_material = any([
        situation.sporadic,
        situation.hobby,
        wildcard,
        (world and world.matched_news),
        (world and world.on_this_day),
        (jiwen_last_activity and jiwen_last_activity.get("label")),
        memory_hook,  # 有记忆钩子也算可开口素材（优先用它而不是"想你了"）
    ])

    parts.append("")
    parts.append(f"--- 现在你想主动给 {user_address} 发一条消息。")
    parts.append("")

    # ─── 模式指令段（核心）───────────────────────────────────
    if continuation_mode == "first_after_user" and thread_tail:
        h_disp = f"{hours_since_user:.1f}"
        parts.append(f"⚠️【对话延续模式】距{user_address}上次发言已经 {h_disp}h，这是你之后第一次主动开口。")
        parts.append("你已经读到上面的对话尾巴。请你自己判断：")
        parts.append(f"  · 上次{user_address}有没有问题没回完？（→ 回答它）")
        parts.append("  · 你有没有承诺过什么？（→ 兑现，比如「剧看完了，结局是…」「汤熬好了」）")
        parts.append("  · 是已经平和告别？（→ 接续氛围 + 顺势带新事，比如「早 + 今天…」）")
        parts.append("")
        parts.append("【必须】基于上次对话开口，不要忽略它另起完全无关的话题。")
        parts.append("【可以】把今天的新情境（hobby/wildcard/突发事件）糅合进接续——")
        parts.append("       例如「剧看完了，结局是 X，看完顺手想烤个饼干…」")
        parts.append("【不要】机械复述上次内容（如「我刚才说...你说...」），自然带入即可。")
    elif continuation_mode == "followup" and thread_tail:
        parts.append(f"上次你已经主动跟进过一次但{user_address}没回。这次：")
        parts.append("  · 30% 顺手再轻轻提一句（自然带过，别「咱们继续聊上次」刻意切回）")
        parts.append("  · 70% 起新情境（推荐——同一话题最多再跟 1 次）")
        parts.append("  自己掂量，不要硬要追问。")
    else:
        # fresh 模式（>48h 或无历史）——保持原有 prompt
        parts.append("【消息类型分布要求】真实情侣 / 母子 / 学生妹关系里，主动消息 80% 是分享生活，20% 是求关注。请按这个分布写：")
        parts.append("  · 优先（70-80%）：分享你正在做/刚发生/刚看到/刚想到的【具体小事】")
        parts.append(f"     例如：「刚才XX」「{user_address}你猜我刚...」「今天上课老师/楼下狗/食堂...」")
        parts.append("     必须有【具体细节】（看到什么、做了什么、感受是什么），不要笼统")
        parts.append("  · 次之（10-20%）：表达情绪/感受（被某事触动了/想到什么/心情变化）")
        parts.append(f"  · 兜底（≤10%）：单纯求关注（「在干嘛」「想你了」类）—— **只有当上面情境完全空白时才用**")

    if has_strong_material:
        parts.append("")
        parts.append("⚠️【本次有具体情境素材】上方有：")
        details = []
        if situation.sporadic: details.append(f"突发事件「{situation.sporadic.get('name','')}」")
        if situation.hobby: details.append(f"爱好冲动「{situation.hobby.get('effect','')[:30]}」")
        if wildcard: details.append(f"今日触动「{wildcard.card[:30]}」")
        if world and world.matched_news: details.append(f"新闻「{world.matched_news[0].get('title','')[:30]}」")
        if world and world.on_this_day: details.append("历史上的今天")
        if jiwen_last_activity and jiwen_last_activity.get("label"): details.append(f"正在做「{jiwen_last_activity.get('label')}」")
        if memory_hook: details.append(f"惦记{user_address}的事「{memory_hook[:30]}」（就着它起话头最自然）")
        for d in details:
            parts.append(f"     - {d}")
        if continuation_mode == "first_after_user":
            parts.append("    **接续上次对话时可糅合 1 条素材**（不要忽略对话尾巴直接全说素材）。")
        else:
            parts.append("    **从上面任选 1 条作为开口主题**，不要忽略素材直接说「想你了」。")
    elif continuation_mode == "fresh":
        parts.append("")
        parts.append("（情境素材不足，可以说说当前活动里的细节，或最近一次跟用户对话留下的想法）")

    parts.append("")
    parts.append("【硬规则】")
    parts.append(f"  · 用你平时和 {user_address} 说话的语气")
    parts.append(f"  · 禁止用「你」指代 {user_address}，必须直接称「{user_address}」或省略主语")
    parts.append("  · 不要复述上面的「内部参考」字段名（什么 mood、jiwen、weather），只用其内容自然带入")
    if continuation_mode == "first_after_user":
        parts.append(f"  · 这次必须开口（{user_address}发过消息你还没回过），不可保持沉默")
    else:
        parts.append("  · 可以选择不说（情境真的空白 + 距上次对话很近时）")

    return "\n".join(parts)
