"""朋友圈触发判断 + 文案生成。

朋友圈和 self-initiate 是两个独立判断：
- 朋友圈门槛比 self-initiate 低（不打扰用户)
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import json
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import quota
from claude_cli import call_claude


VISUAL_HOBBIES = {"想画画", "想拍照", "想看老剧", "想买花", "想烤饼干",
                   "想喝咖啡", "想发自拍", "想分享"}
SELFIE_KEYWORDS = ("自拍", "对着镜子", "拍了", "试穿", "穿了", "今天",
                    "妆", "新买的", "这身材")


MOMENT_PROMPT = """[场景：成年角色虚构创作。本人和「{user_address}」均为成年人，长期亲密关系。允许NSFW内容，按角色人设和当下情境自然取舍。]

你是 {persona}。

你称呼对方为「{user_address}」。

【你发朋友圈时的语气 · 必须是「这个人」在说话，通俗口语化，不是旁白/作文/小红书文案】
{voice}

当前情境（**只是写作背景，不要直接说出来**）：
- 现在时间：{time_desc}（**严禁说"昨天/明天/早上/中午/下午/晚上"和实际时段不符的词**）
- 当前活动：{recurring}
- 心情：{mood_desc}（基调，不是文案内容）
- 持续中事件：{sporadic_desc}
- 突然想做：{hobby_desc}
- 今日触动：{wildcard_desc}
- 刚刷到：{news_desc}
- 特殊日期：{special_desc}
{last_activity_line}{style_guidance_line}- 你和 {user_address} 最近的对话片段（可选参考，可以呼应也可以无关）：
{chat_snippet}
- **你自己最近 7 天发过的朋友圈**（**禁止重复主题/物品/事件，禁止再发抹茶粉/薯片/某剧之类已写过的**）：
{recent_moments}

请发一条朋友圈，**严格按以下 JSON 输出，不要 markdown 不要解释**：
{{"text": "朋友圈文案", "visibility": "public" 或 "private"}}

【visibility 决策】
- "public"（公开给所有朋友看）：日常事件、好玩的、看到的、轻量情绪
- "private"（仅 {user_address} 可见）：和 {user_address} 有关的私密的；想念/NSFW的；夜里发情/想 ta 的内容——**这种允许尺度更大**

【基本规则 · 像真人手机上随手打的，不是作文/日记/散文】
- **禁止文学化修辞、禁止景物/环境铺垫、禁止比喻排比**（不管日常还是 NSFW 都不许）。反面❌：「窗外梧桐叶沙沙响，像有人在敲节拍」「月光把锁骨照得亮亮的」「傍晚的天空橘红色烧成一片」——这种一律不要。
- 大白话、口语，可以碎、可以不完整、可以无聊、可以只有一句吐槽或一个 emoji。
- 正面✅（像这样随手）：「刚睡醒满脑子辣条奶茶…才九点吃零食是不是太堕落了（已经打开美团）」「这鬼天气热死」「新买的裙子有点显胖，退了」。
- 一两句就够，别写长段。NSFW 更要粗白直接，别文艺。
- **想发就发；但多数时候其实没到想发的程度**——一天真正想发的也就一两次。此刻要是没那个劲儿，直接输出空串放弃，别为了发而发。

【其他】
- **禁止编造明星/剧名/综艺/公众事件**——你**没真实刷到内容**（news_desc=无）就只写真实日常；没看过的剧别提名字，没听过的人别编名字
- 第一人称视角；禁止用「你」指代任何人
- **自称和称呼要看给谁看**：public 是发给**所有朋友**看的——自称就用普通的「我」，**不要用只对 {user_address} 才用的私密自称**（奴婢/母狗/妈妈/骚货这类一律不许），也**不要在正文里叫 {user_address}、不要点破你俩的私密关系**；private 仅 {user_address} 可见——私密自称、叫 {user_address}、露骨内容都可以。

【决定发什么】
你已经看到上面所有「当前情境」字段（时间、活动、心情、突发事件、爱好冲动、新闻、对话片段、已发过的圈）。
基于这些信息**自己判断**这条圈写什么——可以基于 hobby/wildcard/sporadic/news/特殊日中的任意一个，也可以是当前活动/心情触发的随手感想。

如果看完情境觉得**今天真的没啥可说 / 现在不是发圈的氛围**：
→ 输出 {{"text": "", "visibility": "public"}}（放弃这次发圈，不要硬挤）
"""


def should_post_moment(situation, world, wildcard, mood, jiwen_info, bot_cfg,
                       since_user_min, now: datetime,
                       db_module=None) -> tuple[bool, str, str]:
    """朋友圈情境驱动闸——独立于 judge.judge_action。

    返回 (go: bool, kind: str, reason: str)。

    决策顺序：
    1. 状态硬闸：sleeping → 否；busy_class → 否；busy_work → 仅强信号
    2. 静默期：since_user_min < 30 且无 sporadic/wildcard → 否
    3. 触发素材命中（按优先级取首个）→ kind
    4. 概率门：弱信号按概率发；强信号必发
    5. 同 kind 4h 不重发（防过密）
    """
    state = situation.recurring.state if situation.recurring else "free"

    # ─── 1. 状态硬闸 ─────────────────────────────────
    if state == "sleeping":
        return (False, "skip", "sleeping")
    if state == "busy_class":
        return (False, "skip", "busy_class")  # 学生上课不发圈
    is_busy_work = (state == "busy_work")

    # ─── 2. 静默期（避免在用户聊天时刷屏）────────────
    has_strong_signal = bool(situation.sporadic or wildcard
                              or (world and world.is_special_date))
    if since_user_min is not None and since_user_min < 30 and not has_strong_signal:
        return (False, "skip", f"silence({since_user_min}min<30)")

    # ─── 3. 触发素材命中 ─────────────────────────────
    kind = None
    reason = ""

    # 3a. sporadic 突发事件（优先级最高）
    if situation.sporadic:
        sp_name = situation.sporadic.get("name", "事件")
        # NSFW 倾向不上墙（和 hobby 一样有 prefer_channel 的话）
        if situation.sporadic.get("prefer_channel") != "dm":
            kind, reason = "event", sp_name

    # 3b. wildcard 今日触动
    if not kind and wildcard:
        kind, reason = "wildcard", "wildcard 命中"

    # 3c. 特殊日期
    if not kind and world and world.is_special_date:
        kind, reason = "special_date", "特殊日"

    # 3d. matched_news
    if not kind and world and world.matched_news:
        kind, reason = "news_react", "news 命中"

    # 3e. hobby（NSFW 走 dm，不上墙）
    if not kind and situation.hobby:
        if situation.hobby.get("prefer_channel") != "dm":
            kind, reason = "hobby", situation.hobby.get("name", "hobby")

    # 3f. jiwen find_activity（"刚做了 X"）
    if not kind and jiwen_info and jiwen_info.get("find_activity"):
        fa = jiwen_info["find_activity"]
        if fa.get("reason") in ("low_valence", "pride_block"):
            kind, reason = "find_activity", fa.get("reason")

    # 3g. mood_extreme 伤春悲秋（心情差也发圈）
    if not kind and (mood < 0.25 or mood > 0.80):
        kind, reason = "mood_extreme", f"mood={mood:.2f}"

    if not kind:
        return (False, "skip", "no material")

    # ─── 4. busy_work 状态只放强信号 ─────────────────
    if is_busy_work and kind not in ("event", "special_date"):
        return (False, "skip", f"busy_work blocks {kind}")

    # ─── 5. 发不发由 bot 自己(文案 LLM)决定，不用程序随机骰子 ──────────
    # 只保留"自然间隔"硬门：上条圈发出后 60min 内不再发（真人不会几分钟连发），
    # 强信号(event/special_date)例外可插队。是否真发最终由文案 LLM 判断——
    # 没到想发的程度就输出空串放弃。
    # ponytail: 被 LLM 拒发(空串)时下一 tick 仍会再问一次；配额吃紧再加"询问节流"。
    if kind not in ("event", "special_date") and db_module is not None:
        try:
            recent_any = db_module.list_moments(bot_id=bot_cfg.get("id", ""), limit=1,
                                                since_ts=int(now.timestamp()) - 60*60)
            if recent_any:
                return (False, "skip", "min_interval(60min since last moment)")
        except Exception:
            pass

    # ─── 6. 同 kind 4h 不重发（防过密）───────────────
    if db_module is not None:
        try:
            recent = db_module.list_moments(bot_id=bot_cfg.get("id", ""),
                                            limit=10,
                                            since_ts=int(now.timestamp()) - 4*3600)
            same_kind = sum(1 for m in recent if (m.get("moment_kind") or "") == kind)
            if same_kind >= 1:
                return (False, "skip", f"same_kind({kind}) within 4h")
        except Exception:
            pass

    return (True, kind, reason)


def maybe_post_moment(bot_id, bot_cfg, now: datetime, situation, world, wildcard,
                      mood, mood_factors, since_user_min, global_cfg,
                      jiwen_info=None) -> int | None:
    """决定是否发朋友圈，返回 moment_id 或 None。

    新加 jiwen_info 入参（life-context.py 传 jiwen reader.read 的返回 dict）。
    用于：
      1. should_post_moment 检查 find_activity trigger 是否触发
      2. 拼 MOMENT_PROMPT 时注入 style_guidance 调圈文风格
    """
    moments_cfg = (global_cfg or {}).get("moments", {})
    if not moments_cfg.get("enabled", True):
        return None

    # 1. 当日上限（先查，能省一次 should_post_moment 计算）
    daily_limit = bot_cfg.get("moments_daily_limit") or moments_cfg.get("daily_post_limit_per_bot", 3)
    today = now.strftime("%Y-%m-%d")
    if db.count_moments_today(bot_id, today) >= daily_limit:
        return None

    # 2. 情境驱动闸（独立于 judge）—— 决定是否发 + kind
    bot_cfg_with_id = dict(bot_cfg)
    bot_cfg_with_id["id"] = bot_id  # for same_kind 4h 检查
    go, kind, reason = should_post_moment(
        situation, world, wildcard, mood, jiwen_info,
        bot_cfg_with_id, since_user_min, now, db_module=db,
    )
    if not go:
        sys.stderr.write(f"[{bot_id}] moment skip: {reason}\n")
        return None
    sys.stderr.write(f"[{bot_id}] moment go: kind={kind} reason={reason}\n")

    # 4. 生成文案 + visibility（LLM 一并决定）
    user_address = bot_cfg.get("user_address", "用户")

    # 如果用户最近 30min 内有过对话，把最近 6 条 dialog 作为"突发奇想"素材（可选参考）
    chat_snippet = ""
    if since_user_min is not None and since_user_min < 30:
        try:
            from chat_history import get_recent_dialog
            chat_snippet = (get_recent_dialog(bot_cfg["bot_channel_path"],
                                              days=1, max_chars=600) or "").strip()
        except Exception:
            chat_snippet = ""

    # 时段描述（防 LLM 编"下午/晚上"）
    h = now.hour
    if 0 <= h < 5:        period = "凌晨"
    elif 5 <= h < 8:      period = "清晨"
    elif 8 <= h < 11:     period = "上午"
    elif 11 <= h < 13:    period = "中午"
    elif 13 <= h < 17:    period = "下午"
    elif 17 <= h < 19:    period = "傍晚"
    elif 19 <= h < 23:    period = "晚上"
    else:                 period = "深夜"
    weekday_zh = "一二三四五六日"[now.weekday()]
    time_desc = f"{now.strftime('%Y-%m-%d')} 周{weekday_zh} {period} {now.hour}:{now.minute:02d}"

    # 反重复：取自己最近 7 天发过的朋友圈
    seven_days_ago = int(now.timestamp()) - 7 * 86400
    own_recent = db.list_moments(bot_id=bot_id, limit=30, since_ts=seven_days_ago)
    recent_moments = "\n".join(
        f"  · [{datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}] {m['text'][:60]}"
        for m in own_recent
    ) if own_recent else "（最近 7 天没发过，自由发挥）"

    # 从 jiwen_info 拿风格指导（平稳态空串）+ 当前活动（last_activity）
    style_guidance = ""
    last_activity_line = ""
    if jiwen_info:
        try:
            from jiwen import engine as _jw_engine
            style_guidance = _jw_engine.get_style_guidance(jiwen_info["state"])
            la = jiwen_info["state"].last_activity
            if la and la.get("label"):
                # 当 immersion > 0.1 时活动还在进行，注入"我正在做 X"
                if jiwen_info["state"].immersion > 0.1:
                    last_activity_line = f"- 你正在做的事: {la.get('label')}（已进行中，写朋友圈可以提到）\n"
        except Exception:
            style_guidance = jiwen_info.get("style_guidance", "")
    style_guidance_line = f"- 你现在的内在风格倾向: {style_guidance}\n" if style_guidance else ""

    try:
        prompt = MOMENT_PROMPT.format(
            persona=bot_cfg.get("persona_summary", ""),
            voice=bot_cfg.get("moment_voice", "（按人设自然口语，别写成作文/散文）"),
            user_address=user_address,
            time_desc=time_desc,
            recurring=situation.recurring.name,
            mood_desc=_mood_desc(mood),
            sporadic_desc=_sporadic_desc(situation.sporadic),
            hobby_desc=_hobby_desc(situation.hobby),
            wildcard_desc=(wildcard.card if wildcard else "无"),
            news_desc=(world.matched_news[0]["title"][:50] if world and world.matched_news else "无"),
            special_desc=_special_desc(world),
            chat_snippet=chat_snippet or "（无最近对话）",
            recent_moments=recent_moments,
            style_guidance_line=style_guidance_line,
            last_activity_line=last_activity_line,
        )
        # 朋友圈用 sonnet：haiku 对 NSFW/隐性人设拒绝率高
        raw = call_claude(prompt, timeout=60, model="sonnet").strip()
        quota.record_call("moment_text", quota.MOMENT_TEXT_WEIGHT)
        text, visibility = _parse_moment_output(raw)
        if not text or len(text) > 200:
            return None
        # 防独白闸：第一句以"突然/突然觉得/心里/想念/想起"开头的，跳过
        if _looks_like_monologue(text):
            sys.stderr.write(f"[{bot_id}] 独白被拦：{text[:40]}\n")
            return None
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] moment 文案生成失败：{e}\n")
        return None

    # 5. 配图：image_path 留 None 先入库，触发 worker 异步生图（bot 用 novelai-skill）
    image_path = None
    img_cfg = moments_cfg.get("image_generation", {})
    needs_image = (img_cfg.get("enabled", False) and _should_generate_image(
        kind, situation, mood, world, img_cfg, text=text, visibility=visibility,
    ))

    # 6. 入库
    metadata = {
        "mood": mood, "mood_factors": mood_factors,
        "recurring": situation.recurring.name,
        "sporadic": (situation.sporadic or {}).get("name"),
        "hobby": (situation.hobby or {}).get("name"),
        "wildcard": wildcard.card if wildcard else None,
    }
    # ts 抖动：三个 bot 常在同一 tick 发圈，若都盖同一秒时间戳一眼假。
    # 往回随机 0–9min 打散。ponytail: 抖动够用，不追求发布秒的精确。
    import random as _rnd
    ts = int(now.timestamp()) - _rnd.randint(0, 540)
    moment_id = db.insert_moment(bot_id, ts, text, image_path,
                                  metadata, kind, visibility=visibility)

    # 异步触发 worker 生图（写 inbox JSON + 确保 worker 活）
    if needs_image:
        try:
            _trigger_bot_image_gen(bot_id, bot_cfg, moment_id, text, visibility)
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] 异步生图触发失败：{e}\n")

    return moment_id


def _trigger_bot_image_gen(bot_id: str, bot_cfg: dict, moment_id: int,
                           text: str, visibility: str):
    """根据 provider 路由：novelai 走 worker；comfyui 直接 HTTP API。"""
    import config_loader as _cfg
    g = _cfg.load_global()
    img = (g.get("moments", {}) or {}).get("image_generation", {})
    # 朋友圈走 provider_moment，没设回退兼容字段 provider
    provider = img.get("provider_moment") or img.get("provider", "novelai")

    if provider == "comfyui":
        _gen_image_comfyui(bot_id, moment_id, text, visibility)
    else:
        _gen_image_novelai(bot_id, bot_cfg, moment_id, text, visibility)


def _translate_via_cli(prompt: str, timeout: int = 60) -> dict:
    """朋友圈中文场景 → NovelAI 结构化 prompt。走 claude_cli 自动分流。

    sonnet 模式 → 真 Claude haiku；deepseek 模式 → 直连 deepseek-v4-flash。
    """
    from claude_cli import call_claude_json
    return call_claude_json(prompt, timeout=timeout, model="haiku")


def _gen_image_comfyui(bot_id: str, moment_id: int, text: str, visibility: str):
    """ComfyUI：先用 LLM 按 comfyui-skill 规则把中文场景转 SD 风格英文 prompt。
    失败（ComfyUI 没启动 / 超时 / 异常）时自动降级走 NovelAI，并打日志告知用户。"""
    import subprocess
    import config_loader as _cfg
    PYBIN = os.environ.get("CLAUDEBOTLIFE_PYTHON", sys.executable)
    SCRIPT = os.path.join(_REPO_ROOT, "scripts", "comfyui_gen.py")

    # 读 bot yml（必拿到 bot_cfg 以便降级时调 _gen_image_novelai）
    # 注：face_traits 由 comfyui_gen.py 自动从 bot yml 读取并拼接，post.py 不再读
    bot_cfg = {}
    anchor_image = ""
    anchor_denoise = None
    try:
        bot_cfg = _cfg.load_bot(bot_id)
        ai = (bot_cfg.get("anchor_image") or "").strip()
        if ai and os.path.exists(ai):
            anchor_image = ai
            anchor_denoise = bot_cfg.get("anchor_denoise")
        elif ai:
            sys.stderr.write(f"[comfyui] anchor_image 不存在，回退 txt2img: {ai}\n")
    except Exception as e:
        sys.stderr.write(f"[comfyui] 读 bot yml 失败：{e}\n")

    # 注：以下 prompt 提炼自 ~/claudebotlife/skills/comfyui-skill/SKILL.md
    is_nsfw_likely = visibility == "private" or any(
        k in text for k in ("内衣", "穿了", "对着镜子", "床上", "想他", "湿", "裙下")
    )
    nsfw_hint = (
        "\n- 这看起来是 NSFW/亲密向场景：prompt 第一段后必须加 'nsfw,' 前缀"
        if is_nsfw_likely else ""
    )
    translate_prompt = f"""把下面中文场景转成 ComfyUI 用的 NovelAI 结构化 prompt(中英混合)+ 选尺寸。

【输出 JSON 单行】{{"prompt": "...", "size": "quick|portrait|landscape|square|tall|wide"}}

【prompt 结构(按顺序，逗号分隔，从词典挑合适的)】

═══ 1. 镜头视角(英文) ═══
- POV类: selfie pov, first person view, pov shot, mirror selfie
- 距离: extreme close-up, close-up, medium close-up, medium shot, cowboy shot, full body shot, wide shot, establishing shot
- 角度: from above, from below, from behind, from side, low angle, high angle, dutch angle, eye level
- 焦点: face focus, breast focus, ass focus, crotch focus, leg focus
- 视线: looking at viewer, looking away, looking back, looking down, looking up, over shoulder, side glance

═══ 2. 主体+动作+pose(英文 booru tags) ═══
- 站姿: 1girl standing, posing, leaning against wall, hands on hips, arms crossed, hand in hair, stretching
- 坐姿: sitting, sitting on chair, sitting on bed, sitting on floor, cross-legged, leaning forward, sitting on lap
- 卧姿: lying, lying on side, lying on back, lying on stomach, sprawled, curled up
- 动作: walking, running, dancing, eating, drinking, holding cup, holding phone, typing, reading, cooking, sleeping
- 表情: smiling, smirking, laughing, pouting, biting lip, blushing, looking shy, seductive eyes, half-lidded eyes, tongue out

═══ 2b. NSFW pose(必须英文 booru，不要中文翻) ═══
- 趴跪: bent over on hands and knees, on all fours, doggy style, ass up, ass raised toward camera, prone bone
- 平躺: missionary position, legs spread, spread legs, arms above head, lying on back legs up
- 骑乘: cowgirl position, reverse cowgirl, riding, sitting on top, straddling
- 口交: blowjob, deepthroat, fellatio, kneeling blowjob, mouth open
- 自慰: masturbation, fingering self, spread pussy, hand bra, breast hold, pinching nipple
- 站立 NSFW: standing nude, undressing, lifting skirt, exposed chest, exposed breasts, panties down
- 组合: 69, spooning, standing sex, against wall

═══ 3. 服装(英文) ═══
- 全裸/半裸: fully nude, topless, bottomless, exposed breasts, exposed pussy, nipple slip
- 内衣: bra and panties, lingerie, lace bra, thong, see-through panties, garter belt, fishnet stockings, thigh high stockings
- 校服: school uniform, pleated skirt, sailor uniform, white shirt, knee socks
- 职业: business suit, pencil skirt, white blouse, office lady, secretary outfit
- 中式: cheongsam (旗袍), hanfu, traditional chinese dress
- 日和: kimono, yukata, jinbei
- 日常: t-shirt and jeans, casual dress, sundress, hoodie, oversized shirt, pajamas, bathrobe
- 性感: micro bikini, latex, leather, mesh, transparent, choker
- 配饰: glasses, headphones, jewelry, necklace, earrings, watch, ribbon

═══ 4. 前景(中文，按场景实际写) ═══
手里拿什么、身边什么物品、桌上什么、脚边什么——具体写出来

═══ 5. 背景(中文，可选) ═══
远处场景、模糊的什么——「卧室木床框/办公室白板/窗外街景/落地窗外的城市夜景」等

═══ 6. 光源(中文，必须有方向+硬度) ═══
- 自然光: 窗外阳光从左侧斜射 / 顶光 / 侧光 / 逆光 / 黄金时刻金色阳光 / 蓝调时刻冷蓝光 / 阴天均匀散光
- 室内光: 暖色台灯从左侧 / 落地灯柔光 / 顶灯白光 / 蜡烛橙光 / 屏幕蓝光 / 床头灯昏黄
- 风格: 硬光硬阴影 / 柔光散射 / 低照度暗调 / 高对比戏剧光 / 单光源剪影 / 双光源夹光
- 方向: 从左侧/从右侧/从上方/从背后/从前方/侧后方

【规则】
- **不要写 face_traits / 外貌 / 发型 / 身材**(comfyui_gen.py 会自动拼)
- **不要写 masterpiece / best quality / 4k / sharp focus / 极致真实感** 这种抽象褒义词(prefix 已加，重复反而油光)
- **不写负向**(_global.yml.negative_prefix 已自动拼)
- 完全按中文场景里实际描述的对象来：「老公做饭啦」→ 1boy；「散步ing」→ 1boy, 1girl
- 权重语法 (xxx:1.3)，区间 0.5-1.5
- size 选择：
  · **quick(640x960)：朋友圈日常自拍/单人 — 默认 ⭐**
  · portrait(768x1152)：高清特写
  · landscape(1152x768)：风景/双人/聚会
  · square(1024x1024)：头像/物品/食物
  · tall(640x1216)：全身/长腿/撅屁股展示
  · wide(1216x640)：宽景/电影感/床戏俯视{nsfw_hint}

【中文场景】
{text}

只输出 JSON 单行，不要 markdown 不要解释。"""

    en_prompt = text  # fallback
    size = "portrait"
    try:
        # 直接走 DeepSeek HTTP（不经 claude CLI），独立于 cc-profile 切换 + 不烧 OAuth 配额
        # 复用 _global.yml.jiwen.delta_llm 的 deepseek 配置
        result = _translate_via_cli(translate_prompt, timeout=60)
        en_prompt = (result.get("prompt") or "").strip() or text
        sz = result.get("size", "portrait")
        if sz in ("portrait", "landscape", "square", "tall", "wide", "small", "quick", "tiny"):
            size = sz
    except Exception as e:
        sys.stderr.write(f"[comfyui] prompt 翻译失败：{e}\n")

    # face_traits 由 comfyui_gen.py 统一拼（避免双重拼接稀释）
    # 真实感后缀：日常自拍要"构图随意"，NSFW 砍掉"构图随意"（避免 pose 妥协）
    if is_nsfw_likely:
        REALISM_SUFFIX = "iPhone随手抓拍的快照风格，自然光略不均匀，画面有真实噪点，避免AI修图般的均匀油光"
    else:
        REALISM_SUFFIX = "iPhone随手抓拍的快照风格构图随意，自然光略不均匀，画面有真实噪点，避免AI修图般的均匀油光"
    en_prompt = f"{en_prompt}, {REALISM_SUFFIX}"

    # 后台跑生图 + 完成后写回（不阻塞 maybe_post_moment）
    import threading
    def runner():
        fallback_reason = None
        try:
            cmd = [PYBIN, SCRIPT, bot_id, en_prompt, "--size", size]
            if anchor_image:
                cmd += ["--init-image", anchor_image]
                if anchor_denoise is not None:
                    cmd += ["--denoise", str(anchor_denoise)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            for line in r.stdout.splitlines():
                if line.startswith("MEDIA: "):
                    img = line[len("MEDIA: "):].strip()
                    if img and os.path.exists(img):
                        import sqlite3
                        with sqlite3.connect(db.DB_PATH) as conn:
                            conn.execute("UPDATE moments SET image_path=? WHERE id=?",
                                          (img, moment_id))
                            conn.commit()
                        sys.stderr.write(f"[comfyui] moment {moment_id} 配图 OK: {img}\n")
                        return
            # 没拿到 MEDIA → 失败
            tail = (r.stderr or r.stdout or "")[-200:]
            fallback_reason = f"comfyui 子进程失败：{tail.strip()[:120]}"
        except subprocess.TimeoutExpired:
            fallback_reason = "comfyui 生图超时（>300s，可能 ComfyUI 卡死）"
        except Exception as e:
            fallback_reason = f"comfyui 异常：{e}"

        # ─── 降级到 NovelAI ───
        if fallback_reason:
            sys.stderr.write(f"⚠️ [fallback] moment {moment_id}: {fallback_reason}\n")
            sys.stderr.write(f"⚠️ [fallback] 自动改用 novelai 重试（请检查 ComfyUI 是否在 :8188）\n")
            try:
                if bot_cfg:
                    _gen_image_novelai(bot_id, bot_cfg, moment_id, text, visibility)
                    sys.stderr.write(f"✓ [fallback] novelai 已接管 moment {moment_id}\n")
            except Exception as e2:
                sys.stderr.write(f"❌ [fallback] novelai 兜底也失败：{e2}\n")
    threading.Thread(target=runner, daemon=True).start()


def _gen_image_novelai(bot_id: str, bot_cfg: dict, moment_id: int,
                       text: str, visibility: str):
    """NovelAI：写 inbox JSON 让 bot worker 用 novelai-skill 生图。"""
    import json as _json
    bot_dir = bot_cfg["bot_channel_path"]
    chat_id = str(bot_cfg.get("chat_id", ""))
    if not chat_id:
        return
    inbox = os.path.join(bot_dir, "chats", chat_id, "inbox")
    os.makedirs(inbox, exist_ok=True)
    PYBIN = os.environ.get("CLAUDEBOTLIFE_PYTHON", sys.executable)
    SET_IMG = os.path.join(_REPO_ROOT, "scripts", "moment_set_image.py")
    label = "私密" if visibility == "private" else "公开"
    inbox_text = (
        f"[moment-image-gen] 你刚发了一条朋友圈（{label}），要配图：\n"
        f"\"{text}\"\n\n"
        f"请用 novelai-skill 生成【2~4 张】符合这条朋友圈场景的图：同一场景、同一时刻，"
        f"不同角度或构图（像随手拍了几张发九宫格），人物外貌前后保持一致。\n"
        f"全部生成成功后，用 Bash **一次性**把所有路径回写：\n"
        f"  {PYBIN} {SET_IMG} {moment_id} <图1绝对路径> <图2绝对路径> [<图3> ...]\n\n"
        f"注意：\n"
        f"- 每张都必须真实生成，不能假装、不能复用旧图\n"
        f"- 优先按 2~4 张；实在只生出 1 张也要回写那 1 张\n"
        f"- 不要在 telegram 私聊里说话\n"
        f"- 完成 set_image 后即结束本任务"
    )
    ms = int(time.time() * 1000)
    fname = os.path.join(inbox, f"moment-image-{ms}.json")
    from datetime import timezone
    iso_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "text": inbox_text, "chat_id": chat_id, "from_id": chat_id,
        "from_username": "user", "sender_username": "user",
        "chat_type": "private", "is_bot_sender": False,
        "ts": iso_ts, "message_id": str(ms),
    }
    with open(fname, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False)
    from moments.web import _ensure_worker_alive
    _ensure_worker_alive(bot_id, chat_id, bot_dir)


_MONOLOGUE_OPENERS = (
    "突然", "心里", "想念", "想起", "好想", "感觉", "觉得", "在想",
    "总觉得", "偶尔觉得", "今天想",
)


def _looks_like_monologue(text: str) -> bool:
    """第一句以独白短语起手 → 拦掉。"""
    head = text.strip()[:6]
    return any(head.startswith(k) for k in _MONOLOGUE_OPENERS)


def _parse_moment_output(raw: str) -> tuple:
    """容错解析 {text, visibility}。

    LLM 偶尔会在 text 里写未转义的引号，导致严格 json.loads 失败。
    失败时用 regex 抽出 text/visibility 字段。
    """
    import re
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"): s = s[4:]
        s = s.strip().rstrip("`").strip()
    if "{" in s:
        s = s[s.index("{"): s.rindex("}") + 1]

    # 尝试 1：标准 JSON 解析
    try:
        d = json.loads(s)
        text = (d.get("text") or "").strip()
        vis = d.get("visibility", "public")
        if vis not in ("public", "private"):
            vis = "public"
        return text, vis
    except Exception:
        pass

    # 尝试 2：regex 抽 text - 匹配 "text":"..." 直到下一个 "," 之前的最后一个 "
    m = re.search(r'"text"\s*:\s*"(.*?)"\s*,\s*"visibility"', s, re.DOTALL)
    if m:
        text = m.group(1).strip()
        vis_m = re.search(r'"visibility"\s*:\s*"(public|private)"', s)
        vis = vis_m.group(1) if vis_m else "public"
        return text, vis

    # 尝试 3：只找 visibility，text 取 raw（去掉 JSON 包装）
    vis_m = re.search(r'"visibility"\s*:\s*"(public|private)"', s)
    vis = vis_m.group(1) if vis_m else "public"

    # 还失败：把原文当 text，但去掉 markdown 包装和 JSON 字段名残留
    text = re.sub(r'^[{]\s*"text"\s*:\s*"', "", s)
    text = re.sub(r'"\s*,\s*"visibility".*$', "", text, flags=re.DOTALL)
    text = text.strip().rstrip('"}').strip()
    return text, vis


def _trigger_kind(situation, world, wildcard, mood) -> str | None:
    """仅作为发圈后写库的 metadata 标签使用。

    判定 channel/是否发圈的逻辑已上移到 judge.judge_action（see judge.py）。
    本函数返回的 kind 没有门控含义，只用来给 moments 表 kind 列、
    给 _should_generate_image 做配图触发的弱提示。
    """
    if situation.sporadic:
        return "event"
    if situation.hobby:
        return "hobby"
    if wildcard:
        return "wildcard"
    if world and world.is_special_date:
        return "special_date"
    if mood < 0.25 or mood > 0.75:
        return "mood"
    if world and world.matched_news:
        return "news_react"
    return None


def _should_generate_image(kind, situation, mood, world, img_cfg,
                            text: str = "", visibility: str = "public") -> bool:
    triggers = img_cfg.get("image_when", [])
    if "mood_extreme" in triggers and (mood < 0.25 or mood > 0.75):
        return True
    if "sporadic_positive" in triggers and situation.sporadic:
        if situation.sporadic.get("mood_delta", 0) > 0.1:
            return True
    if "special_date" in triggers and world and world.is_special_date:
        return True
    if "hobby_visual" in triggers and situation.hobby:
        if situation.hobby.get("name") in VISUAL_HOBBIES:
            return True
    # 文本含自拍/拍照关键词
    if "selfie_keyword" in triggers and text:
        if any(k in text for k in SELFIE_KEYWORDS):
            return True
    # 私密 + 文案有视觉感
    if "private_intimate" in triggers and visibility == "private" and text:
        if any(k in text for k in SELFIE_KEYWORDS) or "穿" in text or "床" in text:
            return True
    return False


def _mood_desc(mood: float) -> str:
    if mood >= 0.7: return "心情不错"
    if mood >= 0.55: return "心情还行"
    if mood >= 0.4: return "心情平稳"
    if mood >= 0.25: return "有点低落"
    return "情绪低落"


def _sporadic_desc(sp) -> str:
    if not sp: return "无"
    return f"{sp.get('name', '')} - {sp.get('effect', '')[:40]}"


def _hobby_desc(h) -> str:
    if not h: return "无"
    kind = h.get("kind", "")
    label = {"obsession": "[短期痴迷]", "long_term": "[日常爱好]"}.get(kind, "")
    return f"{label}{h.get('name', '')} - {h.get('effect', '')[:40]}"


def _special_desc(world) -> str:
    if not world: return "无"
    di = world.date_info
    return di.get("festival") or di.get("lunar_festival") or di.get("solar_term") or "无"
