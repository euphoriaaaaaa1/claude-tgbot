"""加载 bot 配置和全局配置。"""
import os
import yaml

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(PROJECT_ROOT, "configs")
GLOBAL_CFG_PATH = os.path.join(CONFIGS_DIR, "_global.yml")


def load_global() -> dict:
    if not os.path.exists(GLOBAL_CFG_PATH):
        return {}
    with open(GLOBAL_CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_bot(bot_id: str) -> dict:
    p = os.path.join(CONFIGS_DIR, f"{bot_id}.yml")
    if not os.path.exists(p):
        raise FileNotFoundError(f"配置不存在：{p}")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_enabled_bots() -> list[dict]:
    """枚举所有 configs/<bot>.yml（除 _ 开头的）。"""
    bots = []
    if not os.path.isdir(CONFIGS_DIR):
        return bots
    for fn in sorted(os.listdir(CONFIGS_DIR)):
        if fn.startswith("_") or not fn.endswith(".yml"):
            continue
        bot_id = fn[:-4]
        try:
            cfg = load_bot(bot_id)
            cfg["_bot_id"] = bot_id
            bots.append(cfg)
        except Exception:
            continue
    return bots


def in_sleep_hours(cfg: dict, now) -> bool:
    """now 在 sleep_hours 任一区间内 → True"""
    from generators.situation import _time_in_range
    for spec in cfg.get("sleep_hours", []):
        if _time_in_range(now.time(), spec):
            return True
    return False
