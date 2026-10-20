# -*- coding: utf-8 -*-
"""dsh 底层的 HTTP 适配器（T14；端点表见 INTERFACE §7）。

只用标准库。安全面：

- 只接受回环基址（默认 ``http://127.0.0.1:17990``），构造期就拒绝非回环地址，不发任何请求；
- 直连不经任何代理（``http.client`` 不看代理环境变量）；
- 不跟随重定向（3xx 按“看不懂”处理）；
- 读 5 秒、重启/写入 60 秒超时；响应体上限 1 MiB；
- 令牌每次请求现读 ``~/.config/claude-dsh-console/adapter.token``（dsh 安装步骤按 0600 创建），
  只进 ``Authorization`` 头或新建请求体；不进日志、异常文本与返回值。Windows 上等价的保护是
  只给当前用户读权限的 ACL（``icacls <token> /inheritance:r /grant:r "%USERNAME%:R"``），
  本适配器只读令牌文件，不改它的权限。

令牌文件不存在或为空 → ``AdapterUnavailable("unconfigured")``（壳映射 503
``backend_unconfigured``）；连不上、超时 → ``down``（502 ``backend_down``）；401 →
``unauthorized``（503 ``backend_unconfigured``）；5xx/坏形状 → ``error``（502
``backend_down``）。能力矩阵以对方 ``GET /adapter/v1/meta`` 的返回为准。
"""
import http.client
import json
import os
import re
import urllib.parse

from .base import (AdapterError, AdapterUnavailable, BackendAdapter, BAD_ARCHIVE_ID, BAD_SHELL_META, BOT_ID_MESSAGE,
                   CONFIRM_RULE, ENABLED_TYPE_MESSAGE, NOTIFY_STATUSES, LifecycleError, SecretToken,
                   check_features_body, check_group_body, check_notify, check_voice_body, display_name_error, polling_of,
                   shell_meta_ok, valid_archive_id, valid_bot_id)
from .guard import NotLoopbackError, assert_loopback, loopback_address, scrub

DEFAULT_BASE_URL = "http://127.0.0.1:17990"
DEFAULT_TOKEN_FILE = "~/.config/claude-dsh-console/adapter.token"
API_PREFIX = "/adapter/v1"
READ_TIMEOUT = 5
RESTART_TIMEOUT = 60
# 换供应商、模型、强度：dsh 侧要等网关确认，正常几秒、最坏接近一分钟，5 秒会把已保存的改动误报成"没在运行"
PROVIDER_TIMEOUT = 60
# 投递通知：每次 10 秒；超时、连不上或回 5xx 时用同一个 key 最多再试 2 次
NOTIFY_TIMEOUT = 10
NOTIFY_RETRIES = 2
# 功能开关：读沿用 5 秒；写可能要等对方空闲重启，60 秒，超时不重试，结果未知时文案说清
FEATURES_TIMEOUT = 60
FEATURES_TIMEOUT_MESSAGE = "dsh 侧没有及时回应，开关可能已经改了，请刷新后再看"
# 启停：dsh 停用时最多等 50 秒、整条 55 秒封顶，壳等 60 秒
ENABLE_TIMEOUT = 60
# 归档、恢复、删除：各 60 秒；归档清单是读，用 READ_TIMEOUT
LIFECYCLE_TIMEOUT = 60
MAX_BODY = 1024 * 1024
MAX_MESSAGE = 300

# ── 启停的文案（INTERFACE §5.1）──────────────────────────────────────────────
ENABLE_STOPPED = "已停用，不再收发消息"
ENABLE_STARTED = "已启用"
ENABLE_STARTED_NOT_UP = "已启用，但还没起来"
STILL_RUNNING_MESSAGE = "等了 50 秒它还在跑，没能停下；请稍后再试或手动处理"
ENABLE_NOT_APPLIED = "dsh 没有给出结果，这次没有生效，请稍后再试"
ENABLE_UNKNOWN = "dsh 没有给出结果，不确定这次是否生效；请刷新后看实际状态"
ENABLE_MOVED_TO_RE = re.compile(r"[a-z][a-z0-9_]{1,31}")
ENABLE_MOVED_TO_BAD = "moved_to 只能是 pending、另一种底层的名字或 null"
ENABLE_IMPORT_BAD = "import_from 要是源频道目录名"
# 群聊：读 10 秒、写 60 秒
GROUP_READ_TIMEOUT = 10
GROUP_WRITE_TIMEOUT = 60
# 令牌转交：只供切换任务的 take_token 步骤；超时 10 秒，令牌不进日志与异常文本
TOKEN_HANDOFF_TIMEOUT = 10
TAKE_DENIED = "只能在进行中的切换任务的取令牌步骤里取令牌"
TAKE_ONCE = "这次切换已经取过令牌"
TAKE_SHAPE = "底层回的令牌形状不对"
TOKEN_HANDOFF_PURPOSE = "switch"

# 归档清单每项的键；dsh 没给的按 null
ARCHIVE_KEYS = ("archive_id", "bot_id", "display_name", "person", "archived_at", "status", "shell_meta")
_STEP_KEYS = ("step", "status", "message", "at")


class ServerError(AdapterUnavailable):
    """对方回了 5xx、回体又看不懂。与 ``AdapterUnavailable("error")`` 同形同文案，单独一类只为让投递通知
    认出"回了 5xx"去重试；别的调用方照旧按 AdapterUnavailable 处理。"""


class DshHttpAdapter(BackendAdapter):
    backend = "dsh"

    def __init__(self, base_url=None, token_file=None):
        if base_url is None:
            base_url = os.environ.get("CONSOLE_DSH_ADAPTER_URL") or DEFAULT_BASE_URL
        parts = urllib.parse.urlsplit(base_url)
        # 构造期就拒绝非回环基址：不发任何请求。localhost 要求解析结果全回环，
        # 并把 host 固定成字面回环 IP，后续 _request 不再解析（防 /etc/hosts 被改指向外部）。
        if parts.scheme != "http" or parts.hostname is None:
            raise ValueError("dsh 适配器基址只接受 http 协议的回环地址")
        try:
            host = loopback_address(parts.hostname)
        except NotLoopbackError:
            raise ValueError("dsh 适配器基址只接受 http 协议的回环地址") from None
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("dsh 适配器基址不能带路径、查询串或用户信息")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("dsh 适配器基址端口非法")
        self._host = host
        self._port = port if port is not None else 80
        if token_file is None:
            token_file = os.environ.get("CONSOLE_ADAPTER_TOKEN_FILE") or DEFAULT_TOKEN_FILE
        self._token_file = os.path.expanduser(token_file)

    # ── 令牌 ───────────────────────────────────────────────────────────────
    def _token(self) -> str:
        """每次请求现读；不存在或为空 → unconfigured。返回值只在本模块内流向请求头/请求体。"""
        try:
            with open(self._token_file, "r", encoding="utf-8") as f:
                token = f.read(MAX_BODY)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise AdapterUnavailable("unconfigured")
        except (OSError, UnicodeDecodeError):
            raise AdapterUnavailable("error", "令牌文件读不出来")
        token = token.strip()
        if not token:
            raise AdapterUnavailable("unconfigured")
        # 中间夹着换行等控制字符、或不是 ASCII：http.client 拒发时会把整行 Authorization 头值写进
        # 异常文本，令牌就随调用方的异常日志外泄。这里先拦，文案固定。
        if not (token.isascii() and token.isprintable()):
            raise AdapterUnavailable("error", "令牌文件内容不合规")
        return token

    def token_configured(self) -> bool:
        """令牌文件在、读得出、内容可用。只给"导航里放不放 dsh 页面入口"用；令牌本身不出本方法。"""
        try:
            self._token()
        except AdapterUnavailable:
            return False
        return True

    # ── 传输 ───────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body=None, timeout=None, timeout_message="", multi_step=False,
                 keep_ok=False, allow_list=False):
        """``timeout_message``：等回话超时时 ``down`` 用的文案（不给就是缺省的"底层没在运行"）；连不上照旧。
        ``keep_ok``：2xx 回体里的 ``ok`` 键保留（群写入要按它判形状，其余调用方照旧看不到这个键）。
        ``allow_list``：2xx 回体允许直接是 JSON 数组（INTERFACE §7 的 ``/bots``、``/bots/templates`` 表）。"""
        if timeout is None:
            timeout = READ_TIMEOUT   # 调用时取模块常量，测试可缩短
        token = self._token()
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        del token
        conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            try:
                conn.request(method, API_PREFIX + path, body=payload, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_BODY + 1)
                status = resp.status
            except TimeoutError:      # 3.10 起 socket.timeout 就是它；必须排在 OSError 前面
                raise AdapterUnavailable("down", timeout_message)
            except (OSError, http.client.HTTPException):
                # 连不上 / 对方半路断开：一律 down。不带原异常文本（请求头里有令牌）。
                raise AdapterUnavailable("down")
        finally:
            conn.close()
        if len(raw) > MAX_BODY:
            raise AdapterUnavailable("error", "底层回的内容过大")
        if status == 401:
            raise AdapterUnavailable("unauthorized")
        if 300 <= status < 400:
            raise AdapterUnavailable("error", "底层回了重定向")
        unreadable = ServerError if status >= 500 else AdapterUnavailable
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise unreadable("error", "底层回的不是 JSON")
        if isinstance(data, list) and allow_list:
            if 200 <= status < 300:
                return data
            raise unreadable("error", "底层回的形状不对")
        if not isinstance(data, dict):
            raise unreadable("error", "底层回的形状不对")
        if 200 <= status < 300:
            if not keep_ok:
                data.pop("ok", None)
            return data
        code = data.get("code")
        if not (isinstance(code, str) and code) and status == 409 and data.get("error") == "stale":
            # 人设版本冲突：dsh 契约只约定 409，回体用 {"error":"stale"}。
            raise AdapterError("stale", 409, "人设已被他人修改，请刷新后重试")
        if isinstance(code, str) and code:
            message = data.get("message")
            message = message if isinstance(message, str) else ""
            fields = _field_errors(data.get("fields"))
            if multi_step and ("steps" in data or "state" in data):
                # 多步操作的失败体：做到哪一步、动没动过东西原样交给编排；state 认不出按
                # needs_attention（LifecycleError 自己归一），宁可停下让人看。
                raise LifecycleError(code, status, message[:MAX_MESSAGE], steps=_steps_of(data.get("steps")),
                                     state=data.get("state"), fields=fields)
            raise AdapterError(code, status, message[:MAX_MESSAGE], fields=fields)
        raise unreadable("error", "底层回了看不懂的错误")

    @staticmethod
    def _bot_path(bot_id: str, suffix: str) -> str:
        # 壳已校验过一次；这里再拦一道：不合规的 bot_id 不读令牌、不发请求（"." / ".." 不能进路径）
        if not valid_bot_id(bot_id):
            raise AdapterError("invalid", 422, BOT_ID_MESSAGE)
        return "/bots/" + urllib.parse.quote(bot_id, safe="") + suffix

    # ── 协议 ───────────────────────────────────────────────────────────────
    def meta(self) -> dict:
        """能力矩阵与思考强度枚举以 dsh 的 ``GET /meta`` 为准。"""
        data = self._request("GET", "/meta")
        levels = data.get("effort_levels")
        if (data.get("backend") != "dsh" or not isinstance(data.get("capabilities"), dict)
                or not isinstance(levels, list) or not all(isinstance(x, str) for x in levels)):
            raise AdapterUnavailable("error", "底层回的形状不对")
        data.setdefault("backend_version", None)
        data.setdefault("adapter", "v1")
        return data

    def bots(self) -> list:
        # INTERFACE §7 写的是裸数组；真实 dsh 文档给的是 {"bots": [...]}。两种都收。
        data = self._request("GET", "/bots", allow_list=True)
        bots = data.get("bots") if isinstance(data, dict) else data
        if not isinstance(bots, list):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return bots

    def providers(self) -> dict:
        data = self._request("GET", "/providers")
        if not isinstance(data.get("providers"), list):
            raise AdapterUnavailable("error", "底层回的形状不对")
        data.setdefault("default", None)
        return data

    def set_provider(self, bot_id: str, body: dict) -> dict:
        return self._request("POST", self._bot_path(bot_id, "/provider"), body=dict(body), timeout=PROVIDER_TIMEOUT)

    def restart(self, bot_id: str, force: bool) -> dict:
        return self._request("POST", self._bot_path(bot_id, "/restart"),
                             body={"force": bool(force)}, timeout=RESTART_TIMEOUT)

    # ── 生活设定（只做转发）──────────────────────────────────────────────────
    def get_life(self, bot_id: str) -> dict:
        data = self._request("GET", self._bot_path(bot_id, "/life"))
        if not isinstance(data.get("fields"), list) or not isinstance(data.get("values"), dict):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    def set_life(self, bot_id: str, values: dict) -> dict:
        if not isinstance(values, dict):
            raise AdapterError("invalid", 422, "values 必须是键值表")
        return self._request("PUT", self._bot_path(bot_id, "/life"), body={"values": dict(values)})

    # ── 投递通知 ─────────────────────────────────────────────────────────────
    def notify(self, bot_id: str, source: str, text: str, key: str) -> dict:
        """``{"status": "delivered" | "duplicate", ...}``。入参不合规、bot_id 不合规都不读令牌、不发请求。

        超时、连不上（``down``）或对方回 5xx 时，用同一个 ``key`` 最多再发 ``NOTIFY_RETRIES`` 次；对方按 key
        去重，重发不会重复投递。401、4xx（停用、没这只 bot、入参不对）、令牌没配不重试。"""
        check_notify(source, text, key)
        path = self._bot_path(bot_id, "/notify")
        body = {"source": source, "text": text, "key": key}
        attempt = 0
        while True:
            try:
                data = self._request("POST", path, body=body, timeout=NOTIFY_TIMEOUT)
                break
            except AdapterError as e:
                if e.http_status < 500 or attempt >= NOTIFY_RETRIES:
                    raise
            except AdapterUnavailable as e:
                if not (e.status == "down" or isinstance(e, ServerError)) or attempt >= NOTIFY_RETRIES:
                    raise
            attempt += 1
        if data.get("status") not in NOTIFY_STATUSES:
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    # ── 群聊进退（只做转发）──────────────────────────────────────────────────
    def groups(self, bot_id: str) -> dict:
        data = self._request("GET", self._bot_path(bot_id, "/groups"), timeout=GROUP_READ_TIMEOUT)
        username = data.setdefault("username", None)
        if not isinstance(data.get("groups"), dict) or not (username is None or isinstance(username, str)):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    def set_groups(self, bot_id: str, body: dict) -> dict:
        """入参不合规、bot_id 不合规都不读令牌、不发请求。

        写路径：先走单群路由 ``POST|DELETE /groups/<群号>``（INTERFACE §7 的 dsh 形状，一次写就够）；
        它不被支持（404/405，或回体不是 JSON 的 5xx）时再发整组 ``POST /groups``。幂等操作，回落只为兼容；
        401、连接失败、超时一律不重试。回体必须是 JSON 对象，``in_group``（若有）必须是布尔，
        没有 ``in_group`` 时至少要求 ``ok is true``。"""
        clean = check_group_body(body)
        chat = urllib.parse.quote(clean["chat_id"], safe="")
        single = self._bot_path(bot_id, "/groups/" + chat)
        whole = self._bot_path(bot_id, "/groups")
        try:
            if clean["join"]:
                data = self._request("POST", single, body={"group": clean.get("policy", {})},
                                     timeout=GROUP_WRITE_TIMEOUT, keep_ok=True)
            else:
                data = self._request("DELETE", single, timeout=GROUP_WRITE_TIMEOUT, keep_ok=True)
            return _group_write_shape(data, clean["join"])
        except AdapterError as e:
            if e.http_status not in (404, 405):
                raise
        except AdapterUnavailable as e:
            if e.status != "error":
                raise
        data = self._request("POST", whole, body=clean, timeout=GROUP_WRITE_TIMEOUT, keep_ok=True)
        return _group_write_shape(data, clean["join"])

    # ── 人设、语音（只做转发，校验在对方）────────────────────────────────────
    def get_persona(self, bot_id: str) -> dict:
        data = self._request("GET", self._bot_path(bot_id, "/persona"))
        if not isinstance(data.get("text"), str) or not isinstance(data.get("version"), str):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    def set_persona(self, bot_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise AdapterError("invalid", 422, "请求体必须是键值表")
        return self._request("PUT", self._bot_path(bot_id, "/persona"), body=dict(body))

    def get_voice(self, bot_id: str) -> dict:
        return _voice_shape(self._request("GET", self._bot_path(bot_id, "/voice")))

    def set_voice(self, bot_id: str, body: dict) -> dict:
        # 与 claude 侧同一套字段校验（INTERFACE §0.3/§2.4）：非法模型、未知键、
        # 非法语言在壳内就 422，不再把坏值转给 dsh。
        clean = check_voice_body(body)
        return _voice_shape(self._request("PUT", self._bot_path(bot_id, "/voice"), body=clean))

    # ── 功能开关：只做映射，壳已查过能力键 ─────────────────────────────────
    def get_features(self, bot_id: str) -> dict:
        return _features_shape(self._request("GET", self._bot_path(bot_id, "/features")))

    def set_features(self, bot_id: str, changes: dict) -> dict:
        """先取对方 GET 的 ``keys``：只发对方认识的键，不认识的按与 claude 侧同一条规则回 422、不发给对方。
        PUT 只带 ``features`` 一个键；60 秒没回按结果未知报 down，不重试。"""
        path = self._bot_path(bot_id, "/features")
        keys = [k["key"] for k in _features_shape(self._request("GET", path))["keys"]]
        clean = check_features_body({"features": changes}, keys)
        data = self._request("PUT", path, body={"features": clean}, timeout=FEATURES_TIMEOUT,
                             timeout_message=FEATURES_TIMEOUT_MESSAGE)
        if not isinstance(data.get("message"), str):   # 成功体至少有生效说明
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    # ── 刚发现的群：只做映射，壳已查过能力键 ───────────────────────────────
    def discovered_groups(self, bot_id: str, include_joined: bool = False) -> dict:
        path = self._bot_path(bot_id, "/groups/discovered")
        if include_joined:
            path += "?include_joined=true"
        data = self._request("GET", path)
        if not isinstance(data.get("groups"), list):
            raise AdapterUnavailable("error", "底层回的形状不对")
        warnings = data.get("warnings")
        data["warnings"] = [w for w in warnings if isinstance(w, str)] if isinstance(warnings, list) else []
        return data

    # ── 新建与模板清单 ───────────────────────────────────────────────────────
    def templates(self) -> list:
        """``GET /bots/templates``；取不到（不可用、不支持、形状不对）一律 ``[]``。"""
        try:
            data = self._request("GET", "/bots/templates", allow_list=True)
        except (AdapterError, AdapterUnavailable):
            return []
        items = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("label"), str):
                out.append({"id": item["id"], "label": item["label"]})
        return out

    def create(self, spec: dict, token) -> dict:
        """``POST /bots``。令牌只进请求体：发出前断言目标是回环；请求体与响应体不进日志与异常文本。"""
        if not isinstance(spec, dict):
            raise AdapterError("invalid", 422, "请求体必须是 JSON 对象")
        if not isinstance(token, SecretToken):
            raise AdapterError("invalid", 422, "令牌格式不对")
        try:
            assert_loopback(self._host)
        except NotLoopbackError:
            raise AdapterUnavailable("error") from None
        bot_id = spec.get("bot_id")
        if not valid_bot_id(bot_id):
            raise AdapterError("invalid", 422, BOT_ID_MESSAGE)
        body = dict(spec)
        body.pop("op_id", None)   # 任务编号不发给 dsh
        body.setdefault("person", bot_id)
        body.setdefault("enabled", True)
        body["telegram_token"] = token.reveal()
        data = self._request("POST", "/bots", body=body, timeout=LIFECYCLE_TIMEOUT, multi_step=True)
        person = data.get("person") if isinstance(data.get("person"), str) else body["person"]
        username = data.get("username") if isinstance(data.get("username"), str) else spec.get("telegram_username")
        enabled = data.get("enabled") if isinstance(data.get("enabled"), bool) else body["enabled"]
        running = data.get("running")
        if not isinstance(running, bool):
            running = enabled
        return {"bot_id": data.get("bot_id") if isinstance(data.get("bot_id"), str) else bot_id,
                "person": person, "username": username, "enabled": enabled, "running": running,
                "message": _message_of(data), "steps": _steps_of(data.get("steps"))}

    def take_token(self, bot_id: str, op):
        """D13：只供切换任务的 ``take_token`` 步骤，向 dsh 的 ``POST /bots/<id>/token_handoff`` 取这只 bot 的令牌。
        门不满足（不是进行中的切换任务、不在取令牌那一步、这次已经取过）→ ``step_failed``；取到后在本任务的记录里
        只记 ``token_taken: true``。令牌只经 ``SecretToken`` 返回，请求体与响应体都不进日志与异常文本。"""
        if not valid_bot_id(bot_id):
            raise AdapterError("invalid", 422, BOT_ID_MESSAGE)
        rec = getattr(op, "rec", op)
        if not isinstance(rec, dict) or rec.get("kind") != "switch" or rec.get("status") != "running" \
                or _running_step(rec) != "take_token" or rec.get("bot_id") != bot_id \
                or rec.get("backend") != self.backend:
            raise AdapterError("step_failed", 500, TAKE_DENIED)
        inner = rec.get("_internal")
        if not isinstance(inner, dict):
            inner = rec["_internal"] = {}
        data = inner.get("data")
        if not isinstance(data, dict):
            data = inner["data"] = {}
        if data.get("token_taken") is True:
            raise AdapterError("step_failed", 500, TAKE_ONCE)
        target = rec.get("target")
        to = target.get("backend") if isinstance(target, dict) else target
        if not isinstance(to, str) or not to or to == self.backend:
            raise AdapterError("invalid", 422, "目标底层不对")
        try:
            assert_loopback(self._host)
        except NotLoopbackError:
            raise AdapterUnavailable("error") from None
        body = {"op_id": rec.get("op_id"), "to": to, "purpose": TOKEN_HANDOFF_PURPOSE}
        out = self._request("POST", self._bot_path(bot_id, "/token_handoff"), body=body,
                            timeout=TOKEN_HANDOFF_TIMEOUT)
        value = out.get("telegram_token")
        try:
            token = SecretToken(value) if isinstance(value, str) else None
        except ValueError:
            token = None
        if token is None:
            raise AdapterUnavailable("error", TAKE_SHAPE)
        data["token_taken"] = True
        if hasattr(op, "save"):
            op.save()
        return token

    def get_profile(self, bot_id: str) -> dict:
        data = self._request("GET", self._bot_path(bot_id, "/profile"))
        if not isinstance(data.get("display_name"), str):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return data

    def set_profile(self, bot_id: str, display_name: str) -> dict:
        """入参先过同一条显示名规则：不合规不读令牌、不发请求。"""
        err = display_name_error(display_name)
        if err:
            raise AdapterError("invalid", 422, err, fields={"display_name": err})
        return self._request("PUT", self._bot_path(bot_id, "/profile"), body={"display_name": display_name})

    # ── 启停 ─────────────────────────────────────────────────────────────────
    def set_enabled(self, bot_id: str, enabled: bool, *, op_id=None, moved_to=..., import_from=None) -> dict:
        """``{"enabled", "running", "message"}``，与 claude 侧同形；dsh 回的 ``running`` 与 ``polling`` 是同一判定。
        ``running`` 为 null 或缺键按 true 算（宁可误报）。

        请求体带 ``enabled``，并按切换约定多带：``op_id``（只用于记录）、停用时的 ``moved_to``
        （``"pending"``、底层名或 ``null``）、切进时的 ``import_from``。入参不合规都不读令牌、不发请求。
        以下两类例外：
        - 停用的结果必须是不在收消息：回 200 而 ``running`` 不是 false，按 ``still_running``（503）；
        - 超时、连不上、半路断开、5xx 回体看不懂、``503 unavailable``：结果未知，重读名单定结果。"""
        if not isinstance(enabled, bool):
            raise AdapterError("invalid", 422, ENABLED_TYPE_MESSAGE)
        if moved_to is not ... and not (moved_to is None or (isinstance(moved_to, str)
                                                            and ENABLE_MOVED_TO_RE.fullmatch(moved_to))):
            raise AdapterError("invalid", 422, ENABLE_MOVED_TO_BAD)
        if import_from is not None and not (isinstance(import_from, str) and 0 < len(import_from) <= 64):
            raise AdapterError("invalid", 422, ENABLE_IMPORT_BAD)
        body = {"enabled": enabled}
        if isinstance(op_id, str) and op_id:
            body["op_id"] = op_id
        if moved_to is not ...:
            body["moved_to"] = moved_to
        if import_from is not None:
            body["import_from"] = import_from
        path = self._bot_path(bot_id, "/enabled")
        try:
            data = self._request("POST", path, body=body, timeout=ENABLE_TIMEOUT)
        except AdapterError as e:
            if (e.code, e.http_status) != ("unavailable", 503):
                raise
            # dsh 的 503 unavailable：停用时是"等了 50 秒还在跑"，一般含义是"进程连不上、没执行"；
            # 重读分得出就按实际结果，分不出就照 dsh 的原话交回
            not_applied = unknown = e
        except AdapterUnavailable as e:
            if e.status != "down" and not isinstance(e, ServerError):
                raise   # 令牌没配或不对、重定向、2xx 回体看不懂：照旧
            not_applied = AdapterUnavailable("down", ENABLE_NOT_APPLIED)
            unknown = AdapterUnavailable("down", ENABLE_UNKNOWN)
        else:
            running = data.get("running")
            if data.get("enabled") is not enabled or not (running is None or isinstance(running, bool)):
                raise AdapterUnavailable("error", "底层回的形状不对")
            return _enabled_result(enabled, running is not False, data.get("message"))
        return self._settle_enabled(bot_id, enabled, not_applied, unknown)

    def _settle_enabled(self, bot_id: str, enabled: bool, not_applied: Exception, unknown: Exception) -> dict:
        """结果未知时重读一次名单定结果：这只的 ``enabled`` 已是目标值 → 照它的 ``polling`` 回成功
        （停用而仍在收消息 → still_running）；还是原值 → 抛 ``not_applied``；名单读不出、里面没有这只、
        ``enabled`` 不是布尔 → 抛 ``unknown``。"""
        try:
            me = next((b for b in self.bots() if isinstance(b, dict) and b.get("bot_id") == bot_id), None)
        except (AdapterError, AdapterUnavailable):
            me = None
        state = me.get("enabled") if me else None
        if not isinstance(state, bool):
            raise unknown
        if state is not enabled:
            raise not_applied
        return _enabled_result(enabled, polling_of(me))

    # ── 归档、归档清单、恢复、删除 ───────────────────────────────────────────
    def archive(self, bot_id: str, shell_meta: dict, *, op_id=None) -> dict:
        """``{"archive_id", "message", "steps"}``。``shell_meta`` 原样放进请求体，为 None 时请求体是 ``{}``。"""
        path = self._bot_path(bot_id, "/archive")
        body = {} if shell_meta is None else {"shell_meta": _shell_meta(shell_meta)}
        data = self._request("POST", path, body=body, timeout=LIFECYCLE_TIMEOUT, multi_step=True)
        archive_id = data.get("archive_id")
        if not (isinstance(archive_id, str) and archive_id):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return {"archive_id": archive_id, "message": _message_of(data), "steps": _steps_of(data.get("steps"))}

    def list_archives(self) -> list:
        """``[{ARCHIVE_KEYS…}]``，dsh 没给的键为 null，``shell_meta`` 原样交回；dsh 多给的键照留。"""
        archives = self._request("GET", "/archives").get("archives")
        if not isinstance(archives, list) or not all(isinstance(a, dict) for a in archives):
            raise AdapterUnavailable("error", "底层回的形状不对")
        out = []
        for a in archives:
            item = dict(a)
            for key in ARCHIVE_KEYS:
                item.setdefault(key, None)
            if not (item["shell_meta"] is None or isinstance(item["shell_meta"], dict)):
                raise AdapterUnavailable("error", "底层回的形状不对")
            out.append(item)
        return out

    def restore(self, archive_id: str, *, op_id=None) -> dict:
        """``{"bot_id", "enabled", "running", "message", "steps"}``。恢复后恒为停用态：没给 ``enabled`` 按 false；
        ``running`` 没给或为 null 按 true（宁可误报）。"""
        if not valid_archive_id(archive_id):
            raise AdapterError("invalid", 422, BAD_ARCHIVE_ID)
        path = "/archives/" + urllib.parse.quote(archive_id, safe="") + "/restore"
        data = self._request("POST", path, body={}, timeout=LIFECYCLE_TIMEOUT, multi_step=True)
        bot_id, enabled, running = data.get("bot_id"), data.get("enabled", False), data.get("running")
        if not (isinstance(bot_id, str) and bot_id and isinstance(enabled, bool)
                and (running is None or isinstance(running, bool))):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return {"bot_id": bot_id, "enabled": enabled, "running": running is not False, "message": _message_of(data),
                "steps": _steps_of(data.get("steps"))}

    def delete(self, bot_id: str, confirm: str, *, op_id=None) -> dict:
        """``{"message", "trash", "trash_path", "steps"}``；``trash``、``trash_path`` 是 claude 侧另带的，dsh 没给为 null。
        ``confirm`` 与 ``bot_id`` 不逐字相同 → 422 invalid（同 claude 侧）。"""
        if not isinstance(confirm, str) or confirm != bot_id:
            raise AdapterError("invalid", 422, CONFIRM_RULE, fields={"confirm": CONFIRM_RULE})
        data = self._request("POST", self._bot_path(bot_id, "/delete"), body={"confirm": confirm},
                             timeout=LIFECYCLE_TIMEOUT, multi_step=True)
        trash, trash_path = data.get("trash"), data.get("trash_path")
        # trash 在 claude 侧是路径字符串，dsh 契约只要求说明“是否进了废纸篓”，
        # 因此布尔也合法；两种都原样透传给管理台。
        if not all(v is None or isinstance(v, (str, bool)) for v in (trash, trash_path)):
            raise AdapterUnavailable("error", "底层回的形状不对")
        return {"message": _message_of(data), "trash": trash, "trash_path": trash_path,
                "steps": _steps_of(data.get("steps"))}


def _features_shape(data: dict) -> dict:
    """功能开关 GET 的回包：``keys`` 是 ``{key, label}`` 字符串对的列表，``features`` 给其中每个键一个布尔，
    ``requirements`` 是对象；``warnings`` 缺了按空列表。形状不对 → error（壳回 502）。"""
    keys, features = data.get("keys"), data.get("features")
    if not (isinstance(keys, list) and isinstance(features, dict) and isinstance(data.get("requirements"), dict)):
        raise AdapterUnavailable("error", "底层回的形状不对")
    for item in keys:
        if not (isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("label"), str)
                and isinstance(features.get(item["key"]), bool)):
            raise AdapterUnavailable("error", "底层回的形状不对")
    warnings = data.setdefault("warnings", [])
    if not (isinstance(warnings, list) and all(isinstance(w, str) for w in warnings)):
        raise AdapterUnavailable("error", "底层回的形状不对")
    return data


def _group_write_shape(data, join: bool) -> dict:
    """群写入的成功回体：JSON 对象；``in_group`` 若有必须是布尔；没有它时要求 ``ok is true``。"""
    if not isinstance(data, dict):
        raise AdapterUnavailable("error", "底层回的形状不对")
    if "in_group" in data:
        if not isinstance(data.get("in_group"), bool):
            raise AdapterUnavailable("error", "底层回的形状不对")
        if data["in_group"] is not join:   # 回体必须和这次请求一致，否则不当作成功
            raise AdapterUnavailable("error", "底层回的形状不对")
    elif data.get("ok") is True:
        data["in_group"] = join
    else:
        raise AdapterUnavailable("error", "底层回的形状不对")
    data.pop("ok", None)
    return data


def _running_step(rec: dict):
    """任务记录里正在执行的那一步（恰好一个才算）；取令牌的门用它认“就在 take_token 这一步”。"""
    steps = rec.get("steps") if isinstance(rec.get("steps"), list) else ()
    keys = [s.get("step") for s in steps if isinstance(s, dict) and s.get("status") == "running"]
    return keys[0] if len(keys) == 1 else None


def _enabled_result(enabled: bool, running: bool, message=None) -> dict:
    """启停的成功体。停用而仍在收消息 → still_running；dsh 给了 message 就打码后用，没给按接口表补。"""
    if not enabled and running:
        raise AdapterError("still_running", 503, STILL_RUNNING_MESSAGE)
    if not (isinstance(message, str) and message):
        if not enabled:
            message = ENABLE_STOPPED
        else:
            message = ENABLE_STARTED if running else ENABLE_STARTED_NOT_UP
    return {"enabled": enabled, "running": running, "message": scrub(message)}


def _voice_shape(data: dict) -> dict:
    """语音的回包：两个键缺了按 null；不是字符串也不是 null → 形状不对。"""
    for key in ("voice_id", "tts_model"):
        value = data.setdefault(key, None)
        if not (value is None or isinstance(value, str)):
            raise AdapterUnavailable("error", "底层回的形状不对")
    return data


def _field_errors(raw) -> dict:
    """对方 422 响应里的 ``fields``：只收「键 → 文字」，每条截到 MAX_MESSAGE；形状不对就当没有。"""
    if not isinstance(raw, dict):
        return {}
    return {str(k)[:MAX_MESSAGE]: v[:MAX_MESSAGE] for k, v in raw.items() if isinstance(v, str)}


def _shell_meta(value) -> dict:
    """随归档保存的说明：编码后不超过 64 KiB 的 JSON 对象，否则 422；NaN 这类非标准 JSON 也拒。"""
    if not shell_meta_ok(value):
        raise AdapterError("invalid", 422, BAD_SHELL_META)
    return value


def _message_of(data: dict) -> str:
    message = data.get("message")
    return scrub(message) if isinstance(message, str) else ""


def _steps_of(raw) -> list:
    """回体里的 ``steps``：只收键值表，每项只留四个键里是文字的，先打码再截到 MAX_MESSAGE；
    形状不对的项跳过，整个不是列表就当没有。"""
    if not isinstance(raw, list):
        return []
    return [{k: scrub(item[k])[:MAX_MESSAGE] for k in _STEP_KEYS if isinstance(item.get(k), str)}
            for item in raw if isinstance(item, dict)]
