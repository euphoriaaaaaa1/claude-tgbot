# -*- coding: utf-8 -*-
"""settings.json 的唯一读写落点（INTERFACE §3.6 C 阶段 / §5）。

**路径一律由调用方传入**（口径见 provider_model.settings_path()），本模块绝不自己
expanduser 到 ~/.claude/settings.json —— 那正是"测试踩到真实配置"的事故成因。

写是 tmp + rename：mkstemp 随机名 + 0600 一次建成，fsync 后 os.replace 原子替换，
finally 清理残留。临时文件里有 ANTHROPIC_AUTH_TOKEN 明文，所以不能用可预测的固定名
（settings.json.hubtmp 会被并发 activate 互相覆盖，[R4D] I9），也不能"先建再 chmod"
（那有一个 644 的窗口，PLAN 6.1 已点名过 _write_novelai_token）。
"""
import json
import os
import stat
import tempfile
import time

from moments.provider_model import HubError

_REPLACE_RETRIES = 3          # §3.6 C2：Windows 上文件被占用时重试 3×200ms
_REPLACE_DELAY = 0.2


def read_text(path):
    """读全文；文件不存在返回 None（§3.6：视为 {} 并新建，不是错误）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # 不是 UTF-8（Windows 记事本另存的 GBK/带 BOM 很常见）。解码是解析的一部分，
        # 按坏 JSON 同路走 409，用户改完文件即可自愈；500 会让人以为是门户坏了。
        raise HubError(409, "settings_unparsable", "settings.json 不是 UTF-8 编码")
    except OSError as e:
        raise HubError(500, "config_unreadable", "settings.json 读取失败：%s" % type(e).__name__)


def dir_writable(path):
    """§3.6 A3：探 settings.json 所在目录可写（不存在则探父目录）。只读探测，无副作用。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    while not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    return os.access(d, os.W_OK | os.X_OK)


def _file_mode(path):
    """沿用原文件权限；文件不存在时才 0600（[R4D] B3：别把用户原本 644 的文件悄悄改成 600）。"""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return 0o600


def write_json_atomic(path, data):
    """把 data 原子写进 path。失败一律抛 HubError，且不留临时文件（里面有明文令牌）。

    | 失败 | 状态码 | error |
    |---|---|---|
    | 建/写临时文件失败、os.replace 抛 OSError | 500 | settings_write_failed |
    | Windows 上文件被占用，重试 3 次仍 PermissionError | 409 | settings_write_locked |
    """
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = None
    try:
        # mkstemp 一次就建成 0600 的随机名文件，中间没有别人可读的窗口
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())            # 崩溃安全：内容先落盘，再让 rename 生效
        os.chmod(tmp, _file_mode(path))
        _replace(tmp, path)
        tmp = None                          # 换名成功，没有残留可清
    except OSError as e:                    # HubError（Windows 被占用）不是 OSError，照常冒泡
        raise HubError(500, "settings_write_failed",
                       "写 settings.json 失败：%s" % type(e).__name__)
    finally:
        if tmp:
            try:                            # §3.6 C3：残留清理是尽力而为，失败不改变返回码
                os.unlink(tmp)
            except OSError:
                pass


def _replace(tmp, path):
    """os.replace 原子替换。**必须按属性调 os.replace**（用例靠 monkeypatch os.replace 构造落盘失败）。

    Windows 上被占用会抛 PermissionError，重试 3×200ms；仍失败 → 409 settings_write_locked
    （POSIX 上的 PermissionError 是真的没权限，不重试，走 500 settings_write_failed）。
    """
    if os.name != "nt":
        os.replace(tmp, path)
        return
    for i in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == _REPLACE_RETRIES - 1:
                raise HubError(409, "settings_write_locked",
                               "settings.json 被其他程序占用，请重试")
            time.sleep(_REPLACE_DELAY)
