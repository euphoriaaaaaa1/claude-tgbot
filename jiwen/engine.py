"""积温（jiwen）情绪驱力数学引擎 — Phase 1 核心。

借鉴 ClaraShafiq/jiwen 的 4 维状态空间，纯数学函数，零依赖外部服务。
本模块**不持久化**、**不调 LLM**、**不接 Telegram**——只做状态漂移 + 阈值判断 + 翻译。

集成层（持久化/HTTP/cron）由调用方负责。

State 4 维：
  connection  0..1     连接需求（越高越想找用户）
  pride      -1..1     骄傲（>0 拉不下脸；<0 谦卑想求安慰）
  valence    -1..1     情绪基调（>0 开心；<0 低落）
  arousal     0..1     情绪激活度/沉浸度（高=投入到正在做的事或激动情绪）

漂移公式（每分钟）：
  connection += rate_per_min * accel(state) * valence_modifier(state)
  pride      → 0     线性回归
  valence    → 0     线性回归
  arousal    → 0     线性衰减（活动结束 60min 归零）
"""
from __future__ import annotations
import math
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ─── 配置数据类 ──────────────────────────────────────────────

@dataclass
class Rates:
    """每分钟漂移速率（全局默认，bot 可覆盖）"""
    connection_per_min: float = 0.0007       # 基础累积速率
    pride_decay_per_min: float = 0.003       # pride 向 0 回归
    valence_decay_per_min: float = 0.005     # valence 向 0 回归
    arousal_decay_per_min: float = 0.01      # arousal 衰减（约 100min 归零，加上线性更快）
    accel_threshold_min: float = 5.0         # 距上次消息超过 N min 才启动加速
    connection_accel: float = 1.0            # 加速指数（accelFactor = (1+connection)^N）
    # valence 调节器（情绪影响 connection 累积）
    valence_severe_low: float = -0.5         # 严重低落阈值
    valence_severe_multiplier: float = 0.3   # 严重低落 → 抑制 connection（自我封闭）
    valence_mild_low: float = -0.2           # 轻度低落阈值
    valence_mild_multiplier: float = 1.5     # 轻度低落 → 加速 connection（求安慰）

    # ─── 新增：完整移植 ClaraShafiq/jiwen 的高级机制 ────────────
    # valence lock：connection 高时坏情绪难消散（情绪记忆）
    valence_lock_threshold: float = 0.5      # connection 超过此值触发锁定
    valence_lock_factor: float = 0.3         # 衰减率乘数（最低 0.1× 防卡死）

    # arousal 由 connection 攀升：等待越久越焦躁
    arousal_connection_rise_threshold: float = 0.5
    arousal_connection_rise_rate: float = 0.002   # 每分钟攀升量

    # pride 防御：被冷落时 pride 漂向防御目标（嘴硬机制）
    pride_defend_threshold: float = 0.4      # 被冷落且 c≥此值 → pride 漂高
    pride_defend_target: float = 0.6
    pride_defend_rate: float = 0.003

    # pride × arousal 冲突：想要又端着 → 内心煎熬（arousal 升温）
    pride_arousal_conflict_rate: float = 0.001

    # pride 侵蚀：想念太重 → pride 被磨低（不再嘴硬）
    pride_erosion_rate: float = 0.002

    # immersion 维度（沉浸度衰减）
    immersion_decay_per_min: float = 0.010

    # 活动缓解：bot 自己做事可以缓解 connection
    activity_connection_relief: float = 0.0005    # connection -= 此值 × mins

    # 刚聊过就退潮：窗口内 connection 按心情回落到静息底线（connection 唯一的下行通道，见 drift 机制 8）
    connection_relief_window_min: float = 240.0   # 距上次用户消息 < 此值算"刚聊过"
    connection_relief_per_min: float = 0.007      # valence=+1 时的退潮速度上限；设 0 = 关掉本机制
    connection_relief_floor: float = 0.15         # 退到此值为止；它决定"多久不理她才会主动开口"，须 < Thresholds.notice

    # F-02 connection 的另两条下行通道（在 tick 里施加，不进 drift：drift 是纯函数，
    # 拿不到"窗口内是否有 bot 回复"这种来自会话扫描的信号）
    connection_decay_after_min: float = 240.0        # 用户沉默超过此值 → 按小步回落
    connection_silence_decay_per_min: float = 0.002  # 久未开口时的每分钟回落量
    connection_reply_decay: float = 0.05             # bot 主动消息真正送达后的固定回落量

    # immersion 初始映射（不同活动初始沉浸度不同）
    immersion_map: dict = field(default_factory=lambda: {
        "reading": 0.6,
        "cooking": 0.5,
        "search": 0.4,
        "browse": 0.35,
        "observe": 0.15,
        "selfcare": 0.5,
    })


@dataclass
class Thresholds:
    """阈值（全局默认，bot 可覆盖）"""
    notice: float = 0.20         # connection 达到此 → 内心念头（不发出）
    consider: float = 0.35       # connection 达到此 → 考虑开口（pride 可阻断）
    forced: float = 0.50         # connection 达到此 → 强制开口（无视 pride）
    pride_block: float = 0.5     # pride 高于此 → 阻断 consider 阶段
    # 新增：find_activity 触发阈值
    valence_activity: float = -0.6   # valence 低于此 → 触发 find_activity（自我调节）
    arousal_agitation: float = 0.7   # arousal 高于此 → 触发 find_activity（转移注意力）
    immersion_block: float = 0.3     # immersion ≥ 此值 → 不再触发 find_activity（已在做事）


@dataclass
class State:
    """5 维浮点状态 + 元数据（immersion 是新加的第 5 维）"""
    connection: float = 0.0
    pride: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    immersion: float = 0.0          # 沉浸度 0..1：在做事时高（独立于 arousal）
    last_tick_ts: int = 0           # 上次 drift 的 unix 时间戳
    # 下面两个时间戳语义**不同**，曾经合并成一个字段用，导致 delta 调用失败时
    # "用户说过话"这个客观事实一并被冻结、since_last_msg 无限增长、connection 涨到封顶：
    last_user_msg_ts: int = 0       # 客观事实：真人最后一次私聊的【发送】时刻（来源 = dispatcher marker）
    last_delta_ts: int = 0          # 已成功评过分的位置：只有 delta 调用成功并应用后才推进
    last_user_msg_meta: dict = field(default_factory=dict)
    last_activity: dict = field(default_factory=dict)  # {type, label, started_ts, initial_immersion}
    # meta 字段建议放：
    #   content_len: int
    #   ends_with_close: bool   (晚安/睡了/出门 等结束词)
    #   ends_with_cut: bool     (短消息/突然中断)


# ─── 工具函数 ────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    """把 x 限定到 [lo, hi]，并防 NaN/Inf。"""
    if not isinstance(x, (int, float)) or math.isnan(x) or math.isinf(x):
        return (lo + hi) / 2  # NaN 时回到中点
    return max(lo, min(hi, x))


def _decay_to_zero(value: float, rate_per_min: float, mins: float) -> float:
    """线性向 0 回归。value 越接近 0，回归越慢；这里实现成简单线性（每分钟扣固定量直到 0）。"""
    if value > 0:
        return max(0.0, value - rate_per_min * mins)
    elif value < 0:
        return min(0.0, value + rate_per_min * mins)
    return 0.0


def _connection_modifier(state: State, rates: Rates) -> float:
    """根据 valence 调节 connection 增速倍率。"""
    if state.valence < rates.valence_severe_low:
        return rates.valence_severe_multiplier  # 严重低落 → 抑制（自我封闭）
    elif state.valence < rates.valence_mild_low:
        return rates.valence_mild_multiplier    # 轻度低落 → 加速（求安慰）
    return 1.0


def _connection_accel_factor(state: State, rates: Rates, since_last_msg_min: float) -> float:
    """connection 自激加速因子。connection 越高、距上次消息越久，涨得越快。"""
    if since_last_msg_min < rates.accel_threshold_min:
        return 1.0
    if state.connection <= 0:
        return 1.0
    # accelFactor = (1 + connection)^connection_accel
    return math.pow(1.0 + state.connection, rates.connection_accel)


# ─── 核心函数 ────────────────────────────────────────────────

def drift(state: State,
          rates: Rates,
          mins: float,
          since_last_msg_min: Optional[float] = None,
          base_rate_override: Optional[float] = None) -> State:
    """状态漂移 mins 分钟。

    base_rate_override: 调用方可根据"最近用户消息内容"动态覆盖 connection 基础速率
                        （例：'晚安'结尾→0.0003；短消息切断→0.0010）
    since_last_msg_min: 距上次用户消息分钟数（用于决定加速）
    """
    if mins <= 0:
        return state
    # 防止补漂太多（系统休眠后突然 24h 一起补）
    mins = min(mins, 60.0)

    if since_last_msg_min is None:
        since_last_msg_min = 0.0 if state.last_user_msg_ts == 0 else \
            (time.time() - state.last_user_msg_ts) / 60.0

    base_rate = base_rate_override if base_rate_override is not None else rates.connection_per_min
    accel = _connection_accel_factor(state, rates, since_last_msg_min)
    modifier = _connection_modifier(state, rates)
    connection_delta = base_rate * accel * modifier * mins

    # ─── 基础漂移（与原版一致）─────────────────────────────
    new_connection = state.connection + connection_delta
    # valence 衰减率受 lock 影响（connection 高时坏情绪记忆持久）
    valence_decay = rates.valence_decay_per_min
    if state.connection >= rates.valence_lock_threshold:
        # 锁定时减慢，但保底 0.1× 防卡死
        valence_decay = max(rates.valence_decay_per_min * 0.1,
                            rates.valence_decay_per_min * rates.valence_lock_factor)
    new_valence = _decay_to_zero(state.valence, valence_decay, mins)

    new_pride = _decay_to_zero(state.pride, rates.pride_decay_per_min, mins)
    new_arousal = max(0.0, state.arousal - rates.arousal_decay_per_min * mins)

    # ─── 新增机制 1：arousal connection-rise（等待让人焦躁）────
    if state.connection >= rates.arousal_connection_rise_threshold:
        new_arousal = min(0.95, new_arousal + rates.arousal_connection_rise_rate * mins)

    # ─── 新增机制 2：pride defend（被冷落时嘴硬）─────────────
    if (since_last_msg_min >= 30
            and state.connection >= rates.pride_defend_threshold
            and new_pride < rates.pride_defend_target):
        new_pride = min(rates.pride_defend_target,
                        new_pride + rates.pride_defend_rate * mins)

    # ─── 新增机制 3：pride × arousal conflict（想要又端着）────
    if state.pride > 0.3 and state.connection > 0.4:
        new_arousal = min(0.95,
                          new_arousal + rates.pride_arousal_conflict_rate * mins)

    # ─── 新增机制 4：pride erosion（想念太重磨低 pride）──────
    if state.connection >= 0.6 and new_pride > 0:
        new_pride = max(0.0, new_pride - rates.pride_erosion_rate * mins)

    # ─── 新增机制 5：immersion decay ─────────────────────────
    new_immersion = max(0.0, state.immersion - rates.immersion_decay_per_min * mins)

    # ─── 新增机制 8：刚聊过就退潮（connection relief by valence）──
    # connection 原本只有加项、没有任何减项，一天必然封顶卡死在 forced；这是它唯一的下行通道。
    # 退潮速度由心情决定而不是随机数：聊得开心退得快，聊崩/被冷落退得慢（valence=-1 时完全
    # 不退，还惦记着）——"每次不一样"由 valence 每次不同自然产生，同时保住因果、可诊断、可测试。
    # 读旧值 state.valence（不是衰减后的 new_valence），与本函数其余机制的口径一致。
    # 编号 8 但**必须排在机制 6 之前**：口径是先退潮、再扣活动缓解（贴着底线时两种顺序差
    # 至多一个 activity_connection_relief×mins，口径要唯一）。
    # 退到 floor 就停、不退到 0：退到 0 会让"每天聊几次"的用户彻底收不到主动消息；
    # 又必须 < Thresholds.notice(0.20)，否则静息态恒被判成"开始注意到沉默"，正是本次要修的观感。
    # 两道守卫都不是防御性冗余：
    #   ① relief_per_min == 0（速率设 0 / 窗口关掉 / valence=-1）→ 整段跳过，输出逐位等于
    #      改动前。不靠"减掉底线再加回来"的浮点等价——那样会差 1 个 ULP，kill switch 就不成立。
    #   ② new_connection > floor → 低于底线的值不能进来：_decay_to_zero 的负值分支会把它
    #      反向抬高（刚聊完反而更想念），方向完全错。
    # _clamp 兜脏 valence：NaN 回中点（半速退潮）而不是把速率算成 NaN；越界值截到两端不放大速率。
    if since_last_msg_min < rates.connection_relief_window_min:
        relief_per_min = rates.connection_relief_per_min * _clamp(
            (state.valence + 1.0) / 2.0, 0.0, 1.0)
        if relief_per_min != 0.0 and new_connection > rates.connection_relief_floor:
            new_connection = rates.connection_relief_floor + _decay_to_zero(
                new_connection - rates.connection_relief_floor, relief_per_min, mins)

    # ─── 新增机制 6：activity connection relief（做事缓解黏）──
    new_last_activity = dict(state.last_activity) if state.last_activity else {}
    if new_last_activity and new_immersion > 0.1:
        new_connection = max(0.0,
                             new_connection - rates.activity_connection_relief * mins)

    # ─── 新增机制 7：last_activity 自动失效 ──────────────────
    if new_immersion < 0.05:
        new_last_activity = {}

    new_state = State(
        connection=_clamp(new_connection, 0.0, 1.0),
        pride=_clamp(new_pride, -1.0, 1.0),
        valence=_clamp(new_valence, -1.0, 1.0),
        arousal=_clamp(new_arousal, 0.0, 1.0),
        immersion=_clamp(new_immersion, 0.0, 1.0),
        last_tick_ts=int(time.time()),
        last_user_msg_ts=state.last_user_msg_ts,
        last_delta_ts=state.last_delta_ts,
        last_user_msg_meta=dict(state.last_user_msg_meta),
        last_activity=new_last_activity,
    )
    return new_state


def apply_delta(state: State, delta: dict) -> State:
    """新对话发生后，外部观察者（小模型）算出的 delta 应用到状态。

    delta = {connection: -0.15, pride: -0.1, valence: +0.2, arousal: +0.05, immersion: 0}
    所有字段可选，缺省为 0。immersion 通常由 set_activity 管理，delta 一般不传。
    """
    return State(
        connection=_clamp(state.connection + float(delta.get("connection", 0)), 0.0, 1.0),
        pride=_clamp(state.pride + float(delta.get("pride", 0)), -1.0, 1.0),
        valence=_clamp(state.valence + float(delta.get("valence", 0)), -1.0, 1.0),
        arousal=_clamp(state.arousal + float(delta.get("arousal", 0)), 0.0, 1.0),
        immersion=_clamp(state.immersion + float(delta.get("immersion", 0)), 0.0, 1.0),
        last_tick_ts=state.last_tick_ts,
        last_user_msg_ts=state.last_user_msg_ts,
        last_delta_ts=state.last_delta_ts,
        last_user_msg_meta=dict(state.last_user_msg_meta),
        last_activity=dict(state.last_activity),
    )


def get_triggers(state: State, thresholds: Thresholds) -> list[dict]:
    """根据当前状态返回触发动作列表。

    动作类型：
      observation         - connection 在 notice 阶段，生成内心念头但不发出
      contact             - connection 在 consider 阶段，pride 不阻断时开口
      forced              - connection 达到 forced 阈值，无视 pride 强制开口
      pride_block         - pride 高且 immersion 也高，已经在做事不需要新活动
      find_activity       - 嘴硬（pride 高+immersion 低）/ 心情差 / 焦躁时主动找事做
                            附带 reason ∈ {pride_block, low_valence, high_arousal}
                            和 suggested_activity_type ∈ {reading, cooking, browse, search, observe, selfcare}
    """
    out = []
    c = state.connection
    p = state.pride
    v = state.valence
    a = state.arousal
    i = state.immersion

    # ─── connection 阶段 ────────────────────────────
    if c >= thresholds.forced:
        out.append({"action": "forced", "urgency": c, "reason": "connection_forced"})
    elif c >= thresholds.consider:
        if p >= thresholds.pride_block:
            if i < thresholds.immersion_block:
                # 嘴硬 + 没在做事 → 找事做（转移注意）
                out.append({
                    "action": "find_activity",
                    "urgency": c,
                    "reason": "pride_block",
                    "suggested_activity_type": "reading",  # 嘴硬偏看书
                })
            else:
                # 嘴硬但已经在做事 → 仅记录沉默理由，不需要新 activity
                out.append({"action": "pride_block", "urgency": c, "reason": "pride_too_high_immersed"})
        else:
            out.append({"action": "contact", "urgency": c, "reason": "consider_threshold"})
    elif c >= thresholds.notice:
        out.append({"action": "observation", "urgency": c, "reason": "notice_threshold"})

    # ─── 独立 find_activity 分支（任何 c 阶段都可叠加触发）────
    has_find_activity = any(t.get("action") == "find_activity" for t in out)
    if not has_find_activity and i < 0.3:
        if v <= thresholds.valence_activity:
            # 心情差 → 找事做安抚自己
            out.append({
                "action": "find_activity",
                "urgency": min(1.0, abs(v)),
                "reason": "low_valence",
                "suggested_activity_type": "cooking",  # 心情差偏做饭/敷面膜（具身安抚）
            })
        elif a >= thresholds.arousal_agitation:
            # 焦躁 → 找事做转移注意
            out.append({
                "action": "find_activity",
                "urgency": min(1.0, a),
                "reason": "high_arousal",
                "suggested_activity_type": "browse",  # 焦躁偏刷手机/逛
            })

    return out


def set_activity(state: State, activity_type: str, label: str, rates: Rates) -> State:
    """设置当前 bot 在做的活动。

    - 查 rates.immersion_map 拿到该活动初始沉浸度
    - 写 state.last_activity = {type, label, started_ts, initial_immersion}
    - 设 state.immersion = 初始沉浸度
    - 不动其他 4 维（让 drift 通过 activity_connection_relief 自然缓解 connection）

    冷却保护：若 state.immersion > 0.2 已在做事，拒绝覆盖（防止反复重置 immersion）。
    返回新 State；若被冷却拒绝则返回原 state（不变）。
    """
    if state.immersion > 0.2:
        # 已经在做事，不覆盖
        return state

    initial_immersion = float(rates.immersion_map.get(activity_type, 0.4))

    return State(
        connection=state.connection,
        pride=state.pride,
        valence=state.valence,
        arousal=state.arousal,
        immersion=_clamp(initial_immersion, 0.0, 1.0),
        last_tick_ts=state.last_tick_ts,
        last_user_msg_ts=state.last_user_msg_ts,
        last_delta_ts=state.last_delta_ts,
        last_user_msg_meta=dict(state.last_user_msg_meta),
        last_activity={
            "type": activity_type,
            "label": label,
            "started_ts": int(time.time()),
            "initial_immersion": initial_immersion,
        },
    )


def get_style_guidance(state: State) -> str:
    """根据 5 维状态返回"语气指导"——给 LLM 看怎么说话（与 get_state_description 平行）。

    与 get_state_description 区别：
      - get_state_description 描述"为什么"（内心念头/动机）
      - get_style_guidance 描述"怎么说"（句式/标点/字数倾向）

    朋友圈 MOMENT_PROMPT 和 dm worker 都注入这段。平稳态返空串以省 token。
    """
    parts = []
    c, p, v, a, i = state.connection, state.pride, state.valence, state.arousal, state.immersion

    # 嘴硬 + 黏
    if c >= 0.6 and p >= 0.5:
        parts.append("嘴硬、傲娇但黏人")
    elif c >= 0.5 and p < 0:
        parts.append("依恋、卑微、爱撒娇")

    # 心情维度
    if v >= 0.4 and p >= 0:
        parts.append("轻快，多用感叹号和波浪号")
    elif v <= -0.4 and a >= 0.5:
        parts.append("烦躁，多用反问")
    elif v <= -0.4 and a < 0.3:
        parts.append("话少而平淡，省略号偏多，不用感叹号")
    elif v <= -0.2:
        parts.append("情绪偏低，语气偏冷，反应略慢")

    # 沉浸度
    if i >= 0.5:
        parts.append("在忙，回复偏慢，会提到手头的事")

    # arousal 高（非负面）
    if a >= 0.6 and v >= 0:
        parts.append("兴奋，语速快，标点密集")

    if not parts:
        return ""  # 平稳态返空串省 token
    return "；".join(parts) + "。"


def get_state_description(state: State) -> str:
    """状态 → 自然语言描述（注入 judge prompt 用）。

    每条规则独立判断，按重要性顺序加入。最多 3-4 条，控制长度。
    """
    rules = []
    c = state.connection
    p = state.pride
    v = state.valence
    a = state.arousal
    i = state.immersion

    # 主导情绪：connection
    if c >= 0.50:
        rules.append("快忍不住了，想主动说话")
    elif c >= 0.35 and p >= 0.5:
        rules.append("别扭，想找又傲娇，说话时语气会有埋怨")
    elif c >= 0.35:
        rules.append("想找 user 说话，正找理由")
    elif c >= 0.20:
        rules.append("开始注意到沉默，但还忍得住")

    # 骄傲单独触发
    if p >= 0.7 and c < 0.50:
        rules.append("嘴硬，想了也不承认")

    # valence/arousal 修饰
    if v < -0.3 and a > 0.3:
        rules.append("烦躁，坐不住")
    elif v < -0.3:
        rules.append("情绪低落，话少且平")
    elif v > 0.3:
        rules.append("心情还不错")

    # 沉浸度（第 5 维 immersion；以前误用 arousal —— 越激动越被描述成"在忙着别的事"）
    if i >= 0.5:
        rules.append("专注在手头的事上，不急着找 user")

    if not rules:
        return "平稳，没什么挂念"
    return "；".join(rules) + "。"


# ─── 序列化辅助 ──────────────────────────────────────────────

def state_to_dict(state: State) -> dict:
    return asdict(state)


def _num(d: dict, k: str, default, cast=float, _bad: list | None = None):
    """逐字段容错取数：单个值损坏只回落该字段，不牵连其余状态。

    以前是裸 float(d.get(...))，一个 null 或手抖打的引号就会抛异常，
    被调用方的 try/except 吞成"整份状态回默认"——bot 的情绪被静默清零且无人知晓。
    缩到单字段之后**静默那一半还在**，所以坏值（不是缺键）要把键名记进 _bad 让上层喊一声：
    缺键是正常的向后兼容（老 state 文件本来就没有新字段），坏值才是异常。
    """
    try:
        return cast(d[k])
    except KeyError:
        return default
    except (TypeError, ValueError):
        if _bad is not None:
            _bad.append(k)
        return default


def _dict_field(d: dict, k: str, _bad: list | None = None) -> dict:
    v = d.get(k)
    if isinstance(v, dict):
        return dict(v)
    if v is not None and _bad is not None:
        _bad.append(k)
    return {}


def state_from_dict(d: dict) -> State:
    """任何输入都不抛：坏字段逐个回落默认值，好字段照常读出。

    回落是**可见**的：坏字段的键名会打一行 stderr。只打键名不打值——
    last_user_msg_meta 之类可能带对话内容，不进日志。
    """
    if not isinstance(d, dict):
        d = {}
    bad: list[str] = []
    last_user_msg_ts = _num(d, "last_user_msg_ts", 0, int, bad)
    state = State(
        connection=_num(d, "connection", 0.0, float, bad),
        pride=_num(d, "pride", 0.0, float, bad),
        valence=_num(d, "valence", 0.0, float, bad),
        arousal=_num(d, "arousal", 0.0, float, bad),
        immersion=_num(d, "immersion", 0.0, float, bad),   # 老 state 文件兼容默认 0
        last_tick_ts=_num(d, "last_tick_ts", 0, int, bad),
        last_user_msg_ts=last_user_msg_ts,
        # 缺 last_delta_ts（现存生产 state 文件就是这样）时回落到 last_user_msg_ts：
        # 旧字段的旧语义就是"只有 delta 成功才推进"，与新字段同构，直接继承即可。
        # 回落成 0 会让升级后第一个 tick 把整个窗口当新对话重算一遍（重复计分）。
        last_delta_ts=_num(d, "last_delta_ts", last_user_msg_ts, int, bad),
        last_user_msg_meta=_dict_field(d, "last_user_msg_meta", bad),
        last_activity=_dict_field(d, "last_activity", bad),   # 老 state 兼容默认 {}
    )
    if bad:
        # 字段级损坏正好绕过 load_state 的"加载状态失败"（那条只在 JSON 语法错时响），
        # 不喊一声就等于把一类可观测故障变成不可观测故障。
        print("[jiwen.engine] state 字段损坏已回落默认: " + ",".join(bad), file=sys.stderr)
    return state
