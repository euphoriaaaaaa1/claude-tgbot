"""壳读 ``_global.yml`` 的唯一入口（BRIEF-merge-back §5：全局参数文件属于共用数据）。

只读这一个文件，不碰同目录下任何 bot 配置（BRIEF §3"不读两个配置根"）。路径规则与
``config_loader.global_cfg_path`` 相同：``CLAUDEBOT_GLOBAL_CFG`` → ``CLAUDEBOT_CONFIG_DIR/_global.yml``
→ 仓库 ``configs/_global.yml``。每次调用现读环境变量，测试换临时目录不会串到真文件。
"""
import os

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path() -> str:
    explicit = os.environ.get("CLAUDEBOT_GLOBAL_CFG")
    if explicit:
        return explicit
    configs = os.environ.get("CLAUDEBOT_CONFIG_DIR") or os.path.join(_REPO_ROOT, "configs")
    return os.path.join(configs, "_global.yml")


def load() -> dict:
    """文件不存在、读不出、不是映射 → 空表（页面用回落值照常显示，不因全局参数坏了打不开）。"""
    try:
        with open(path(), "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}
