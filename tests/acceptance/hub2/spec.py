# -*- coding: utf-8 -*-
"""INTERFACE-hub2 §2.2 的键表 + 测试用 _global.yml 样本（普通模块，非测试文件）。

§2.2 原话：「表单只放这些，**逐键写死，测试直接照此断言**」。
这份表就是那张表的机器可读拷贝，测试只从这里取，改契约就改这一处。
"""

# (点分键, 类型, 下界, 上界)
FLOAT_0_1 = [
    "connection_per_min", "pride_decay_per_min", "valence_decay_per_min",
    "arousal_decay_per_min", "valence_lock_factor", "arousal_connection_rise_rate",
    "pride_defend_rate", "pride_arousal_conflict_rate", "pride_erosion_rate",
    "immersion_decay_per_min", "activity_connection_relief",
]
FLOAT_0_5 = ["connection_accel", "valence_severe_multiplier", "valence_mild_multiplier"]
FLOAT_NEG1_1 = [
    "valence_severe_low", "valence_mild_low", "valence_lock_threshold",
    "arousal_connection_rise_threshold", "pride_defend_threshold", "pride_defend_target",
]
IMMERSION = ["reading", "cooking", "search", "browse", "observe", "selfcare"]
THRESHOLDS = ["notice", "consider", "forced"]

KEYS = []
KEYS += [(f"jiwen.rates.{k}", "float", 0.0, 1.0) for k in FLOAT_0_1]
KEYS += [(f"jiwen.rates.{k}", "float", 0.0, 5.0) for k in FLOAT_0_5]
KEYS += [(f"jiwen.rates.{k}", "float", -1.0, 1.0) for k in FLOAT_NEG1_1]
KEYS += [("jiwen.rates.accel_threshold_min", "float", 0.0, 1440.0)]
KEYS += [(f"jiwen.rates.immersion_map.{k}", "float", 0.0, 1.0) for k in IMMERSION]
KEYS += [(f"jiwen.thresholds.{k}", "float", 0.0, 1.0) for k in THRESHOLDS]
KEYS += [("jiwen.tick_interval_min", "int", 1, 60)]
KEYS += [("moments.enabled", "bool", None, None)]
KEYS += [("moments.daily_post_limit_per_bot", "int", 0, 50)]
KEYS += [("moments.silence_threshold_minutes", "int", 1, 1440)]
KEYS += [("max_calls_per_5h", "int", 1, 1000)]

KEY_SET = {k for k, _, _, _ in KEYS}
BY_KEY = {k: (t, lo, hi) for k, t, lo, hi in KEYS}

# §2.2 明写「不进表单」的 Thresholds 另 5 个字段（出现在 schema 里即契约违反）
FORBIDDEN_IN_SCHEMA = [
    "jiwen.thresholds.pride_block", "jiwen.thresholds.valence_activity",
    "jiwen.thresholds.arousal_agitation", "jiwen.thresholds.immersion_block",
]

# §3.2 检查 5 的四个必需顶层键
REQUIRED_TOP_KEYS = ["user_display_name", "max_calls_per_5h", "moments", "jiwen"]

PLACEHOLDER = "__KEEP_EXISTING__"
# §3.1 用例要求：40 位无前缀随机串，断言它不出现在任何响应/日志里
SECRET_KEY = "Zq7Z4nR2xK9mT1bV6cL0wY8dH3jF5gS2aP4eU7iO"

# ---- 测试用 _global.yml 样本 ----------------------------------------------
# 刻意塞满 F2/K8 要保真的东西：注释、锚点 &base、别名 *base、merge key <<:、
# 块标量 |、`0.20` 这种数值书写格式、非 ASCII、以及一个敏感 key。
_YML = """\
# ===== claudebotlife 全局配置 =====
# 这一行注释是 F2 字节保真断言的锚：任何一次保存后它必须一字不动
user_display_name: 哥哥
max_calls_per_5h: 100
webhook_secret: s3cr3t-do-not-echo-me

_defaults: &base          # 锚点：K8 要求原样保留，不许被展开
  retry: 3
  timeout_sec: 30

moments:
  enabled: true
  daily_post_limit_per_bot: 5
  silence_threshold_minutes: 30   # 静默多久算冷场

jiwen:
  tick_interval_min: 5
  delta_llm:
    <<: *base               # merge key：K8
    api_key: __SECRET__
    endpoint: https://api.deepseek.com/v1
  prompt: |
    这是块标量，K8 要求不被展开、不被重排。
    第二行照旧。
  thresholds:
    notice: 0.20            # `0.20` 不许在保存后变成 `0.2`
    consider: 0.35
    forced: 0.50
    pride_block: 0.40       # §2.2：不进表单
    valence_activity: -0.30
    arousal_agitation: 0.70
    immersion_block: 0.60
"""

RATES = {
    "connection_per_min": 0.0007, "pride_decay_per_min": 0.002,
    "valence_decay_per_min": 0.003, "arousal_decay_per_min": 0.004,
    "valence_lock_factor": 0.5, "arousal_connection_rise_rate": 0.01,
    "pride_defend_rate": 0.02, "pride_arousal_conflict_rate": 0.03,
    "pride_erosion_rate": 0.04, "immersion_decay_per_min": 0.05,
    "activity_connection_relief": 0.06,
    "connection_accel": 1.0, "valence_severe_multiplier": 0.3,
    "valence_mild_multiplier": 1.5,
    "valence_severe_low": -0.6, "valence_mild_low": -0.3,
    "valence_lock_threshold": -0.45, "arousal_connection_rise_threshold": 0.55,
    "pride_defend_threshold": -0.2, "pride_defend_target": 0.1,
    "accel_threshold_min": 120.0,
}
IMMERSION_VALUES = {"reading": 0.6, "cooking": 0.2, "search": 0.3,
                    "browse": 0.4, "observe": 0.5, "selfcare": 0.7}
THRESHOLD_VALUES = {"notice": 0.2, "consider": 0.35, "forced": 0.5}


def global_yml(secret=SECRET_KEY):
    """样本原文。`secret` 是要断言"永不回显"的那个 40 位串。"""
    body = _YML.replace("__SECRET__", secret)
    body += "  rates:\n"
    for k, v in RATES.items():
        body += f"    {k}: {v!r}\n"
    body += "    immersion_map:\n"
    for k, v in IMMERSION_VALUES.items():
        body += f"      {k}: {v!r}\n"
    return body


EXPECTED_VALUES = {}
EXPECTED_VALUES.update({f"jiwen.rates.{k}": v for k, v in RATES.items()})
EXPECTED_VALUES.update({f"jiwen.rates.immersion_map.{k}": v
                        for k, v in IMMERSION_VALUES.items()})
EXPECTED_VALUES.update({f"jiwen.thresholds.{k}": v for k, v in THRESHOLD_VALUES.items()})
EXPECTED_VALUES["jiwen.tick_interval_min"] = 5
EXPECTED_VALUES["moments.enabled"] = True
EXPECTED_VALUES["moments.daily_post_limit_per_bot"] = 5
EXPECTED_VALUES["moments.silence_threshold_minutes"] = 30
EXPECTED_VALUES["max_calls_per_5h"] = 100
