# -*- coding: utf-8 -*-
"""claude 底层适配器（T13）：真实实现，替换 T12 占位。

分层（PLAN §3.6）：

- 进程：``claude_proc`` 只调 dispatcher 本机 HTTP ``/status`` / ``/ensure_worker``，
  停用与重启按 ``/status`` 的 pid 结束 worker；不再依赖终端会话或平台启动项。
- 文件：``claude_files``（归档、恢复、删除、令牌）与 ``claude_create``（新建暂存、提交、回滚）。
- 平台服务：主动消息定时任务经 ``platform_services`` 注册/卸载，不写 plist。
- 供应商：只转调 ``provider_config`` 的写函数（T09 尚未移植时按 ``backend_unconfigured`` 拒绝）。
- 人设/语音/功能开关/群设置：直接读本机频道目录文件，用 ``platform_lock`` 加跨进程锁。

能力矩阵如实报缺：``bot.life_settings`` 依赖尚未移植的 ``life_settings`` 模块，报 false；
其余实现的方法报 true。
"""
import contextlib
import datetime
import hashlib
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time

import platform_lock
import yaml

import bots_registry
from . import claude_create, claude_files, claude_proc
from .base import (AdapterError, AdapterUnavailable, BackendAdapter, BOT_ID_MESSAGE, CONFIRM_RULE,
                   ENABLED_TYPE_MESSAGE, GROUP_USERNAME_RE, PROFILE_SAVED, PROFILE_UNCHANGED,
                   check_features_body, check_group_body, check_notify, check_persona_body,
                   check_voice_body, display_name_error, persona_version, valid_bot_id,
                   valid_group_chat_id, CLAUDE_CAPABILITIES)
from .guard import SecretToken

_log = logging.getLogger(__name__)

API_TOKEN_FILENAME = bots_registry.API_TOKEN_FILENAME
ACCESS_NAME = "access.json"
FEATURES_NAME = "features.json"
GROUP_SEEN_NAME = "groups-seen.json"
TELEGRAM_ME_NAME = ".telegram-me.json"
PERSONA_NAME = "CLAUDE.md"
PERSONA_BACKUP_DIR = ".persona-bak"
BACKUP_KEEP = 2
LOCK_WAIT = 3.0
FEATURE_KEYS = ("voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups")
FEATURE_LABELS = {"voice": "发语音", "image": "生图", "stickers": "表情包", "moments": "朋友圈",
                  "proactive": "主动找你聊", "life": "生活作息", "relationship": "关系进展", "groups": "群聊"}
GROUP_SEEN_MAX = 50
GROUP_SEEN_WARNING = "刚发现的群的记录读不出，已当成空的"
MOMENTS_OFF_MESSAGE = "这只 bot 的朋友圈功能关着"
STATE_TEXT = {"running": "运行中", "idle": "空闲，来消息会自己起来", "stopped": "已停用",
              "broken": "配置文件损坏，加载失败，需要人处理"}
_PERSONA_LOCKS = {}
_PERSONA_LOCKS_GUARD = threading.Lock()

CAPABILITIES = dict(CLAUDE_CAPABILITIES)
CAPABILITIES["bot.life_settings"] = False   # life_settings 模块随 T16 移植，届时改 true


# ── 路径与配置 ───────────────────────────────────────────────────────────────

def _channels_root() -> str:
    raw = os.environ.get("HUB_CHANNELS_DIR") or os.path.join("~", ".claude", "channels")
    return os.path.realpath(os.path.expanduser(raw))


def _load_cfg(bot_id: str):
    path = os.path.join(str(bots_registry.configs_dir()), bot_id + ".yml")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _known_bots() -> list:
    out = []
    for item in bots_registry.bots(include_disabled=True):
        bot_id = item["id"]
        cfg = _load_cfg(bot_id) or {}
        raw = cfg.get("bot_channel_path")
        bot_dir = os.path.expanduser(raw) if isinstance(raw, str) and raw else os.path.join(_channels_root(), bot_id)
        out.append({"bot_id": bot_id, "bot_dir": bot_dir, "has_config": True,
                    "display_name": item.get("display_name") or bot_id, "broken": False})
    return out


def _files_ctx() -> dict:
    """每次调用现组的 claude_files 上下文：三个接缝取注册表的当前值。"""
    return claude_files.context(
        channels=_channels_root(),
        config=str(bots_registry.configs_dir()),
        disabled=bots_registry.disabled_dir(),
        bot_id_re=re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*"),
        non_bot_dirs=frozenset({"media", "group_transcripts"}),
        known_bots=_known_bots,
    )


def _ids() -> set:
    return {b["id"] for b in bots_registry.bots(include_disabled=True)}


def _require_bot(bot_id: str) -> dict:
    if not valid_bot_id(bot_id):
        raise AdapterError("invalid", 422, BOT_ID_MESSAGE)
    cfg = _load_cfg(bot_id)
    if bot_id not in _ids() or cfg is None:
        raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
    return cfg


def _channel_dir(bot_id: str) -> str:
    cfg = _require_bot(bot_id)
    raw = cfg.get("bot_channel_path")
    path = os.path.expanduser(raw) if isinstance(raw, str) and raw else os.path.join(_channels_root(), bot_id)
    real = os.path.realpath(path)
    root = _channels_root()
    if real != root and not real.startswith(root + os.sep):
        raise AdapterError("internal", 500, "这只 bot 的频道目录不在频道根内，拒绝读写")
    return real


def _port(bot_id: str):
    return bots_registry.ports().get(bot_id)


def _log_id(bot_id) -> str:
    return bot_id if valid_bot_id(bot_id) else "编号不合规"


@contextlib.contextmanager
def _file_lock(path: str, *, timeout: float = LOCK_WAIT, message: str = "别的操作正在改这只 bot 的设置，请稍后再试"):
    try:
        with platform_lock.file_lock(path, timeout=timeout):
            yield
    except platform_lock.LockBusy:
        raise AdapterError("busy", 409, message) from None
    except platform_lock.LockError:
        raise AdapterError("internal", 500, "锁文件建不出来，文件没有改动") from None


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise AdapterError("internal", 500, "文件读不出来") from None


def _write_atomic(path: str, data: bytes, mode: int = None) -> None:
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _file_mode(path: str, default: int = 0o600) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return default


def _backup(path: str) -> None:
    """同目录备份，只留最近 BACKUP_KEEP 份；备份失败不阻断写入。"""
    try:
        raw = open(path, "rb").read()
        folder = os.path.dirname(path)
        base = os.path.join(folder, os.path.basename(path) + ".bak-" + _stamp())
        with open(os.open(base, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _file_mode(path)), "wb") as f:
            f.write(raw)
        prefix = os.path.basename(path) + ".bak-"
        for old in sorted(x for x in os.listdir(folder) if x.startswith(prefix))[:-BACKUP_KEEP]:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(folder, old))
    except OSError:
        pass


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


# ── 供应商（只转发 provider_config）───────────────────────────────────────────

def _provider_config():
    try:
        return importlib.import_module("provider_config")
    except ImportError:
        raise AdapterUnavailable("unconfigured", "供应商配置模块还没就绪") from None


class ClaudeLocalAdapter(BackendAdapter):
    backend = "claude"

    # ── 能力与名单 ──────────────────────────────────────────────────────────
    def meta(self) -> dict:
        return {"backend": "claude", "backend_version": "local", "adapter": "v1",
                "capabilities": dict(CAPABILITIES), "effort_levels": ["low", "medium", "high", "xhigh", "max"]}

    def bots(self) -> list:
        ports = bots_registry.ports()
        out = []
        pc = None
        with contextlib.suppress(ImportError):
            pc = importlib.import_module("provider_config")
        data = {}
        if pc is not None:
            with contextlib.suppress(Exception):
                data = pc.load_providers()
        bindings = data.get("bindings") if isinstance(data.get("bindings"), dict) else {}
        for item in bots_registry.bots(include_disabled=True):
            bot_id = item["id"]
            cfg = _load_cfg(bot_id) or {}
            enabled = bots_registry.is_enabled(cfg, bot_id)
            try:
                st = claude_proc.status(bot_id, ports.get(bot_id))
                alive = st.get("alive") is True
                phase = st.get("phase")
            except claude_proc.DispatcherError:
                alive, phase = False, None
            state = "stopped" if not enabled else ("running" if alive else "idle")
            binding = bindings.get(bot_id) if isinstance(bindings.get(bot_id), dict) else {}
            provider = None
            source = "default"
            if binding.get("provider_id"):
                provider = {"id": binding.get("provider_id"), "name": binding.get("provider_id"),
                            "model": binding.get("model"), "effort": binding.get("effort")}
                source = "override"
            username = cfg.get("telegram_username")
            out.append({
                "bot_id": bot_id, "person": bot_id,
                "display_name": item.get("display_name") or bot_id,
                "state": state, "state_text": STATE_TEXT[state],
                "provider": provider, "provider_source": source, "pending": None,
                "detail": {"has_config": True, "worker_alive": alive, "phase": phase},
                "enabled": enabled, "polling": alive,
                "username": username if isinstance(username, str) and GROUP_USERNAME_RE.fullmatch(username) else None,
                "moved_to": _read_moved(bot_id),
            })
        return out

    def providers(self) -> dict:
        pc = _provider_config()
        try:
            data = pc.load_providers()
        except Exception:
            raise AdapterUnavailable("error", "供应商清单读不出来") from None
        out = []
        for p in data.get("providers") or []:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            out.append({"id": p.get("id"), "name": p.get("name") or p.get("id"),
                        "has_key": bool(p.get("api_key")), "models": list(p.get("models") or [])})
        return {"providers": out, "default": {"label": "全局设置（可以是官方订阅）"}}

    def set_provider(self, bot_id: str, body: dict) -> dict:
        """只转调 provider_config：本适配器不直接写 settings.json / .env / providers.json。
        停用的 bot 也照写，由壳决定何时重启（与 CBL 的 allow_stopped 一致）。"""
        _require_bot(bot_id)
        pc = _provider_config()
        if not isinstance(body, dict):
            raise AdapterError("invalid", 422, "请求体必须是键值表")
        reset = body.get("reset") is True
        effort = body.get("effort")
        if effort is not None and effort not in ("low", "medium", "high", "xhigh", "max"):
            raise AdapterError("invalid", 422, "思考强度只支持 low、medium、high、xhigh、max")
        result = {"applied": True, "queued": False, "new_segment": False, "message": "已保存，下一条消息起生效"}
        try:
            if reset:
                ok, err = pc.clear_bot_provider(bot_id)
                if not ok:
                    raise AdapterError("invalid", 422, err or "恢复默认失败")
                result["message"] = "已恢复默认，下一条消息起生效"
            elif body.get("provider_id") is not None:
                out, err = pc.set_bot_provider(bot_id, {"provider_id": body.get("provider_id")},
                                               body.get("model"))
                if out is None:
                    raise AdapterError("invalid", 422, err or "换供应商失败")
                result["message"] = "已保存，下一条消息起生效"
            if effort is not None:
                ok, err = pc.set_bot_effort(bot_id, effort)
                if ok is False:
                    raise AdapterError("invalid", 422, err or "思考强度没改成")
        except AdapterUnavailable:
            raise
        except AdapterError:
            raise
        except Exception as e:
            _log.error("换供应商失败 %r：%s", _log_id(bot_id), type(e).__name__)
            raise AdapterError("internal", 500, "供应商设置没写成") from None
        return result

    # ── 进程 ────────────────────────────────────────────────────────────────
    def restart(self, bot_id: str, force: bool) -> dict:
        cfg = _require_bot(bot_id)
        if not bots_registry.is_enabled(cfg, bot_id):
            raise AdapterError("stopped", 409, "已停用，先启用再重启")
        try:
            return claude_proc.restart_worker(bot_id, _port(bot_id), force=bool(force))
        except claude_proc.DispatcherError as e:
            if e.status == "unauthorized":
                raise AdapterUnavailable("unauthorized", "dispatcher 口令不对") from None
            if e.status == "down":
                raise AdapterUnavailable("down", "dispatcher 没在运行") from None
            raise AdapterError("internal", 500, e.message) from None

    # ── 生活设定（T16 移植 life_settings 前如实报未配置）────────────────────
    def get_life(self, bot_id: str) -> dict:
        _require_bot(bot_id)
        try:
            ls = importlib.import_module("life_settings")
        except ImportError:
            raise AdapterUnavailable("unconfigured", "生活设定模块还没就绪") from None
        try:
            return ls.get_life(bot_id)
        except Exception as e:
            raise AdapterError("internal", 500, getattr(e, "message", "生活设定读不出来")) from None

    def set_life(self, bot_id: str, values: dict) -> dict:
        _require_bot(bot_id)
        try:
            ls = importlib.import_module("life_settings")
        except ImportError:
            raise AdapterUnavailable("unconfigured", "生活设定模块还没就绪") from None
        try:
            return ls.set_life(bot_id, values)
        except Exception as e:
            raise AdapterError(getattr(e, "code", "internal"), 500,
                               getattr(e, "message", "生活设定没写成")) from None

    # ── 通知 ────────────────────────────────────────────────────────────────
    def notify(self, bot_id: str, source: str, text: str, key: str) -> dict:
        check_notify(source, text, key)
        cfg = _require_bot(bot_id)
        if not bots_registry.is_enabled(cfg, bot_id):
            raise AdapterError("stopped", 409, "bot 已停用，没有执行")
        bot_dir = _channel_dir(bot_id)
        if not _features_state(bot_dir)["features"]["moments"]:
            raise AdapterError("feature_off", 409, MOMENTS_OFF_MESSAGE)
        chat_id = str(cfg.get("chat_id") or "").strip()
        if not chat_id:
            raise AdapterError("internal", 500, "这只 bot 没有私聊编号，投不进去")
        inbox = os.path.join(bot_dir, "chats", chat_id, "inbox")
        os.makedirs(inbox, mode=0o700, exist_ok=True)
        ms = int(time.time() * 1000)
        prefix = {"user_moment": "moment", "moment_reply": "moment-reply",
                  "moment_image": "moment-image"}[source]
        payload = {"text": text, "chat_id": chat_id, "from_id": chat_id,
                   "from_username": "user", "sender_username": "user", "chat_type": "private",
                   "is_bot_sender": False, "message_id": str(ms),
                   "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        path = os.path.join(inbox, "%s-%d-%s.json" % (prefix, ms, secrets.token_hex(3)))
        _write_atomic(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        with contextlib.suppress(claude_proc.DispatcherError):
            claude_proc.ensure_worker(bot_id, _port(bot_id))
        return {"status": "delivered"}

    # ── 群聊 ────────────────────────────────────────────────────────────────
    def groups(self, bot_id: str) -> dict:
        _require_bot(bot_id)
        bot_dir = _channel_dir(bot_id)
        try:
            data = _read_json(os.path.join(bot_dir, ACCESS_NAME))
        except FileNotFoundError:
            raise AdapterError("internal", 500, "access.json 不存在，读不出群设置") from None
        if not isinstance(data, dict):
            raise AdapterError("internal", 500, "access.json 不是对象")
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
        cfg = _load_cfg(bot_id) or {}
        username = cfg.get("telegram_username")
        return {"username": username if isinstance(username, str) else None, "groups": dict(groups)}

    def set_groups(self, bot_id: str, body: dict) -> dict:
        clean = check_group_body(body)
        _require_bot(bot_id)
        bot_dir = _channel_dir(bot_id)
        path = os.path.join(bot_dir, ACCESS_NAME)
        with _file_lock(path + ".group.lock", message="别的操作正在改这只 bot 的群设置，请稍后再试"):
            try:
                data = _read_json(path)
            except FileNotFoundError:
                raise AdapterError("internal", 500, "access.json 不存在，写不了群设置") from None
            if not isinstance(data, dict):
                raise AdapterError("internal", 500, "access.json 不是对象")
            groups = data.get("groups")
            if not isinstance(groups, dict):
                groups = data["groups"] = {}
            chat_id = clean["chat_id"]
            if clean["join"]:
                entry = dict(groups.get(chat_id) or {}) if isinstance(groups.get(chat_id), dict) else {}
                for key, value in clean.get("policy", {}).items():
                    if value is None:
                        entry.pop(key, None)
                    elif key == "otherBotUsernames":
                        entry[key] = ["@" + u for u in value]
                    else:
                        entry[key] = value
                groups[chat_id] = entry
                result = {"in_group": True, "policy": entry}
            else:
                groups.pop(chat_id, None)
                result = {"in_group": False, "policy": None}
            _backup(path)
            _write_atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                          _file_mode(path))
        return result

    def set_username(self, bot_id: str, username) -> None:
        if username is not None and not (isinstance(username, str) and GROUP_USERNAME_RE.fullmatch(username)):
            raise AdapterError("invalid", 422, "Telegram 用户名必须是 5 到 32 位字母、数字或下划线，不带 @")
        _require_bot(bot_id)
        _write_cfg_scalar(bot_id, "telegram_username", username)

    def discovered_groups(self, bot_id: str, include_joined: bool = False) -> dict:
        _require_bot(bot_id)
        bot_dir = _channel_dir(bot_id)
        warnings, groups = [], {}
        try:
            with open(os.path.join(bot_dir, GROUP_SEEN_NAME), encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError, ValueError):
            warnings.append(GROUP_SEEN_WARNING)
        else:
            groups = doc.get("groups") if isinstance(doc, dict) else None
            if not isinstance(groups, dict):
                warnings.append(GROUP_SEEN_WARNING)
                groups = {}
        try:
            me = _read_json(os.path.join(bot_dir, TELEGRAM_ME_NAME))
        except (FileNotFoundError, AdapterError):
            me = {}
        if not isinstance(me, dict):
            me = {}
        joined = set()
        if not include_joined:
            try:
                acc = _read_json(os.path.join(bot_dir, ACCESS_NAME))
                joined = set(acc.get("groups") or {}) if isinstance(acc, dict) else set()
            except (FileNotFoundError, AdapterError):
                joined = set()
        rows = []
        for raw_id, rec in groups.items():
            chat_id = valid_group_chat_id(raw_id)
            if chat_id is None or chat_id in joined or not isinstance(rec, dict):
                continue
            status = rec.get("status") if isinstance(rec.get("status"), str) else None
            if status in ("left", "kicked"):
                continue
            rows.append({"chat_id": chat_id, "title": rec.get("title") if isinstance(rec.get("title"), str) else "",
                         "type": rec.get("type") if isinstance(rec.get("type"), str) else None,
                         "status": status, "added_by_owner": rec.get("added_by_owner")
                         if isinstance(rec.get("added_by_owner"), bool) else None,
                         "sees_all": (me.get("can_read_all") is True or status == "administrator"),
                         "first_seen": rec.get("first_seen") if isinstance(rec.get("first_seen"), str) else None,
                         "last_seen": rec.get("last_seen") if isinstance(rec.get("last_seen"), str) else None})
        rows.sort(key=lambda r: r["last_seen"] or "", reverse=True)
        return {"can_read_all": me.get("can_read_all") if isinstance(me.get("can_read_all"), bool) else None,
                "can_join_groups": me.get("can_join_groups") if isinstance(me.get("can_join_groups"), bool) else None,
                "groups": rows[:GROUP_SEEN_MAX], "warnings": warnings}

    def group_chat_enabled(self, bot_id: str) -> bool:
        _require_bot(bot_id)
        return _features_state(_channel_dir(bot_id))["features"]["groups"]

    # ── 人设、语音 ──────────────────────────────────────────────────────────
    def get_persona(self, bot_id: str) -> dict:
        bot_dir = _channel_dir(bot_id)
        try:
            raw = open(os.path.join(bot_dir, PERSONA_NAME), "rb").read()
        except FileNotFoundError:
            raw = b""
        except OSError:
            raise AdapterError("internal", 500, "人设文件读不出来") from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise AdapterError("internal", 500, "人设文件不是 UTF-8，读不出来") from None
        return {"text": text, "version": persona_version(raw)}

    def set_persona(self, bot_id: str, body: dict) -> dict:
        bot_dir = _channel_dir(bot_id)
        text, version = check_persona_body(body)
        path = os.path.join(bot_dir, PERSONA_NAME)
        new_raw = text.encode("utf-8")
        with _persona_locked(bot_id, bot_dir):
            try:
                raw = open(path, "rb").read()
            except FileNotFoundError:
                raw = b""
            except OSError:
                raise AdapterError("internal", 500, "人设文件读不出来") from None
            if persona_version(raw) != version:
                raise AdapterError("stale", 409, "人设刚被别处改过，请刷新后再改")
            if new_raw == raw:
                return {"version": version, "message": PROFILE_UNCHANGED}
            if os.path.islink(path):
                raise AdapterError("internal", 500, "这只 bot 的人设文件是个链接，请到它指向的文件那里改")
            try:
                _backup(path)
                _write_atomic(path, new_raw, _file_mode(path))
            except OSError:
                raise AdapterError("internal", 500, "人设文件写不进去，文件没有改动") from None
        return {"version": persona_version(new_raw), "message": "已保存"}

    def get_voice(self, bot_id: str) -> dict:
        bot_dir = _channel_dir(bot_id)
        data = self._access(bot_dir)
        return _voice_view(data)

    def set_voice(self, bot_id: str, body: dict) -> dict:
        bot_dir = _channel_dir(bot_id)
        changes = check_voice_body(body)
        path = os.path.join(bot_dir, ACCESS_NAME)
        with _file_lock(path + ".group.lock", message="别的操作正在改这只 bot 的 access.json，请稍后再试"):
            data = self._access(bot_dir)
            before = dict(data)
            mapping = (("voice_id", "voiceId"), ("tts_model", "voiceModel"),
                       ("text_language", "textLanguage"), ("voice_language", "voiceLanguage"))
            for key, field in mapping:
                if key not in changes:
                    continue
                if changes[key] is None:
                    data.pop(field, None)
                else:
                    data[field] = changes[key]
            if data != before:
                _backup(path)
                _write_atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                              _file_mode(path))
        if "voice_id" in changes:
            _write_cfg_scalar(bot_id, "voice_id", changes["voice_id"])
        return _voice_view(data)

    @staticmethod
    def _access(bot_dir: str) -> dict:
        try:
            data = _read_json(os.path.join(bot_dir, ACCESS_NAME))
        except FileNotFoundError:
            raise AdapterError("internal", 500, "access.json 不存在，读不出来") from None
        if not isinstance(data, dict):
            raise AdapterError("internal", 500, "access.json 不是对象")
        return data

    # ── 功能开关 ────────────────────────────────────────────────────────────
    def get_features(self, bot_id: str) -> dict:
        bot_dir = _channel_dir(bot_id)
        state = _features_state(bot_dir)
        if not state["readable"]:
            raise AdapterError("internal", 500, "读不出这只 bot 的功能开关")
        return {"keys": [{"key": k, "label": FEATURE_LABELS[k]} for k in FEATURE_KEYS],
                "features": state["features"],
                "requirements": {k: {"ready": True, "missing": [], "notes": []} for k in FEATURE_KEYS},
                "skills_synced": True, "warnings": state["warnings"]}

    def set_features(self, bot_id: str, changes: dict) -> dict:
        changes = check_features_body({"features": changes}, FEATURE_KEYS)
        bot_dir = _channel_dir(bot_id)
        result = _features_write(bot_dir, changes)
        message = "已保存" if result["changed"] else "没有改动"
        return {"features": result["features"], "changed": result["changed"],
                "applied": True, "queued": False, "message": message,
                "skills_synced": True, "skills_placed": [],
                "warnings": result["warnings"]}

    # ── 显示名 ──────────────────────────────────────────────────────────────
    def get_profile(self, bot_id: str) -> dict:
        cfg = _require_bot(bot_id)
        name = cfg.get("display_name")
        return {"display_name": name.strip() if isinstance(name, str) and name.strip() else bot_id}

    def set_profile(self, bot_id: str, display_name: str) -> dict:
        err = display_name_error(display_name)
        if err:
            raise AdapterError("invalid", 422, err, fields={"display_name": err})
        _require_bot(bot_id)
        value = display_name.strip()
        changed = _write_cfg_scalar(bot_id, "display_name", value)
        return {"display_name": value, "message": PROFILE_SAVED if changed else PROFILE_UNCHANGED}

    # ── 启停 ────────────────────────────────────────────────────────────────
    def set_enabled(self, bot_id: str, enabled: bool, *, op_id=None, moved_to=..., import_from=None) -> dict:
        if not isinstance(enabled, bool):
            raise AdapterError("invalid", 422, ENABLED_TYPE_MESSAGE)
        if import_from is not None:
            raise AdapterError("unsupported", 501, "claude 侧不支持从别的底层导入")
        _require_bot(bot_id)
        if enabled and _read_moved(bot_id) is not None:
            raise AdapterError("moved", 409, "这只 bot 已迁出，不能再启用")
        if not enabled and moved_to is not ...:
            _write_moved(bot_id, moved_to, op_id)
        _write_enabled(bot_id, enabled)
        if not enabled:
            try:
                claude_proc.stop_worker(bot_id, _port(bot_id))
            except claude_proc.DispatcherError as e:
                if e.status == "unauthorized":
                    raise AdapterUnavailable("unauthorized", "dispatcher 口令不对") from None
                if "还在运行" in e.message:
                    raise AdapterError("still_running", 503, "停用后它仍在收消息") from None
                # dispatcher 已经不在：worker 自然也不在
            return {"enabled": False, "running": False, "message": "已停用"}
        if not claude_proc.polling(bot_id, _port(bot_id)):
            try:
                claude_proc.ensure_worker(bot_id, _port(bot_id))
            except claude_proc.DispatcherError as e:
                if e.status == "unauthorized":
                    raise AdapterUnavailable("unauthorized", "dispatcher 口令不对") from None
                if e.status == "down":
                    raise AdapterUnavailable("down", "dispatcher 没在运行") from None
                raise AdapterError("internal", 500, "没能启用：dispatcher 不可用") from None
        running = claude_proc.wait_polling(bot_id, _port(bot_id), True, time.monotonic() + 15)
        return {"enabled": True, "running": running,
                "message": "已启用" if running else "已启用，worker 还没起来"}

    # ── 归档、恢复、删除 ────────────────────────────────────────────────────
    def list_archives(self) -> list:
        return claude_files.list_archives(ctx=_files_ctx())

    def archive(self, bot_id: str, shell_meta: dict, *, op_id=None) -> dict:
        _require_bot(bot_id)
        op_id = _op_id(op_id, "archive")
        steps = []
        self._stop_for_lifecycle(bot_id, steps)
        try:
            res = claude_files.archive(bot_id, op_id=op_id, ctx=_files_ctx(), shell_meta=shell_meta or None)
        except claude_files.FilesError as e:
            raise AdapterError(e.code, e.http_status, e.message) from None
        except claude_files.FilesStuck as e:
            raise AdapterError("step_failed", 500, e.message) from None
        return {"archive_id": res["archive_id"], "message": "已归档", "steps": steps}

    def restore(self, archive_id: str, *, op_id=None) -> dict:
        try:
            res = claude_files.restore(archive_id, op_id=_op_id(op_id, "restore"), ctx=_files_ctx())
        except claude_files.FilesError as e:
            raise AdapterError(e.code, e.http_status, e.message) from None
        except claude_files.FilesStuck as e:
            raise AdapterError("step_failed", 500, e.message) from None
        return {"bot_id": res.get("bot_id"), "enabled": False, "running": False,
                "message": "已恢复，保持停用", "steps": []}

    def delete(self, bot_id: str, confirm: str, *, op_id=None) -> dict:
        if confirm != bot_id:
            raise AdapterError("invalid", 422, CONFIRM_RULE, fields={"confirm": CONFIRM_RULE})
        _require_bot(bot_id)
        steps = []
        self._stop_for_lifecycle(bot_id, steps)
        try:
            res = claude_files.delete(bot_id, op_id=_op_id(op_id, "delete"), ctx=_files_ctx())
        except claude_files.FilesError as e:
            raise AdapterError(e.code, e.http_status, e.message) from None
        except claude_files.FilesStuck as e:
            raise AdapterError("step_failed", 500, e.message) from None
        return {"message": "已删除", "trash": res.get("trash"), "trash_path": res.get("trash_path"),
                "steps": steps}

    def _stop_for_lifecycle(self, bot_id: str, steps: list) -> None:
        try:
            res = self.set_enabled(bot_id, False)
            steps.append({"step": "stop", "status": "done", "message": res.get("message", "")})
        except AdapterError as e:
            if e.code == "stopped":
                steps.append({"step": "stop", "status": "skipped", "message": "本来就停着"})
                return
            raise

    # ── 新建、模板、令牌 ────────────────────────────────────────────────────
    def templates(self) -> list:
        return claude_create.list_templates(_files_ctx())

    def chat_id_default(self):
        counts = {}
        for item in bots_registry.bots(include_disabled=True):
            cfg = _load_cfg(item["id"]) or {}
            value = cfg.get("chat_id")
            if isinstance(value, bool):
                continue
            if isinstance(value, str) and value.isascii() and value.isdigit():
                value = int(value)
            if isinstance(value, int) and value > 0:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            return None
        top = max(counts.values())
        return str(min(v for v, n in counts.items() if n == top))

    def create(self, spec: dict, token) -> dict:
        if not isinstance(spec, dict):
            raise AdapterError("invalid", 422, "请求体必须是 JSON 对象")
        if not isinstance(token, SecretToken):
            raise AdapterError("invalid", 422, "令牌格式不对")
        picked = spec.get("features")
        if picked is None:
            picked = []
        elif not isinstance(picked, (list, tuple)):
            raise AdapterError("invalid", 422, "features 必须是数组")
        picked = list(picked)
        voice = spec.get("voice") if isinstance(spec.get("voice"), dict) else {}
        voice_id = voice.get("voice_id")
        if isinstance(voice_id, str) and voice_id.strip() and "voice" not in picked:
            picked.append("voice")
        try:
            doc = _initial_features(picked, voice_id)
        except ValueError as e:
            raise AdapterError("invalid", 422, str(e)) from None
        return claude_create.create(spec.get("bot_id"), dict(spec, features=doc), token,
                                    op_id=_op_id(spec.get("op_id"), "create"), ctx=_files_ctx())

    def take_token(self, bot_id: str, op):
        return claude_files.take_token(bot_id, op, ctx=_files_ctx())

    def resolve(self, op) -> dict:
        ctx = _files_ctx()
        if isinstance(op, dict) and op.get("kind") == "create":
            return claude_create.resolve(op, ctx=ctx)
        return claude_files.resolve(op, ctx=ctx)


# ── 功能开关文件 ─────────────────────────────────────────────────────────────

def _features_state(bot_dir: str) -> dict:
    path = os.path.join(bot_dir, FEATURES_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {"features": dict.fromkeys(FEATURE_KEYS, True), "warnings": [], "readable": True}
    except (OSError, UnicodeDecodeError, ValueError):
        return {"features": dict.fromkeys(FEATURE_KEYS, False), "warnings": [], "readable": False}
    if not isinstance(doc, dict):
        return {"features": dict.fromkeys(FEATURE_KEYS, False), "warnings": [], "readable": False}
    default = doc.get("default") is True
    given = doc.get("features") if isinstance(doc.get("features"), dict) else {}
    state, warnings = {}, []
    for key in FEATURE_KEYS:
        value = given.get(key, default)
        if isinstance(value, bool):
            state[key] = value
        else:
            state[key] = False
            warnings.append("%s的开关值不是真假，已按关着处理" % key)
    return {"features": state, "warnings": warnings, "readable": True}


def _features_write(bot_dir: str, changes: dict) -> dict:
    path = os.path.join(bot_dir, FEATURES_NAME)
    with _file_lock(path + ".lock"):
        state = _features_state(bot_dir)
        if not state["readable"]:
            raise AdapterError("internal", 500, "读不出这只 bot 的功能开关")
        before = state["features"]
        changed = [k for k in FEATURE_KEYS if k in changes and changes[k] != before[k]]
        created = not os.path.exists(path)
        if not changed and not created:
            return {"features": before, "changed": [], "warnings": state["warnings"]}
        features = dict(before)
        features.update({k: changes[k] for k in changed})
        doc = {"version": 1, "default": False, "features": features}
        _backup(path)
        _write_atomic(path, (json.dumps(doc, ensure_ascii=True, indent=2) + "\n").encode("ascii"),
                      _file_mode(path))
        return {"features": features, "changed": changed, "warnings": state["warnings"]}


def _initial_features(picked, voice_id) -> dict:
    features = dict.fromkeys(FEATURE_KEYS, False)
    for key in picked:
        if not isinstance(key, str) or key not in FEATURE_KEYS:
            raise ValueError("没有这个功能：%r" % (key,))
        if key == "voice" and not (isinstance(voice_id, str) and voice_id.strip()):
            continue
        features[key] = True
    return {"version": 1, "default": False, "features": features}


# ── 人设锁 ───────────────────────────────────────────────────────────────────

def _persona_lock(bot_id: str) -> threading.Lock:
    with _PERSONA_LOCKS_GUARD:
        lock = _PERSONA_LOCKS.get(bot_id)
        if lock is None:
            lock = _PERSONA_LOCKS[bot_id] = threading.Lock()
        return lock


@contextlib.contextmanager
def _persona_locked(bot_id: str, bot_dir: str):
    lock = _persona_lock(bot_id)
    if not lock.acquire(timeout=LOCK_WAIT):
        raise AdapterError("busy", 409, "别的操作正在改这只 bot 的人设，请稍后再试")
    try:
        if not os.path.isdir(bot_dir):
            yield
            return
        try:
            with platform_lock.file_lock(os.path.join(bot_dir, ".persona.lock"), timeout=LOCK_WAIT):
                yield
        except platform_lock.LockBusy:
            raise AdapterError("busy", 409, "别的操作正在改这只 bot 的人设，请稍后再试") from None
        except platform_lock.LockError:
            raise AdapterError("internal", 500, "人设锁建不出来，文件没有改动") from None
    finally:
        lock.release()


# ── 配置文本改写 ─────────────────────────────────────────────────────────────

def _cfg_path(bot_id: str) -> str:
    return os.path.join(str(bots_registry.configs_dir()), bot_id + ".yml")


def _write_cfg_scalar(bot_id: str, key: str, value) -> bool:
    """只替换/追加 yml 顶层一个键，保留其余字节与注释。返回是否改动。"""
    path = _cfg_path(bot_id)
    with _file_lock(path + ".life.lock"):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            raise AdapterError("internal", 500, "配置文件读不出来") from None
        line = "%s: %s" % (key, json.dumps(value, ensure_ascii=False))
        lines = text.splitlines(True)
        found, changed = False, False
        for i, old in enumerate(lines):
            if re.match(r"^%s\s*:" % re.escape(key), old):
                found = True
                if value is None:      # None = 删掉这个顶层键
                    del lines[i]
                    changed = True
                    break
                body = old.rstrip("\r\n")
                ending = old[len(body):] or "\n"
                if body.strip() == line.strip():
                    return False
                lines[i] = line + ending
                changed = True
                break
        if not found and value is not None:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(line + "\n")
            changed = True
        if not changed:
            return False
        _backup(path)
        _write_atomic(path, "".join(lines).encode("utf-8"), _file_mode(path))
        return True


def _write_enabled(bot_id: str, enabled: bool) -> None:
    _write_cfg_scalar(bot_id, "enabled", bool(enabled))


def _voice_view(data: dict) -> dict:
    out = {}
    for key, field in (("voice_id", "voiceId"), ("tts_model", "voiceModel"),
                       ("text_language", "textLanguage"), ("voice_language", "voiceLanguage")):
        value = data.get(field)
        out[key] = value if isinstance(value, str) and value else None
    return out


# ── 已迁出标记（T15 切换用；只读写 dispatcher 状态目录）────────────────────────

def _moved_path(bot_id: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "dispatcher", ".moved", bot_id)


def _read_moved(bot_id: str):
    try:
        with open(_moved_path(bot_id), encoding="utf-8") as f:
            value = f.read(200).strip()
    except OSError:
        return None
    return value or None


def _write_moved(bot_id: str, target, op_id=None) -> None:
    path = _moved_path(bot_id)
    if target is None:
        with contextlib.suppress(OSError):
            os.unlink(path)
        return
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if target == "pending":
        value = "pending"
    elif isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", target):
        value = target
    else:
        raise AdapterError("invalid", 422, "迁出目标不合规")
    _write_atomic(path, value.encode("utf-8"), 0o600)


def _op_id(value, kind: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,100}", value):
        return value
    return "%s-%s-%s" % (kind, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"), secrets.token_hex(3))
