# -*- coding: utf-8 -*-
"""令牌保护与日志安全工具。

只依赖标准库，不依赖 Flask；蓝图、适配器、任务记录都从这里取：

- Telegram 令牌的正则只在本模块定义（打码用的 ``SCRUB_RE`` 与校验形状用的 ``TOKEN_RE``——
  ``TOKEN_RE`` 实际放在 ``base`` 里，与 ``SecretToken`` 同处，本模块原样转出）；
- ``scrub``：返回、任务记录、日志里的每段文字先过它；
- ``SecretToken``：令牌在内存里只以它传递，任何打印、格式化都只给出占位文字，不能序列化；
- ``assert_loopback``：带令牌的请求发出前断言对方是本机回环。

日志安全的口径：任何要写日志的字符串（尤其是底层返回的 message、异常原文、请求体的
回显）都先过 ``scrub``，日志里不得出现令牌原文。
"""
import ipaddress
import re
import socket

from .base import HIDDEN, TOKEN_FORMAT_MESSAGE, TOKEN_RE, SecretToken  # noqa: F401  （对外转出）

# 打码：文字里任何像 Telegram 令牌的子串。全仓只在这里定义。
SCRUB_RE = re.compile(r"[0-9]{5,12}:[A-Za-z0-9_-]{30,}")

_LOOPBACK_LITERALS = ("127.0.0.1", "::1")
_getaddrinfo = socket.getaddrinfo   # 解析注入点：单测换成假解析，不碰本机真实解析


def scrub(text) -> str:
    """把文字里像令牌的子串换成 ``<令牌已隐藏>``。None 给空串；不是字符串的先转成字符串再打码
    （异常对象、``SecretToken`` 之类传进来也不会漏）。"""
    if text is None:
        return ""
    return SCRUB_RE.sub(HIDDEN, text if isinstance(text, str) else str(text))


class NotLoopbackError(ValueError):
    """对方地址不是本机回环：带令牌的请求不许发出。"""

    def __init__(self):
        super().__init__("对方不是本机回环地址，不发送")


def _is_loopback_addr(addr) -> bool:
    # IPv6 链路本地地址带 "%网卡" 后缀（如 fe80::1%lo0），先去掉再判断；判断不了按不是回环
    try:
        return ipaddress.ip_address(str(addr).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def loopback_address(host) -> str:
    """返回可连接的字面回环地址（构造期固定，后续不再解析，消除 TOCTOU）：

    字面 ``127.0.0.1``/``::1`` 原样；``localhost`` 解析结果必须全是回环，优先 IPv4，否则第一个
    回环地址。其他写法或解析出非回环都抛 ``NotLoopbackError``。"""
    if not isinstance(host, str):
        raise NotLoopbackError()
    if host in _LOOPBACK_LITERALS:
        return host
    if host != "localhost":
        raise NotLoopbackError()
    try:
        infos = _getaddrinfo("localhost", None)
    except (OSError, UnicodeError):
        raise NotLoopbackError() from None
    addrs = [str(info[4][0]) for info in infos]
    if not addrs or not all(_is_loopback_addr(a) for a in addrs):
        raise NotLoopbackError()
    for addr in addrs:
        if ":" not in addr:
            return addr
    return addrs[0]


def assert_loopback(host) -> None:
    """``host`` 必须是字面 ``127.0.0.1``、``::1``，或 ``localhost`` 且解析结果全是回环；否则抛
    ``NotLoopbackError``。

    只认这三种写法（大小写、末尾的点、方括号、别的 127.x 写法一律不认）：宁可拒发，不可错发。
    ``localhost`` 解析失败或一个结果都没有也拒绝。"""
    if not isinstance(host, str):
        raise NotLoopbackError()
    if host in _LOOPBACK_LITERALS:
        return
    if host == "localhost":
        try:
            infos = _getaddrinfo("localhost", None)
        except (OSError, UnicodeError):
            raise NotLoopbackError() from None
        addrs = [info[4][0] for info in infos]
        if addrs and all(_is_loopback_addr(a) for a in addrs):
            return
    raise NotLoopbackError()
