"""Phase 4：薄封装，给 life-context.py 用。

只读 state 文件 + 调 engine 拿 triggers/description，不做 drift（tick 已经在做）。
读失败一律返回 None — 调用方应当做 jiwen 未启用处理。
"""
from __future__ import annotations
import os
import json
from typing import Optional

from . import engine


DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.jiwen-state")


def _state_path(state_dir: str, bot_id: str, chat_id: str) -> str:
    return os.path.join(state_dir, f"{bot_id}-{chat_id}.json")


def read(bot_id: str, chat_id: str, global_cfg: dict) -> Optional[dict]:
    """读 jiwen state 并附带 triggers / description。

    返回：{
        "state":       engine.State,
        "triggers":    list[dict],
        "description": str,
        "forced":      bool,        # 是否有 forced trigger
        "pride_block": bool,        # 是否被 pride 阻断
    }
    若 jiwen 未启用 / 文件不存在 / 解析失败 → None。
    """
    jcfg = (global_cfg or {}).get("jiwen") or {}
    if not jcfg.get("enabled"):
        return None

    state_dir = jcfg.get("state_dir") or DEFAULT_STATE_DIR
    path = _state_path(state_dir, bot_id, chat_id)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = engine.state_from_dict(data)
    except Exception:
        return None

    # thresholds 走 _global.yml 配置；缺省走 engine 默认
    # float() 强转防御 YAML 偶尔被写成字符串
    th_cfg = jcfg.get("thresholds") or {}
    try:
        thresholds = engine.Thresholds(
            notice=float(th_cfg.get("notice", 0.20)),
            consider=float(th_cfg.get("consider", 0.35)),
            forced=float(th_cfg.get("forced", 0.50)),
            pride_block=float(th_cfg.get("pride_block", 0.5)),
            valence_activity=float(th_cfg.get("valence_activity", -0.6)),
            arousal_agitation=float(th_cfg.get("arousal_agitation", 0.7)),
            immersion_block=float(th_cfg.get("immersion_block", 0.3)),
        )
    except (TypeError, ValueError):
        thresholds = engine.Thresholds()  # fallback to defaults

    triggers = engine.get_triggers(state, thresholds)
    description = engine.get_state_description(state)
    # 平稳态描述无信息增益，返空避免 token 浪费
    if description.startswith("状态平稳"):
        description = ""

    # 提取首个 find_activity trigger（life-context.py 据此调 set_activity）
    find_activity = next((t for t in triggers if t.get("action") == "find_activity"), None)

    return {
        "state": state,
        "triggers": triggers,
        "description": description,
        "style_guidance": engine.get_style_guidance(state),
        "find_activity": find_activity,
        "forced": any(t.get("action") == "forced" for t in triggers),
        "pride_block": any(t.get("action") in ("pride_block", "pride_too_high_immersed") for t in triggers),
    }
