# -*- coding: utf-8 -*-
"""``~/.claude-tgbot/hub.env`` 的读侧公共实现（INTERFACE §0.3）。

三平台的自启路径（launchd plist / systemd unit / Windows 计划任务）**都不 source
这个文件**，所以任何"只读 os.environ"的配置项在出货机器上恒为空。
门户的 cliproxy 三键与两把口令都在这个文件里，读法必须只有一份 ——
两份读法迟早只修好一份（安审 F1 / BUG-21 就是这么来的）。

与 ``scripts/hub_bootstrap.py:parse_env_file`` 是同格式的读侧孪生，**有意不 import 它**：
``scripts/`` 不是包、且是安装期脚本，运行期的 ``moments/`` 不该反向依赖它。
读失败一律回空 —— 页面必须能打开（§2 铁律），读不到就是"没配"。
"""
import os


def hub_env_path(env=None):
    """``HUB_ENV_FILE`` 覆盖优先（测试必须用它指向 tmp，否则会读到机主真实凭据）。"""
    env = os.environ if env is None else env
    override = env.get("HUB_ENV_FILE")
    if override:
        return os.path.expanduser(override)
    return os.path.join(tgbot_home(env), "hub.env")


def tgbot_home(env=None):
    """口径与 ``hub_bootstrap.tgbot_home()`` 一致：``CLAUDE_TGBOT_HOME`` 覆盖优先。"""
    env = os.environ if env is None else env
    return os.path.expanduser(env.get("CLAUDE_TGBOT_HOME") or "~/.claude-tgbot")


def parse(path):
    """``KEY=VALUE`` 逐行解析，忽略 ``#`` 开头与空行。读不出来一律回空 dict。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def get(key, env=None):
    """先进程环境、后 hub.env。**取不到回空串**，调用方不必区分"没设"与"设成空"。

    每次现读不缓存：改文件重启进程即生效，测试也好隔离。
    hub.env 是一行行的小文件，这点 IO 换掉一整类"改了没生效"的支持成本。
    """
    env = os.environ if env is None else env
    got = (env.get(key) or "").strip()
    if got:
        return got
    return (parse(hub_env_path(env)).get(key) or "").strip()
