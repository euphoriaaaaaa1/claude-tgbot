# -*- coding: utf-8 -*-
"""生日/学制验收测试的共享工具（普通模块，不是 conftest、不是测试文件）。

只用公开入口和惰性假数据。配置目录整体指向 tmp_path，真实 configs/ 一个字节都不碰。
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def use_fake_configs(tmp_path, monkeypatch):
    """把 config_loader 的配置目录指向临时目录，返回该目录。

    公开版 config_loader 用模块级常量定位配置（无 env 接缝），因此这里改常量。
    配套的 write_bot_config / write_global_config 负责往里写假配置。
    """
    import config_loader

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", str(configs_dir))
    monkeypatch.setattr(config_loader, "GLOBAL_CFG_PATH", str(configs_dir / "_global.yml"))
    return configs_dir


def write_bot_config(configs_dir, bot_id, data):
    path = Path(configs_dir) / f"{bot_id}.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def write_global_config(configs_dir, data):
    path = Path(configs_dir) / "_global.yml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ---- formatter.format_self_initiate 的最小惰性夹具 ----

class _DummyWorld:
    def __init__(self):
        self.date_info = {}
        self.weather = None
        self.matched_news = None
        self.on_this_day = None

    def __getattr__(self, name):
        return None


class _DummyScene:
    def __init__(self):
        self.name = "测试场景"
        self.id = "test-scene"
        self.desc = "测试描述"
        self.recurring = type("Recurring", (), {"name": "日常", "description": "普通一天"})()
        self.sporadic = None
        self.hobby = None

    def __getattr__(self, name):
        return None


class _DummyWildcard:
    card = "测试牌"
    emotion = "平静"


def render_self_initiate(identity: str = "") -> str:
    """用固定假数据跑一次公开入口 formatter.format_self_initiate。"""
    import formatter

    return formatter.format_self_initiate(
        now=_dt.datetime(2026, 8, 22, 12, 0),
        situation=_DummyScene(),
        world=_DummyWorld(),
        wildcard=_DummyWildcard(),
        mood=0.5,
        mood_factors="",
        since_user_min=5,
        recent_msgs="",
        identity=identity,
    )
