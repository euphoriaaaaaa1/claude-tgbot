# -*- coding: utf-8 -*-
"""适配器协议：两种底层（claude / dsh）共同的调用面、能力矩阵、错误类型与输入校验。

本模块只用标准库。壳（8767 管理台）只通过 ``adapters`` 包访问底层；具体适配器由
``registry.adapter()`` 延迟导入，本模块不导入任何具体适配器。

错误契约（错误码表见 INTERFACE §9）：

- ``AdapterError``：单次操作被拒，带 ``code`` / ``http_status`` / ``message`` / ``fields``；
- ``LifecycleError``：多步操作（归档、恢复、删除…）中途失败，另带 ``steps`` 与 ``state``；
- ``AdapterUnavailable``：整侧不可用，``status`` ∈ down / unconfigured / unauthorized /
  not_installed / error。它到 HTTP 的映射在 ``registry.UNAVAILABLE_HTTP``：
  未安装 → 404 ``unknown_backend``，未配置/未授权 → 503 ``backend_unconfigured``，
  不可用 → 502 ``backend_down``。能力不支持 → 501 ``unsupported``（``require_capability``）。

能力矩阵的键名固定、值为布尔；没出现的键按 false 处理。适配器必须如实报缺：做不到就报
false，不要报 true 再在调用时失败。
"""
import hashlib
import json
import re

# ── bot_id ───────────────────────────────────────────────────────────────────
# 壳与适配器各校验一次（纵深防御）。只许字母数字点下划线连字符、1 到 64 个字符，
# 且不能纯由点组成——"." / ".." 进了路径会被对方的路径规范化带到别的接口上。
BOT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
BOT_ID_MESSAGE = "bot_id 只能用字母、数字、点、下划线、连字符，1 到 64 个字符，且不能只有点"


def valid_bot_id(bot_id) -> bool:
    return isinstance(bot_id, str) and BOT_ID_RE.fullmatch(bot_id) is not None and bot_id.strip(".") != ""


# ── 能力矩阵 ─────────────────────────────────────────────────────────────────
# 思考强度枚举的唯一来源（meta.effort_levels）；T09 接入供应商配置后仍以此为准。
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# 已知能力键。新能力先加到这里，再在两边适配器的 meta() 里如实填 true/false。
CAPABILITY_KEYS = (
    "bot.restart.graceful",
    "bot.restart.force",
    "bot.set_provider",
    "bot.reset_provider",
    "bot.set_effort",
    "provider.subscription",
    "provider.per_session",
    "provider.switch_new_segment",
    "bot.life_settings",
    "bot.notify",
    "bot.enable",
    "bot.switch_in",
    "bot.group_chat",
    "bot.persona",
    "bot.voice",
    "bot.skills",
    "bot.create",
    "bot.archive",
    "bot.delete",
    "bot.profile",
    "bot.token_handoff",
    "bot.features",
    "bot.groups_discovered",
)

# 两边的占位能力表（T13/T14 按真实实现更新）。claude 侧不做 graceful 之外的导入，
# 也不支持按会话分供应商；dsh 侧先不支持 graceful 重启与 reset（网关补上后改 true）。
CLAUDE_CAPABILITIES = {
    "bot.restart.graceful": True,
    "bot.restart.force": True,
    "bot.set_provider": True,
    "bot.reset_provider": True,
    "bot.set_effort": True,
    "provider.subscription": True,
    "provider.per_session": False,
    "provider.switch_new_segment": False,
    "bot.life_settings": True,
    "bot.notify": True,
    "bot.enable": True,
    "bot.switch_in": False,
    "bot.group_chat": True,
    "bot.persona": True,
    "bot.voice": True,
    "bot.skills": True,
    "bot.create": True,
    "bot.archive": True,
    "bot.delete": True,
    "bot.profile": True,
    "bot.token_handoff": True,
    "bot.features": True,
    "bot.groups_discovered": True,
}

DSH_CAPABILITIES = {
    "bot.restart.graceful": False,
    "bot.restart.force": True,
    "bot.set_provider": True,
    "bot.reset_provider": False,
    "bot.set_effort": True,
    "provider.subscription": False,
    "provider.per_session": False,
    "provider.switch_new_segment": True,
    "bot.life_settings": True,
    "bot.notify": True,
    "bot.enable": True,
    "bot.switch_in": True,
    "bot.group_chat": True,
    "bot.persona": True,
    "bot.voice": True,
    "bot.skills": True,
    "bot.create": True,
    "bot.archive": True,
    "bot.delete": True,
    "bot.profile": True,
    "bot.token_handoff": True,
    "bot.features": True,
    "bot.groups_discovered": True,
}

# 能力不满足时的固定文案（与壳的 501 文案一致）
UNSUPPORTED_MESSAGE = "这个底层不提供这项操作"


def capabilities_of(meta) -> dict:
    """从 ``meta()`` 的返回值里取能力表；形状不对时按空表（全部不支持）处理。"""
    caps = meta.get("capabilities") if isinstance(meta, dict) else None
    return caps if isinstance(caps, dict) else {}


def capability_on(meta, key) -> bool:
    """该底层是否提供 ``key``：只有值严格为 ``True`` 才算支持；缺键、false、非布尔一律否。"""
    return capabilities_of(meta).get(key) is True


def normalize_capabilities(raw) -> dict:
    """规范成"已知键 + 严格 True"：未知键、非布尔、false 都不进结果。供测试与页面展示用。"""
    if not isinstance(raw, dict):
        return {}
    return {key: True for key in CAPABILITY_KEYS if raw.get(key) is True}


def require_capability(meta, key, message: str = UNSUPPORTED_MESSAGE) -> None:
    """能力��支持时抛 ``AdapterError("unsupported", 501)``；支持时什么都不做。

    壳在调适配器之前先用它挡一次；适配器内部也可用它做纵深防御。"""
    if not capability_on(meta, key):
        raise AdapterError("unsupported", 501, message)


# ── 投递通知 ─────────────────────────────────────────────────────────────────
# 三种来源；正文按 UTF-8 不超过 16 KiB；去重键 1 到 200 个字符。
NOTIFY_SOURCES = ("user_moment", "moment_reply", "moment_image")
NOTIFY_MAX_TEXT_BYTES = 16 * 1024
NOTIFY_MAX_KEY = 200
NOTIFY_STATUSES = ("delivered", "duplicate")


# ── 群聊进退 ─────────────────────────────────────────────────────────────────
GROUP_CHAT_ID_RE = re.compile(r"-[0-9]{1,20}")
# 壳的新增接口与发现接口的群号写法：只收整数写法（字符串或 JSON 整数），规范化成字符串；
# -0、前导零、小数、布尔、数组一律不收。
GROUP_CHAT_ID_INT_RE = re.compile(r"-[1-9][0-9]{0,15}")
GROUP_CHAT_ID_MESSAGE = "群号是 Telegram 群的数字编号，以 - 开头，例如 -1001234567890"
GROUP_ALLOW_FROM_MESSAGE = "至少一个人；每项是 Telegram 用户的数字编号"
GROUP_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
GROUP_LIST_KEYS = ("allowFrom", "selfAliases", "otherBotUsernames", "otherBotAliases")
GROUP_POLICY_KEYS = ("requireMention",) + GROUP_LIST_KEYS + ("intro",)
GROUP_LIST_MAX = 50
GROUP_ITEM_MAX = 64
GROUP_INTRO_MAX = 4000


# ── 人设 ─────────────────────────────────────────────────────────────────────
PERSONA_MAX_BYTES = 256 * 1024
PERSONA_VERSION_RE = re.compile(r"[0-9a-fA-F]{16}")
PERSONA_KEYS = ("text", "version")

# ── 语音 ─────────────────────────────────────────────────────────────────────
VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 模型 → 写法族：白名单就是它的键，加新模型时必须同时写族；同族模型的语气标签写法相同。
TTS_MODEL_FAMILY = {"drama-3": "drama-3", "s2.1-pro": "s2", "s2.1-pro-free": "s2", "s2-pro": "s2", "s1": "s1"}
TTS_MODELS = tuple(TTS_MODEL_FAMILY)
LANGUAGE_KEYS = ("text_language", "voice_language")
LANGUAGE_MAX = 20
LANGUAGE_FORBIDDEN = "[]<>{}`|"
VOICE_KEYS = ("voice_id", "tts_model") + LANGUAGE_KEYS

# ── 功能开关 ─────────────────────────────────────────────────────────────────
# 请求体只许 features 一个键，它是至少含一个键的对象，值是布尔。文案固定。
FEATURES_BODY_KEY = "features"
FEATURES_NOT_OBJECT = "请求体必须是 JSON 对象"
FEATURES_EXTRA_KEYS = "只许 features 一个键"
FEATURES_EMPTY = "features 必须是至少含一个键的对象"
FEATURES_INVALID = "入参不合规"
FEATURE_UNKNOWN = "没有这个功能"
FEATURE_NOT_BOOL = "只能是 true 或 false"

# ── 显示名 ───────────────────────────────────────────────────────────────────
DISPLAY_NAME_MAX = 32
DISPLAY_NAME_TYPE_MESSAGE = "显示名必须是文字"
DISPLAY_NAME_RULE_MESSAGE = "显示名去掉首尾空白后要 1 到 32 个字，不能有换行或控制字符"
PROFILE_SAVED = "已保存"
PROFILE_UNCHANGED = "没有改动"
_DISPLAY_NAME_BAD_RE = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")

# ── 新建 ─────────────────────────────────────────────────────────────────────
# 编号规则与保留名单是壳、两种适配器与页面共用的一份。保留名单照 INTERFACE §2.4。
NEW_BOT_ID_RE = re.compile(r"[a-z][a-z0-9_]{1,31}")
NEW_BOT_ID_MESSAGE = "编号要小写字母开头，只含小写字母、数字、下划线，2 到 32 个字符"
RESERVED_BOT_IDS = frozenset(("jiwen", "director", "voicecall", "media", "group_transcripts", "telegram",
                              "yasuna", "bot2", "bot3", "bot4", "lili"))
RESERVED_BOT_MESSAGE = "这个编号是保留的，换一个"
CHAT_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
CHAT_ID_MESSAGE = "主人的私聊编号是一串数字"
PERSONA_BOTH_MESSAGE = "人设模板和人设全文只能给一个"
PERSONA_TEMPLATE_MISSING = "没有这个模板"
PERSONA_TEXT_MESSAGE = "人设全文不能为空，不超过 256 KiB"

# ── 启停 ─────────────────────────────────────────────────────────────────────
ENABLED_TYPE_MESSAGE = "enabled 必须是 true 或 false"

# ── 归档、恢复、删除 ─────────────────────────────────────────────────────────
ARCHIVE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
BAD_ARCHIVE_ID = "归档编号不合规"
SHELL_META_MAX = 64 * 1024
BAD_SHELL_META = "随归档保存的说明不是 64 KiB 以内的 JSON 对象"
CONFIRM_RULE = "请原样输入这只 bot 的编号"


def new_bot_id_error(value):
    """新 bot 编号：不合规返回给人看的一句话，合规返回 None。"""
    if not isinstance(value, str) or NEW_BOT_ID_RE.fullmatch(value) is None:
        return NEW_BOT_ID_MESSAGE
    if value in RESERVED_BOT_IDS:
        return RESERVED_BOT_MESSAGE
    return None


def chat_id_error(value):
    """主人的私聊编号：字符串形式的正整数；不合规返回固定文案，合规返回 None。"""
    if not isinstance(value, str) or CHAT_ID_RE.fullmatch(value) is None:
        return CHAT_ID_MESSAGE
    return None


def display_name_error(value):
    """显示名规则的唯一实现：不合规时返回给人看的一句话，合规返回 None。

    控制字符在去首尾空白之前就查：值里任何位置有换行、制表符之类都不收。
    技能模板渲染时注释标记会被当成结构，显示名这一层先挡住。"""
    if not isinstance(value, str):
        return DISPLAY_NAME_TYPE_MESSAGE
    if _DISPLAY_NAME_BAD_RE.search(value) or not 1 <= len(value.strip()) <= DISPLAY_NAME_MAX:
        return DISPLAY_NAME_RULE_MESSAGE
    if "<!--" in value or "-->" in value:
        return DISPLAY_NAME_RULE_MESSAGE
    return None


def valid_archive_id(value) -> bool:
    return isinstance(value, str) and ARCHIVE_ID_RE.fullmatch(value) is not None and value.strip(".") != ""


def shell_meta_ok(value) -> bool:
    """``shell_meta`` 合规：键值表、能编成标准 JSON（不许 NaN 这类值）、UTF-8 后不超过 64 KiB。"""
    if not isinstance(value, dict):
        return False
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return False
    return len(raw) <= SHELL_META_MAX


# ── 错误类型 ─────────────────────────────────────────────────────────────────
# AdapterUnavailable.status 的取值与给人看的话
UNAVAILABLE_TEXT = {
    "down": "底层没在运行",
    "unconfigured": "还没配置",
    "unauthorized": "令牌不对",
    "not_installed": "未安装",
    "error": "出错了",
}


class AdapterError(Exception):
    """底层对单次操作的拒绝；code / http_status 照错误码表，message 给人看。
    ``fields``：校验失败（invalid）时逐键的原因 ``{键: "这一项哪里不对"}``，其余情形为空。"""

    def __init__(self, code: str, http_status: int, message: str, fields=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.fields = dict(fields) if isinstance(fields, dict) else {}


class LifecycleError(AdapterError):
    """多步操作（归档、恢复、删除…）中途失败。比 AdapterError 多带两样：
    ``steps``（这一侧做到哪一步）与 ``state``：

    - ``unchanged``：什么都没改；
    - ``rolled_back``：改过的已全部撤回；
    - ``needs_attention``：停在中间，或已过提交点但收尾没做完；不能当成没做成去撤别的步骤。
    """

    STATES = ("unchanged", "rolled_back", "needs_attention")

    def __init__(self, code: str, http_status: int, message: str, *, steps=None, state: str = "needs_attention",
                 fields=None):
        super().__init__(code, http_status, message, fields)
        self.steps = [dict(s) for s in steps or []]
        self.state = state if state in self.STATES else "needs_attention"


class AdapterUnavailable(Exception):
    """底层整体不可用；status ∈ UNAVAILABLE_TEXT 的键。"""

    def __init__(self, status: str, message: str = ""):
        if status not in UNAVAILABLE_TEXT:
            status = "error"
        super().__init__(message or UNAVAILABLE_TEXT[status])
        self.status = status
        self.message = message or UNAVAILABLE_TEXT[status]


# ── 令牌 ─────────────────────────────────────────────────────────────────────
HIDDEN = "<令牌已隐藏>"
# 令牌形状：整串匹配。比打码用的 SCRUB_RE（在 guard 里）多一个长度上限，所以凡是合形状的
# 令牌都会被打码抹掉。
TOKEN_RE = re.compile(r"[0-9]{5,12}:[A-Za-z0-9_-]{30,64}")
TOKEN_FORMAT_MESSAGE = "令牌格式不对：应是“数字:一串字母数字”，请从 BotFather 整段复制"


class SecretToken:
    """Telegram 令牌在内存里的唯一形态。

    ``repr``、``str``、各种格式化一律给出 ``<令牌已隐藏>``；序列化成 JSON 会因类型不认识而抛
    TypeError，pickle、copy 走的 ``__reduce_ex__`` 直接抛 TypeError，免得令牌多出一份副本。
    不继承 str，否则 json 会把它当字符串原样写出去。要用原值只能显式调 ``reveal()``。
    Python 保证不了擦除内存，这里保证的是不进任何输出。
    """
    __slots__ = ("_value",)

    def __init__(self, value):
        # 只收合形状的令牌：报错文字固定，不带传进来的值
        if not isinstance(value, str) or TOKEN_RE.fullmatch(value) is None:
            raise ValueError("令牌格式不对")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self):
        return HIDDEN

    def __str__(self):
        return HIDDEN

    def __format__(self, spec):
        return HIDDEN

    def __reduce_ex__(self, protocol):
        raise TypeError("令牌不能序列化")

    def __reduce__(self):
        raise TypeError("令牌不能序列化")


# ── 适配器调用面 ─────────────────────────────────────────────────────────────
class BackendAdapter:
    """两种底层共同的调用面。子类必须全部实现；默认实现只是占位，防止漏实现时静默成功。"""

    backend = ""

    def meta(self) -> dict:
        """``{"backend","backend_version","adapter","capabilities","effort_levels"}``。"""
        raise NotImplementedError

    def bots(self) -> list:
        """``bots`` 列表。"""
        raise NotImplementedError

    def providers(self) -> dict:
        """``{"providers": [...], "default": {...} | None}``。"""
        raise NotImplementedError

    def set_provider(self, bot_id: str, body: dict) -> dict:
        """``{"applied", "queued", "new_segment", "message"}``。"""
        raise NotImplementedError

    def restart(self, bot_id: str, force: bool) -> dict:
        """``{"status": "ok" | "queued" | "failed", "message"}``。"""
        raise NotImplementedError

    def get_life(self, bot_id: str) -> dict:
        """``{"fields": [...], "values": {...}}``（可多带 ``display_name``）。"""
        raise NotImplementedError

    def set_life(self, bot_id: str, values: dict) -> dict:
        """``{"applied", "queued", "message"}``；校验失败抛 ``AdapterError("invalid", 422, …, fields=…)``。"""
        raise NotImplementedError

    def notify(self, bot_id: str, source: str, text: str, key: str) -> dict:
        """``{"status": "delivered" | "duplicate"}``；同一个 key 只投一次。入参先过 ``check_notify``。"""
        raise NotImplementedError

    def groups(self, bot_id: str) -> dict:
        """``{"username": str | None, "groups": {群号: 群设置}}``；群设置六个键，没写的为 None。"""
        raise NotImplementedError

    def set_groups(self, bot_id: str, body: dict) -> dict:
        """``{"in_group": bool, "policy": 群设置 | None}``。入参先过 ``check_group_body``。"""
        raise NotImplementedError

    def get_persona(self, bot_id: str) -> dict:
        """``{"text": 全文, "version": 版本号}``。"""
        raise NotImplementedError

    def set_persona(self, bot_id: str, body: dict) -> dict:
        """``{"version": 新版本号, "message": 生效说明}``；版本号对不上抛 ``AdapterError("stale", 409)``。"""
        raise NotImplementedError

    def get_voice(self, bot_id: str) -> dict:
        """``{"voice_id","tts_model","text_language","voice_language"}``，各为 str 或 None。"""
        raise NotImplementedError

    def set_voice(self, bot_id: str, body: dict) -> dict:
        """写入后的四个键；改了语言而且这次真的要生效时另带 ``message``。"""
        raise NotImplementedError

    def get_features(self, bot_id: str) -> dict:
        """``{"keys","features","requirements","warnings"}``；claude 侧另带 ``skills_synced``。"""
        raise NotImplementedError

    def set_features(self, bot_id: str, changes: dict) -> dict:
        """``changes`` 是 ``{键: 布尔}``，壳已按 ``check_features_body(body, None)`` 校验过形状。返回
        ``{"features","changed","applied","queued","message","skills_synced","warnings"}``。"""
        raise NotImplementedError

    def discovered_groups(self, bot_id: str, include_joined: bool = False) -> dict:
        """``{"can_read_all","can_join_groups","groups","warnings"}``。"""
        raise NotImplementedError

    def get_profile(self, bot_id: str) -> dict:
        """``{"display_name": str}``。"""
        raise NotImplementedError

    def set_profile(self, bot_id: str, display_name: str) -> dict:
        """``{"display_name": 写入后的值, "message": "已保存" | "没有改动"}``；先过 ``display_name_error``。"""
        raise NotImplementedError

    def set_enabled(self, bot_id: str, enabled: bool, *, op_id=None, moved_to=..., import_from=None) -> dict:
        """``{"enabled": bool, "running": bool, "message": str}``。
        停用 50 秒后仍在收消息抛 ``AdapterError("still_running", 503)``。"""
        raise NotImplementedError

    # 单次拒绝抛 AdapterError；多步中途失败抛 LifecycleError（带 steps、state）；
    # 整侧不可用抛 AdapterUnavailable。op_id 只用于记录。
    def archive(self, bot_id: str, shell_meta: dict, *, op_id=None) -> dict:
        """``{"archive_id", "message", "steps"}``；``shell_meta`` 原样存进这份归档。"""
        raise NotImplementedError

    def list_archives(self) -> list:
        """``[{"archive_id","bot_id","display_name","person","archived_at","status","shell_meta"}]``。"""
        raise NotImplementedError

    def restore(self, archive_id: str, *, op_id=None) -> dict:
        """``{"bot_id","enabled": False,"running": False,"message","steps"}``：结果恒为停用态。"""
        raise NotImplementedError

    def delete(self, bot_id: str, confirm: str, *, op_id=None) -> dict:
        """``{"message","trash","trash_path","steps"}``；``confirm`` 与 bot_id 不逐字相同 → 422 invalid。"""
        raise NotImplementedError

    def templates(self) -> list:
        """``[{"id","label"}]``：这种底层能在管理台新建的模板。"""
        raise NotImplementedError

    def create(self, spec: dict, token) -> dict:
        """``{"bot_id","person","username","enabled","running","message","steps"}``。``token`` 是
        ``SecretToken``，不放进 ``spec``；失败抛 AdapterError（动手之前）或 LifecycleError（带 steps、state）。"""
        raise NotImplementedError

    def take_token(self, bot_id: str, op):
        """只供切换任务的 ``take_token`` 步骤：返回 ``SecretToken``；门不满足就拒绝。"""
        raise NotImplementedError

    def resolve(self, op) -> dict:
        """按本侧清单收拾中断的操作：``{"state", "message", …}``。dsh 侧不实现，壳只复核。"""
        raise NotImplementedError


# ── 校验函数 ─────────────────────────────────────────────────────────────────
def person_of(bot) -> str:
    """bot 条目的 person：底层给了非空字符串就用它，没给就等于 bot_id。"""
    bot = bot if isinstance(bot, dict) else {}
    person = bot.get("person")
    return person if isinstance(person, str) and person else str(bot.get("bot_id") or "")


def same_bot(a, b) -> bool:
    """“同一只”的唯一判定：person 相同，或两边 username 都已知且相同（不分大小写）。"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    person = person_of(a)
    if person and person == person_of(b):
        return True
    ua, ub = a.get("username"), b.get("username")
    return isinstance(ua, str) and isinstance(ub, str) and ua != "" and ua.lower() == ub.lower()


def polling_of(bot) -> bool:
    """读一条 bot 条目的“在收消息”：底层给了布尔的 polling 就用它；没给（或给的不是布尔）
    就按 state 为 running 或 idle 算在收，宁可误报。"""
    bot = bot if isinstance(bot, dict) else {}
    polling = bot.get("polling")
    if isinstance(polling, bool):
        return polling
    return bot.get("state") in ("running", "idle")


def check_notify(source, text, key) -> None:
    """投递通知的入参校验（两种适配器共用）：不合规抛 ``AdapterError("invalid", 422)``，什么都不发。"""
    if source not in NOTIFY_SOURCES:
        raise AdapterError("invalid", 422, "通知来源只能是 %s" % "、".join(NOTIFY_SOURCES))
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > NOTIFY_MAX_TEXT_BYTES:
        raise AdapterError("invalid", 422, "通知正文不能为空，且不能超过 16 KiB")
    if not isinstance(key, str) or not 1 <= len(key) <= NOTIFY_MAX_KEY:
        raise AdapterError("invalid", 422, "去重键必须是 1 到 200 个字符")


def valid_chat_id(chat_id) -> bool:
    return isinstance(chat_id, str) and GROUP_CHAT_ID_RE.fullmatch(chat_id) is not None


def valid_group_chat_id(value):
    """群号的唯一校验（壳的新增接口、发现接口与适配器共用）。

    只收整数写法（字符串或 JSON 整数），返回规范化后的字符串；别的写法返回 None。
    布尔是 int 的子类，先单独排掉；-0、-01、17 位、正数、空格、@用户名、链接、数组、null 都不收。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    return value if GROUP_CHAT_ID_INT_RE.fullmatch(value) is not None else None


def group_policy_error(policy):
    """群设置不合规时返回给人看的一句话，合规返回 None（值为 None 表示删掉这个键）。"""
    if not isinstance(policy, dict):
        return "policy 必须是键值表"
    for key, value in policy.items():
        if key not in GROUP_POLICY_KEYS:
            return "policy 里不能有 %s" % key
        if value is None:
            continue
        if key == "requireMention":
            if not isinstance(value, bool):
                return "requireMention 必须是布尔"
        elif key == "intro":
            if not isinstance(value, str) or len(value) > GROUP_INTRO_MAX:
                return "介绍必须是字符串，最多 %d 个字符" % GROUP_INTRO_MAX
        else:
            if not isinstance(value, list) or len(value) > GROUP_LIST_MAX:
                return "%s 必须是列表，最多 %d 项" % (key, GROUP_LIST_MAX)
            for item in value:
                if not isinstance(item, str) or not 1 <= len(item) <= GROUP_ITEM_MAX:
                    return "%s 的每一项都必须是 1 到 %d 个字符的字符串" % (key, GROUP_ITEM_MAX)
                if key == "otherBotUsernames" and not GROUP_USERNAME_RE.fullmatch(item):
                    return "otherBotUsernames 的每一项都必须是 5 到 32 位字母、数字或下划线"
    return None


def check_group_body(body) -> dict:
    """进群 / 退群请求体的校验：不合规抛 ``AdapterError("invalid", 422)``，什么都不写。
    返回只含认识的键的干净副本：退群 ``{"chat_id", "join": False}``，进群另带 ``policy``（缺省为空表）。"""
    if not isinstance(body, dict):
        raise AdapterError("invalid", 422, "请求体必须是键值表")
    chat_id, join = body.get("chat_id"), body.get("join")
    if not valid_chat_id(chat_id):
        raise AdapterError("invalid", 422, "群号必须是 - 后跟 1 到 20 位数字")
    if not isinstance(join, bool):
        raise AdapterError("invalid", 422, "join 必须是布尔")
    if not join:
        return {"chat_id": chat_id, "join": False}
    policy = body.get("policy", {})
    err = group_policy_error(policy)
    if err:
        raise AdapterError("invalid", 422, err)
    return {"chat_id": chat_id, "join": True, "policy": dict(policy)}


def persona_version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def check_persona_body(body) -> tuple:
    """人设写入的请求体校验：只许 text、version 两个键；不合规抛 ``AdapterError("invalid", 422)``。
    返回 ``(text, version)``，版本号统一成小写。"""
    if not isinstance(body, dict):
        raise AdapterError("invalid", 422, "请求体必须是键值表")
    extra = [k for k in body if k not in PERSONA_KEYS]
    if extra:
        raise AdapterError("invalid", 422, "请求体只能有 text 和 version")
    text, version = body.get("text"), body.get("version")
    if not isinstance(text, str) or not text.strip():
        raise AdapterError("invalid", 422, "人设不能为空")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:   # 落单的代理字符：存不成 UTF-8
        raise AdapterError("invalid", 422, "人设里有无法保存的字符")
    if size > PERSONA_MAX_BYTES:
        raise AdapterError("invalid", 422, "人设不能超过 256 KiB")
    if not isinstance(version, str) or PERSONA_VERSION_RE.fullmatch(version) is None:
        raise AdapterError("invalid", 422, "版本号必须是 16 位十六进制")
    return text, version.lower()


def language_error(value):
    """语言名不合规时返回给人看的一句话，合规返回 None（None 表示删键，由调用方另判，这里不收）。
    换行在去首尾空白之前就查：值里任何位置有换行都不收。"""
    if not isinstance(value, str):
        return "语言必须是字符串"
    if len(("x" + value + "x").splitlines()) > 1:
        return "语言名里不能有换行"
    name = value.strip()
    if not 1 <= len(name) <= LANGUAGE_MAX:
        return "语言名去掉首尾空白后必须是 1 到 %d 个字符" % LANGUAGE_MAX
    if any(c in LANGUAGE_FORBIDDEN for c in name):
        return "语言名里不能有 %s" % " ".join(LANGUAGE_FORBIDDEN)
    return None


def check_features_body(body, keys) -> dict:
    """改开关的请求体校验（壳与两种适配器共用同一条规则）：不合规抛 ``AdapterError("invalid", 422)``。

    ``keys``：这只 bot 底层 GET 返回的功能键名；为 None 时只查形状、不判认不认识——壳在拿到底层
    的键之前先用它挡掉形状不对的请求，认不认识由适配器按自己那一侧的键再判一次。逐键的原因放在
    ``fields``，不认识的键先报“没有这个功能”，其余值不是布尔的报“只能是 true 或 false”。"""
    if not isinstance(body, dict):
        raise AdapterError("invalid", 422, FEATURES_NOT_OBJECT)
    if any(k != FEATURES_BODY_KEY for k in body):
        raise AdapterError("invalid", 422, FEATURES_EXTRA_KEYS)
    features = body.get(FEATURES_BODY_KEY)
    if not isinstance(features, dict) or not features:
        raise AdapterError("invalid", 422, FEATURES_EMPTY)
    fields = {}
    for key, value in features.items():
        if keys is not None and key not in keys:
            fields[key] = FEATURE_UNKNOWN
        elif not isinstance(value, bool):
            fields[key] = FEATURE_NOT_BOOL
    if fields:
        raise AdapterError("invalid", 422, FEATURES_INVALID, fields)
    return dict(features)


def check_voice_body(body) -> dict:
    """语音写入的请求体校验：只许 voice_id、tts_model、text_language、voice_language，至少给一个；
    值为 None 表示删掉。不合规抛 ``AdapterError("invalid", 422)``。返回只含给了的键的副本；
    语言去掉首尾空白。"""
    if not isinstance(body, dict):
        raise AdapterError("invalid", 422, "请求体必须是键值表")
    if any(k not in VOICE_KEYS for k in body) or not body:
        raise AdapterError("invalid", 422, "请求体只能有 %s，且至少要有一个" % "、".join(VOICE_KEYS))
    voice_id = body.get("voice_id")
    if voice_id is not None and not (isinstance(voice_id, str) and VOICE_ID_RE.fullmatch(voice_id)):
        msg = "语音编号只能用字母、数字、下划线、连字符，1 到 64 个字符"
        raise AdapterError("invalid", 422, msg, {"voice_id": msg})
    model = body.get("tts_model")
    if model is not None and model not in TTS_MODELS:
        msg = "语音模型只能是 %s 之一" % "、".join(TTS_MODELS)
        raise AdapterError("invalid", 422, msg, {"tts_model": msg})
    out = dict(body)
    for key in LANGUAGE_KEYS:
        if out.get(key) is None:
            continue
        err = language_error(out[key])
        if err:
            raise AdapterError("invalid", 422, err, {key: err})
        out[key] = out[key].strip()
    return out
