"""Telegram getMe 客户端（INTERFACE-bot-lifecycle 3.3.5、3.0 第 14 条；PLAN D12、R13）。

只用标准库：``http.client`` 自己控制连接与日志，不走 requests / urllib3。
- 生产地址 ``https://api.telegram.org``，只经本机代理的 CONNECT 隧道；
- 测试接缝 ``CONSOLE_TELEGRAM_API_BASE`` 只许回环（直接连，不经代理）；
- 10 秒超时、不跟随重定向（http.client 本身不跟随）；
- 请求地址里带令牌：任何异常文本、日志都不含令牌，只抛固定的三类异常；
- 限速：同一登录会话 60 秒内最多 10 次 getMe（校验令牌、新建、切换三处共用这一份）。
"""
import http.client
import json
import logging
import os
import ssl
import threading
import time
from urllib.parse import urlsplit

from adapters.guard import NotLoopbackError, TOKEN_RE, SecretToken, assert_loopback

DEFAULT_API_BASE = "https://api.telegram.org"
DEFAULT_PROXY = "http://127.0.0.1:7897"   # 本机 Clash 的 HTTP 代理（与 holiday.py 的约定一致）
TIMEOUT = 10
MAX_BODY = 64 * 1024
RATE_WINDOW = 60.0
RATE_MAX = 10

# 界面与任务里用的固定文案（3.0 第 17 条；令牌形状那句在 adapters.guard 唯一定义）
REJECTED_MESSAGE = "Telegram 不认这个令牌，请到 BotFather 复制完整的令牌"
UNREACHABLE_MESSAGE = "连不上 Telegram（经本机代理），请检查代理后重试"
TOO_MANY_MESSAGE = "校验令牌太频繁，请一分钟后再试"

# 这些库若被别处导入，日志级别固定在 WARNING 以上；http.client 的调试输出关闭（3.3.5）。
for _name in ("urllib3", "requests"):
    logging.getLogger(_name).setLevel(logging.WARNING)
http.client.HTTPConnection.debuglevel = 0

_hits: dict = {}
_hits_lock = threading.Lock()


class TokenRejected(Exception):
    """Telegram 拒绝了令牌（401/404、ok:false、result.is_bot 不为 true）。"""


class TelegramUnreachable(Exception):
    """连不上、超时、代理失败、回体看不懂。"""


class RateLimited(Exception):
    """同一会话 60 秒内第 11 次 getMe。"""


def parse_token(value):
    """去掉首尾空白后合令牌形状 → ``SecretToken``；否则 None。形状规则只在 adapters.guard 定义。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if TOKEN_RE.fullmatch(value) is None:
        return None
    return SecretToken(value)


def rate_key_of(cookie_value) -> str:
    """限速键：登录会话 cookie 的原值（登录门保证有）；没有时退回一个固定键（同进程共用）。"""
    return cookie_value if isinstance(cookie_value, str) and cookie_value else "_anonymous"


def _allow(key: str) -> bool:
    """记一次 getMe 并判断放不放行：60 秒窗口里第 11 次起拒绝。count 的是真正要发的请求。"""
    now = time.monotonic()
    with _hits_lock:
        recent = [t for t in _hits.get(key, ()) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_MAX:
            _hits[key] = recent
            return False
        recent.append(now)
        _hits[key] = recent
        # 顺手清掉别的会话的过期记录，免得限速表无界增长
        for k in [k for k, v in _hits.items() if k != key and not any(now - t < RATE_WINDOW for t in v)]:
            _hits.pop(k, None)
        return True


def _proxy_parts():
    """本机代理地址：``https_proxy``/``HTTPS_PROXY`` 优先，缺省 127.0.0.1:7897。
    只许回环；不是回环就不发请求（免得把带令牌的地址交给别的机器）。"""
    raw = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or DEFAULT_PROXY
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    if parts.scheme != "http" or not parts.hostname:
        raise TelegramUnreachable()
    try:
        assert_loopback(parts.hostname)
    except NotLoopbackError:
        raise TelegramUnreachable() from None
    return parts.hostname, parts.port or 80


def _connection(base: str):
    """按基址建连接，返回 (连接, 路径前缀)。基址来自接缝时只许回环。"""
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise TelegramUnreachable()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    prefix = parts.path.rstrip("/")
    if parts.scheme == "http":
        try:
            assert_loopback(parts.hostname)
        except NotLoopbackError:
            raise TelegramUnreachable() from None
        return http.client.HTTPConnection(parts.hostname, port, timeout=TIMEOUT), prefix
    # https 只许 Telegram 官方主机（默认基址）与 443：接缝被改成别的 https 主机时不发请求（安全审计重要 5）
    if parts.hostname != "api.telegram.org" or parts.port not in (None, 443):
        raise TelegramUnreachable()
    proxy_host, proxy_port = _proxy_parts()
    conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=TIMEOUT,
                                       context=ssl.create_default_context())
    conn.set_tunnel(parts.hostname, port)
    return conn, prefix


def _api_base() -> str:
    return os.environ.get("CONSOLE_TELEGRAM_API_BASE") or DEFAULT_API_BASE


def _rejected_result(data) -> bool:
    """回体是不是明确不认这个令牌：ok 不为 true、is_bot 不为 true、或取不到用户名/编号。"""
    if not isinstance(data, dict) or data.get("ok") is not True:
        return True
    result = data.get("result")
    if not isinstance(result, dict) or result.get("is_bot") is not True:
        return True
    username, bot_id = result.get("username"), result.get("id")
    if not isinstance(username, str) or not username or "@" in username:
        return True
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
        return True
    return False


def get_me(token, *, rate_key: str) -> dict:
    """调一次 getMe，返回 ``{"username": 原样, "bot_user_id": int}``。

    ``token`` 是 ``SecretToken``（或已过形状检查的字符串）。先记限速再发请求。
    失败抛 ``RateLimited`` / ``TokenRejected`` / ``TelegramUnreachable``；异常文本固定、不含令牌。
    """
    if not isinstance(token, SecretToken):
        raise TypeError("token 必须是 SecretToken")
    if not _allow(rate_key):
        raise RateLimited()
    conn, prefix = _connection(_api_base())
    path = prefix + "/bot" + token.reveal() + "/getMe"
    try:
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read(MAX_BODY + 1)
        except TimeoutError:
            raise TelegramUnreachable() from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise TelegramUnreachable() from None
    finally:
        conn.close()
    if len(raw) > MAX_BODY:
        raise TelegramUnreachable()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TelegramUnreachable() from None
    if status == 429:            # Telegram 限流：与本地限速同一类，壳回 429 too_many
        raise RateLimited()
    if status in (401, 404):
        raise TokenRejected()
    if status != 200:            # 其余非 200（5xx、代理错误页等）都按连不上 Telegram
        raise TelegramUnreachable()
    if _rejected_result(data):
        raise TokenRejected()
    result = data["result"]
    return {"username": result["username"], "bot_user_id": int(result["id"])}
