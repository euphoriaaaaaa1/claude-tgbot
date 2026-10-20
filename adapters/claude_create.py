"""claude 侧新建（INTERFACE-bot-lifecycle 3.4.2 A、3.5.1；PLAN D6、D8、D11 的新建部分）。

只管新建这一件事：模板展开、暂存写入与暂存清单、端口分配、信任检查、提交（两次同卷改名）、装启动项、
首次拉起自检、整单撤回、以及重启后的续完。编排与任务记录在 adapters/lifecycle_flow.py 与 adapters/ops.py。

安全面：令牌只从 ``SecretToken`` 取出写进暂存目录的 ``.env``（``O_CREAT|O_EXCL|O_NOFOLLOW``、0600），
不写进暂存清单、日志、异常与返回值；撤回时先抹掉所有 ``.env``，再把其余东西移进撤回件。
路径一律由 ``ctx``（claude_files.context）给的根拼出，写入前不跟随软链。
"""
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

import yaml

import platform_services
from . import claude_files, claude_proc
from .base import (AdapterError, LifecycleError, chat_id_error, display_name_error, new_bot_id_error,
                   NEW_BOT_ID_MESSAGE, NEW_BOT_ID_RE, PERSONA_BOTH_MESSAGE, PERSONA_TEMPLATE_MISSING,
                   PERSONA_TEXT_MESSAGE)
from .guard import SecretToken

PORT_FIRST, PORT_LAST = 17806, 17849
CREATE_LIMIT = 55.0            # 秒：整条新建封顶（3.4.2 A）
FIRST_SPAWN_WAIT = 20.0        # 秒：首次拉起自检窗口
START_PROBE_WAIT = 3.0         # 秒：新建时先短等 dispatcher 端口应答
START_RETRY_WAIT = 5.0         # 秒：唤起自启服务后再短等
START_KICK_WAIT = 2.0          # 秒：唤起自启服务本身的等待上限（端口判断整体 ≤ 10 秒）
FIRST_SPAWN_SKIPPED = "dispatcher 未启动，首次拉起将在服务启动后自动完成"
PROBE_TIMEOUT = 0.2
TRASH_SUFFIX = "新建撤回"
TEMPLATE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
SPEC_KEYS = frozenset(("bot_id", "person", "display_name", "telegram_username", "telegram_user_id",
                       "chat_id", "enabled", "persona_template", "persona_text", "voice", "purpose", "op_id",
                       "features", "telegram_token", "confirm_other_side_unknown"))
# claude 侧不收这两个键（3.4.2 A）；出现就整单拒绝
REJECTED_SPEC_KEYS = ("fish_api_key", "port")

MESSAGES = {
    "trust": "第一次新建 claude 侧 bot 前，需要先在终端执行 cd ~/.claude/channels && claude，按提示选择信任，然后退出（/exit），再回来重新新建",
    "no_port": "没有可用的调度器端口，没有新建",
    "timeout": "新建超时，已撤回",
    "first_spawn": "首次拉起没有等到 worker 会话，已撤回",
    "unavailable": "新建没成功，已撤回",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _step(steps, key, status, message=""):
    steps.append({"step": key, "status": status, "message": message, "at": _now_iso()})


def _mark(steps, key, status):
    for item in reversed(steps):
        if item["step"] == key:
            item["status"] = status
            return


# ── 模板 ────────────────────────────────────────────────────────────────────

def template_root(ctx) -> str:
    return os.path.join(ctx["config"], "templates", "claude")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_templates(ctx) -> list:
    """``configs/templates/claude/*/template.json``：``[{"id","label"}]``，按目录名排序。"""
    root = template_root(ctx)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(root, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        meta = _read_json(os.path.join(path, "template.json"))
        if not meta:
            continue
        tid, label = meta.get("id"), meta.get("label")
        if isinstance(tid, str) and tid and isinstance(label, str):
            out.append({"id": tid, "label": label})
    return out


def _template_dir(template_id, ctx):
    """模板编号必须是普通目录名；不认路径分隔符与点点。"""
    if not isinstance(template_id, str) or TEMPLATE_ID_RE.fullmatch(template_id) is None \
            or template_id.strip(".") == "":
        return None
    path = os.path.join(template_root(ctx), template_id)
    return path if os.path.isdir(path) and not os.path.islink(path) else None


def _template_text(paths, name, default):
    path = os.path.join(paths, name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return default


def _template_json(paths, name):
    data = _read_json(os.path.join(paths, name))
    return data if isinstance(data, dict) else {}


# ── 小工具 ──────────────────────────────────────────────────────────────────

def _write_new(path, data: bytes, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_text(path, text, mode=0o600):
    _write_new(path, text.encode("utf-8"), mode)


def _write_replace(path, data: bytes, mode=0o600):
    """原子写（同目录临时文件 + fsync + 改名），用于清单与 plist 的后续更新。"""
    folder = os.path.dirname(path)
    fd, tmp = secrets_tmp(folder, os.path.basename(path))
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            if os.path.exists(tmp):
                os.unlink(tmp)


def secrets_tmp(folder, base):
    fd, tmp = tempfile.mkstemp(prefix="." + base + ".", suffix=".tmp", dir=folder)
    return fd, tmp


def _spawn_env() -> dict:
    """拉起进程的环境：去掉管理台自己的运行开关。worker 由 dispatcher 拉起，环境由 dispatcher 决定。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("MOMENTS_WEB_") and k != "MOMENTS_DISABLE_CONVERGER"}


def _port_open(port: int) -> bool:
    """本机这个端口此刻有没有人在监听：连得上算占用；超时/别的错误宁可算占用。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


def _used_ports(ctx) -> set:
    """所有 yml 与归档清单里出现过的 dispatcher_port。yml 直接按目录读：本模块不导入旧栈模块。"""
    used = set()
    try:
        names = os.listdir(ctx["config"])
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".yml") or name.startswith("."):
            continue
        try:
            with open(os.path.join(ctx["config"], name), encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        port = cfg.get("dispatcher_port") if isinstance(cfg, dict) else None
        if isinstance(port, int) and not isinstance(port, bool):
            used.add(port)
    try:
        for item in claude_files.list_archives(ctx=ctx):
            meta = item.get("shell_meta") if isinstance(item, dict) else None
            port = meta.get("dispatcher_port") if isinstance(meta, dict) else None
            if isinstance(port, int) and not isinstance(port, bool):
                used.add(port)
            else:
                fallback = _archived_config_port(item, ctx)
                if fallback is not None:
                    used.add(fallback)
    except Exception:
        pass
    return used


def _archived_config_port(item, ctx):
    """归档清单里的 shell_meta 没写端口时，读归档件里的 config yml（历史归档兜底）。"""
    bot = item.get("bot_id") if isinstance(item, dict) else None
    archive_id = item.get("archive_id") if isinstance(item, dict) else None
    if not isinstance(bot, str) or not isinstance(archive_id, str):
        return None
    items_dir = os.path.join(ctx["archive"], archive_id, claude_files.ITEMS)
    try:
        names = os.listdir(items_dir)
    except OSError:
        return None
    for name in names:
        if not name.endswith("-" + bot + ".yml"):
            continue
        try:
            with open(os.path.join(items_dir, name), encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        port = cfg.get("dispatcher_port") if isinstance(cfg, dict) else None
        if isinstance(port, int) and not isinstance(port, bool):
            return port
    return None


def allocate_port(ctx) -> int:
    used = _used_ports(ctx)
    for port in range(PORT_FIRST, PORT_LAST + 1):
        if port in used or _port_open(port):
            continue
        return port
    raise AdapterError("unavailable", 503, MESSAGES["no_port"])


def _channel_dir(bot_id, ctx) -> str:
    return os.path.join(ctx["channels"], bot_id)


def _project_dir(bot_id, ctx) -> str:
    return os.path.join(ctx["projects"], claude_files.project_dir_name(_channel_dir(bot_id, ctx)))


def _moved_marker(bot_id, ctx) -> str:
    return os.path.join(ctx["dispatcher"], ".moved", bot_id)


def _archived(bot_id, ctx) -> bool:
    try:
        return any(name.startswith(bot_id + "-") for name in os.listdir(ctx["archive"]))
    except OSError:
        return False


# ── 受理期检查 ──────────────────────────────────────────────────────────────

def _check_trust(channel: str, ctx) -> bool:
    """D6：只读 ``~/.claude.json`` 的 projects；频道目录或其任一父目录有信任布尔 true 即通过。"""
    doc = _read_json(os.path.join(ctx["home"], ".claude.json"))
    projects = doc.get("projects") if doc else None
    if not isinstance(projects, dict):
        return False
    seen = set()
    for base in (channel, os.path.realpath(channel)):
        path = base
        while True:
            if path in seen:
                break
            seen.add(path)
            entry = projects.get(path)
            if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
                return True
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return False


def _validate_spec(spec, token) -> dict:
    """适配器侧的二次校验（壳已经校验过一次）；返回整理后的 spec。不动任何文件。"""
    if not isinstance(spec, dict):
        raise AdapterError("invalid", 422, "请求体必须是 JSON 对象")
    for key in REJECTED_SPEC_KEYS:
        if key in spec:
            raise AdapterError("invalid", 422, "claude 侧不收这个键：%s" % key)
    extra = sorted(k for k in spec if k not in SPEC_KEYS)
    if extra:
        raise AdapterError("invalid", 422, "claude 侧不收这个键：%s" % extra[0])
    if not isinstance(token, SecretToken):
        raise AdapterError("invalid", 422, "令牌格式不对")
    bot_id = spec.get("bot_id")
    purpose = spec.get("purpose") or "new"
    if purpose not in ("new", "switch_in"):
        raise AdapterError("invalid", 422, "purpose 只能是 new 或 switch_in")
    if purpose == "switch_in":   # 切换建目标不查保留名单（3.4.2 A）
        err = None if isinstance(bot_id, str) and NEW_BOT_ID_RE.fullmatch(bot_id) else NEW_BOT_ID_MESSAGE
    else:
        err = new_bot_id_error(bot_id)
    if err:
        raise AdapterError("invalid", 422, err, {"bot_id": err})
    display_name = spec.get("display_name", bot_id)
    err = display_name_error(display_name)
    if err:
        raise AdapterError("invalid", 422, err, {"display_name": err})
    chat_id = spec.get("chat_id")
    chat_text = chat_id if isinstance(chat_id, str) else str(chat_id)
    err = chat_id_error(chat_text)
    if err:
        raise AdapterError("invalid", 422, err, {"chat_id": err})
    if not isinstance(spec.get("enabled", True), bool):
        raise AdapterError("invalid", 422, "enabled 必须是 true 或 false")
    if "persona_template" in spec and "persona_text" in spec:
        raise AdapterError("invalid", 422, PERSONA_BOTH_MESSAGE)
    text = spec.get("persona_text")
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 256 * 1024:
            raise AdapterError("invalid", 422, PERSONA_TEXT_MESSAGE, {"persona_text": PERSONA_TEXT_MESSAGE})
    username = spec.get("telegram_username")
    user_id = spec.get("telegram_user_id")
    if not isinstance(username, str) or not username:
        raise AdapterError("invalid", 422, "缺 Telegram 用户名")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise AdapterError("invalid", 422, "缺 Telegram 用户编号")
    voice = spec.get("voice")
    if voice is not None and not isinstance(voice, dict):
        raise AdapterError("invalid", 422, "voice 必须是键值表")
    features = spec.get("features")
    if features is not None and not isinstance(features, dict):
        raise AdapterError("invalid", 422, "features 必须是功能文档")
    clean = dict(spec)
    clean.update(bot_id=bot_id, person=spec.get("person") or bot_id, display_name=display_name.strip(),
                 chat_id=chat_text, enabled=spec.get("enabled", True), purpose=purpose,
                 telegram_username=username, telegram_user_id=user_id)
    clean.pop("telegram_token", None)
    return clean


def precheck(bot_id: str, purpose: str, ctx) -> None:
    """已存在 → 409 exists；编号不合规 → 422。swtich_in 允许沿用会话目录、停用标记、已迁出标记。"""
    err = new_bot_id_error(bot_id)
    if err and purpose != "switch_in":
        raise AdapterError("invalid", 422, err)
    channel = _channel_dir(bot_id, ctx)
    config = os.path.join(ctx["config"], bot_id + ".yml")
    hits = [p for p in (config, channel) if os.path.lexists(p)]
    if purpose != "switch_in":
        hits += [p for p in (_project_dir(bot_id, ctx), _moved_marker(bot_id, ctx))
                 if os.path.lexists(p)]
    if hits or _archived(bot_id, ctx):
        raise AdapterError("exists", 409, "编号 %s 已被占用：claude 侧已有" % bot_id)


# ── 暂存写入与提交 ──────────────────────────────────────────────────────────

def _persona_text(spec, ctx) -> str:
    if isinstance(spec.get("persona_text"), str):
        return spec["persona_text"]
    tid = spec.get("persona_template") or "blank"
    paths = _template_dir(tid, ctx)
    if paths is None:
        if spec.get("persona_template"):
            raise AdapterError("invalid", 422, PERSONA_TEMPLATE_MISSING, {"persona_template": PERSONA_TEMPLATE_MISSING})
        return ""
    return _template_text(paths, "CLAUDE.md", "")


def _voice_view(voice) -> dict:
    if not isinstance(voice, dict):
        return {}
    mapping = (("voice_id", "voiceId"), ("tts_model", "voiceModel"),
               ("text_language", "textLanguage"), ("voice_language", "voiceLanguage"))
    return {out: voice[key] for key, out in mapping if isinstance(voice.get(key), str) and voice[key]}


def _write_payload(payload: str, spec: dict, token: SecretToken, channel: str, ctx) -> None:
    os.makedirs(payload, mode=0o700, exist_ok=True)
    os.chmod(payload, 0o700)
    tid = spec.get("persona_template")
    paths = _template_dir(tid, ctx) if tid else None
    _write_text(os.path.join(payload, "CLAUDE.md"), _persona_text(spec, ctx), 0o600)

    access = _template_json(paths, "access.json") if paths else {}
    access.update(dmPolicy="allowlist", allowFrom=[spec["chat_id"]], groups={})
    access.setdefault("splitOnParagraph", True)
    access.update(_voice_view(spec.get("voice")))
    _write_text(os.path.join(payload, "access.json"),
                json.dumps(access, ensure_ascii=False, indent=2) + "\n", 0o600)

    # 令牌：只在这里落盘；O_CREAT|O_EXCL|O_NOFOLLOW、0600、逐字一行
    _write_text(os.path.join(payload, ".env"), "TELEGRAM_BOT_TOKEN=%s\n" % token.reveal(), 0o600)

    claude = os.path.join(payload, ".claude")
    os.makedirs(claude, mode=0o700, exist_ok=True)
    settings = _template_json(paths, "settings.json") if paths else {}
    settings.pop("env", None)
    _write_text(os.path.join(claude, "settings.json"),
                json.dumps(settings, ensure_ascii=False, indent=2) + "\n", 0o600)

    features = spec.get("features")
    if isinstance(features, dict):
        _write_text(os.path.join(payload, "features.json"),
                    json.dumps(features, ensure_ascii=True, indent=2) + "\n", 0o600)

    skills_src = os.path.join(paths, "skills") if paths else None
    if skills_src and os.path.isdir(skills_src) and not os.path.islink(skills_src):
        for name in sorted(os.listdir(skills_src)):
            src = os.path.join(skills_src, name, "SKILL.md")
            if os.path.islink(src) or not os.path.isfile(src):
                continue
            dst = os.path.join(claude, "skills", "%s-%s" % (spec["bot_id"], name))
            os.makedirs(dst, mode=0o700, exist_ok=True)
            _write_text(os.path.join(dst, "SKILL.md"), _template_text(os.path.dirname(src), "SKILL.md", ""), 0o600)

    os.makedirs(os.path.join(payload, "inbox"), mode=0o700, exist_ok=True)


def _yml_text(spec: dict, channel: str, port: int) -> str:
    def q(value):
        return json.dumps(value, ensure_ascii=False)

    lines = [
        "id: %s" % q(spec["bot_id"]),
        "display_name: %s" % q(spec["display_name"]),
        "bot_channel_path: %s" % q(channel),
        "chat_id: %d" % int(spec["chat_id"]),
        "dispatcher_port: %d" % port,
        "namespace_uuid: %s" % q(str(uuid.uuid4())),
        "telegram_username: %s" % q(spec["telegram_username"]),
        "telegram_user_id: %d" % int(spec["telegram_user_id"]),
        "launcher: registry",
        "created_at: %s" % q(_now_iso()),
    ]
    if spec.get("enabled", True) is False:
        lines.append("enabled: false")
    voice = spec.get("voice") if isinstance(spec.get("voice"), dict) else {}
    if isinstance(voice.get("voice_id"), str) and voice["voice_id"]:
        lines.append("voice_id: %s" % q(voice["voice_id"]))
    return "\n".join(lines) + "\n"


def _write_staging(bot_id, spec, token, port, ctx):
    """写暂存目录与暂存清单；返回 (staging_dir, payload, yml_tmp, manifest)。停用标记最先写。
    半路失败时自己收拾干净（抹 .env、删暂存与刚写的停用标记），再原样抛出。"""
    staging = os.path.join(ctx["channels"], ".staging-%s-%s" % (bot_id, secrets.token_hex(4)))
    yml_tmp = os.path.join(ctx["config"], ".staging-%s-%s.yml" % (bot_id, secrets.token_hex(4)))
    try:
        os.mkdir(staging, 0o700)
        payload = os.path.join(staging, "channel")
        _write_payload(payload, spec, token, _channel_dir(bot_id, ctx), ctx)
        _write_new(yml_tmp, _yml_text(spec, _channel_dir(bot_id, ctx), port).encode("utf-8"), 0o644)
        written = ["CLAUDE.md", "access.json", ".env", ".claude/settings.json", "inbox"]
        if isinstance(spec.get("features"), dict):
            written.append("features.json")
        manifest = {"op_id": spec["op_id"], "bot_id": bot_id, "phase": "staged", "enabled": spec["enabled"],
                    "chat_id": spec["chat_id"], "port": port, "channel": _channel_dir(bot_id, ctx),
                    "yml_tmp": yml_tmp, "created_at": _now_iso(),
                    "purpose": spec.get("purpose") or "new",
                    "written": written}
        _write_text(os.path.join(staging, "staging.json"), json.dumps(manifest, ensure_ascii=False) + "\n", 0o600)
        return staging, payload, yml_tmp, manifest
    except BaseException:
        _wipe_staging_envs(bot_id, ctx, staging)
        with contextlib.suppress(OSError, shutil.Error):
            shutil.rmtree(staging)
        with contextlib.suppress(OSError):
            os.unlink(yml_tmp)
        raise


def _save_manifest(staging, manifest):
    _write_replace(os.path.join(staging, "staging.json"),
                   json.dumps(manifest, ensure_ascii=False).encode("utf-8"), 0o600)


# ── 启动项与首次拉起 ────────────────────────────────────────────────────────

def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _service_backend():
    """每次现取：测试可替换本模块的 ``platform_services``，也不会在导入期碰平台命令。"""
    return platform_services.resolve_backend()


def _install_schedule(bot_id, chat_id, ctx) -> str:
    """把主动消息定时任务注册进平台服务（launchd / systemd --user / 计划任务），不再写 plist。"""
    spec = platform_services.service_spec("self-initiate-" + bot_id, repo=_repo_root(),
                                          bot=bot_id, chat_id=str(chat_id))
    result = platform_services.run_action(platform_services.ACTION_REGISTER, spec, _service_backend())
    if not getattr(result, "ok", False):
        raise OSError(getattr(result, "detail", "") or "平台服务注册失败")
    return spec.name


def _uninstall_schedule(bot_id) -> None:
    """撤掉主动消息定时任务；没装过按成功处理。平台不支持时静默跳过（文件层仍会撤回）。"""
    try:
        spec = platform_services.service_spec("self-initiate-" + bot_id, repo=_repo_root(),
                                              bot=bot_id, chat_id="0")
        platform_services.run_action(platform_services.ACTION_UNREGISTER, spec, _service_backend())
    except Exception:   # 撤回是收尾动作，平台不支持不能盖住原始失败
        pass


def _ensure_dispatcher(ctx, timeout: float) -> None:
    """新 bot 的 dispatcher 还没起：让开机自启服务重跑一遍（它会按注册表拉起全部启用的 bot）。
    自启服务没装或平台不支持时不在这里报错：随后等端口应答，等不到按“端口不在线”处理。"""
    name = "dispatchers" if os.name == "nt" else "autostart"
    try:
        spec = platform_services.service_spec(name, repo=_repo_root())
        platform_services.run_action(platform_services.ACTION_KICKSTART, spec, _service_backend())
    except Exception:
        pass


def _kick_dispatcher(ctx, wait: float) -> None:
    """唤起自启服务，但最多等 ``wait`` 秒：平台命令卡住也不能吃掉新建的端口判断预算。
    超时后让它在后台线程里自己结束（daemon，不阻塞进程退出）。"""
    worker = threading.Thread(target=_ensure_dispatcher, args=(ctx, wait), daemon=True)
    worker.start()
    worker.join(max(0.0, wait))


def _dispatcher_online(bot_id) -> bool:
    """dispatcher 端口此刻是否应答。``polling`` 会把“不可达”和“worker 停了”都报成 False，
    这里要区分：端口不应答才算“没启动”。"""
    try:
        claude_proc.status(bot_id)
        return True
    except claude_proc.DispatcherError:
        return False


def _wait_dispatcher(bot_id, deadline: float) -> bool:
    while True:
        if _dispatcher_online(bot_id):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(0.2, left))


def _start(bot_id, ctx, timeout: float) -> bool:
    """等 dispatcher 端口应答；没有就唤起自启服务再短等。

    端口始终不在线时返回 False（新建不因此失败，首次拉起留到服务启动后）；调用方据此跳过
    ``first_spawn``。端口在线但 worker 拉不起来仍是真故障，由 ``first_spawn`` 照常报错。
    整体判断封顶约 10 秒，不耗尽 ``CREATE_LIMIT``。
    """
    if timeout <= 0:
        raise AdapterError("unavailable", 503, MESSAGES["timeout"])
    now = time.monotonic()
    if _wait_dispatcher(bot_id, now + min(START_PROBE_WAIT, timeout)):
        return True
    _kick_dispatcher(ctx, min(START_KICK_WAIT, max(0.0, timeout - (time.monotonic() - now))))
    if _wait_dispatcher(bot_id, time.monotonic() + min(START_RETRY_WAIT, timeout)):
        return True
    return False


def first_spawn(bot_id, chat_id, port, ctx, deadline: float) -> None:
    """首次拉起自检：POST /ensure_worker，并在 20 秒内等到 phase=starting/running。"""
    end = min(deadline, time.monotonic() + FIRST_SPAWN_WAIT)
    try:
        claude_proc.ensure_worker(bot_id, port)
    except claude_proc.DispatcherError as e:
        raise AdapterError("step_failed", 500, e.message) from None
    if not claude_proc.wait_polling(bot_id, port, True, end):
        if time.monotonic() >= deadline:
            raise AdapterError("unavailable", 503, MESSAGES["timeout"])
        raise AdapterError("step_failed", 500, MESSAGES["first_spawn"])


# ── 撤回 ────────────────────────────────────────────────────────────────────

def _trash_root(ctx) -> str:
    """优先系统废纸篓；建不了或改名失败再由调用方回落到 ~/.claude/bot-trash。"""
    return ctx["system"]


def _wipe_envs(path: str) -> list:
    """把路径下所有叫 .env 的文件删掉（不跟随软链）；返回删不掉的路径（调用方据此决定不许移动）。"""
    failed = []
    if not os.path.isdir(path) or os.path.islink(path):
        return failed
    for base, _dirs, files in os.walk(path):
        for name in files:
            if name == ".env":
                target = os.path.join(base, name)
                try:
                    os.unlink(target)
                except FileNotFoundError:
                    pass
                except OSError:
                    failed.append(target)
    return failed


def _staging_roots(bot_id: str, ctx, staging=None) -> list:
    """这只 bot 的所有暂存目录：**按前缀扫描**渠道根下的 ``.staging-<bot>-*``，不只认记录里那一个。
    进程在写完 ``.env``、还没写 ``staging.json`` 时被 kill -9，重启后没有任何清单，只能靠前缀找到它。"""
    roots = []
    if isinstance(staging, str) and staging:
        roots.append(staging)
    try:
        names = os.listdir(ctx["channels"])
    except OSError:
        names = []
    prefix = ".staging-%s-" % bot_id
    for name in names:
        path = os.path.join(ctx["channels"], name)
        if name.startswith(prefix) and path not in roots:
            roots.append(path)
    return roots


def _wipe_staging_envs(bot_id: str, ctx, staging=None) -> list:
    failed = []
    for root in _staging_roots(bot_id, ctx, staging):
        failed.extend(_wipe_envs(root))
    return failed


def _staging_ymls(bot_id: str, ctx) -> list:
    """这只 bot 还没提交的暂存 yml（``configs/.staging-<bot>-*.yml``）：中断在两次改名之间时也要清掉。
    里面没有密钥（令牌只在频道目录的 .env）。"""
    try:
        names = os.listdir(ctx["config"])
    except OSError:
        return []
    prefix = ".staging-%s-" % bot_id
    return [os.path.join(ctx["config"], name) for name in names
            if name.startswith(prefix) and name.endswith(".yml")]


def _move(path, dst):
    if os.path.lexists(path):
        os.replace(path, dst)


def rollback(bot_id, ctx, *, enabled: bool, port, plist, channel: str, yml: str, staging, steps,
             keep_marker: bool = False) -> list:
    """整单撤回：先停（启用的才停）、确认不在收消息；抹掉所有 .env；其余移进撤回件；删 plist、停用标记。"""
    problems = []
    if enabled:
        try:
            claude_proc.stop_worker(bot_id, port)
            _mark(steps, "start", "undone")
        except Exception:
            problems.append("没能把它停下")
    _uninstall_schedule(bot_id)
    # 先抹所有 .env（含按前缀找到的、没有清单的暂存目录）；一个都抹不掉才谈移动：
    # 抹不掉时绝不把带令牌的目录移进废纸篓，文件留在原处交给人
    env_failed = _wipe_envs(channel) + _wipe_staging_envs(bot_id, ctx, staging)
    for leftover_yml in _staging_ymls(bot_id, ctx):   # 没提交的暂存 yml：不是正式文件，直接清掉
        with contextlib.suppress(OSError):
            os.unlink(leftover_yml)
    if env_failed:
        problems.append("令牌没抹干净（%d 个文件），文件留在原处，请手动处理" % len(env_failed))
        leftovers = []
    else:
        # 抹干净了：所有暂存骨架（含没有清单的、按前缀找到的）一起移进撤回件，不留空目录
        leftovers = [p for p in (channel, yml) if p and os.path.lexists(p)]
        leftovers += [p for p in _staging_roots(bot_id, ctx, staging) if os.path.lexists(p)]
    if leftovers:
        stamp = _stamp()
        for root in (_trash_root(ctx), ctx["own"]):
            try:
                os.makedirs(root, mode=0o700, exist_ok=True)
                dest = os.path.join(root, "%s-%s-%s" % (bot_id, stamp, TRASH_SUFFIX))
                os.makedirs(dest, mode=0o700, exist_ok=True)
                for src in leftovers:
                    if src == channel:
                        name = "channel"
                    elif src == yml:
                        name = "config"
                    else:
                        name = os.path.basename(src).lstrip(".") or "staging"
                    target = os.path.join(dest, name)
                    if os.path.lexists(target):
                        target = target + "-" + secrets.token_hex(2)
                    _move(src, target)
                break
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(root, "%s-%s-%s" % (bot_id, stamp, TRASH_SUFFIX)))
        else:
            problems.append("撤回件写不进去")
    for leftover in leftovers:
        if isinstance(leftover, str) and leftover.startswith(ctx["channels"] + os.sep):
            with contextlib.suppress(OSError):
                shutil.rmtree(leftover)
    return problems


# ── 对外入口 ────────────────────────────────────────────────────────────────

def create(bot_id, spec, token, *, op_id, ctx) -> dict:
    """新建一只 claude 侧 bot（3.4.2 A）。成功返回 3.4.1 的形状；失败由适配器撤回并按 state 抛 LifecycleError。"""
    steps = []
    warnings = []
    started = time.monotonic()
    deadline = started + CREATE_LIMIT
    purpose = "new"
    channel = None
    plist = None
    staging = None
    enabled = bool(spec.get("enabled", True))
    port = None
    touched = False
    try:
        clean = _validate_spec(spec, token)
        purpose = clean["purpose"]
        bot_id = clean["bot_id"]
        enabled = clean["enabled"]
        clean = dict(clean, op_id=op_id)
        channel = _channel_dir(bot_id, ctx)
        precheck(bot_id, purpose, ctx)   # 3.4.2 A 第 2 步：已存在先于信任检查
        _step(steps, "check_trust", "running")
        if not _check_trust(channel, ctx):
            _step(steps, "check_trust", "failed", MESSAGES["trust"])
            raise AdapterError("trust_missing", 503, MESSAGES["trust"])
        _step(steps, "check_trust", "done")
        _step(steps, "allocate_port", "running")
        port = allocate_port(ctx)
        clean["_port"] = port
        _step(steps, "allocate_port", "done", "调度器端口 %d" % port)
        _step(steps, "write_staging", "running")
        staging, payload, yml_tmp, manifest = _write_staging(bot_id, clean, token, port, ctx)
        touched = True
        _step(steps, "write_staging", "done")
        _step(steps, "commit", "running")
        if os.path.lexists(channel):
            raise AdapterError("exists", 409, "编号 %s 已被占用：claude 侧已有" % bot_id)
        os.rename(payload, channel)
        try:
            os.replace(yml_tmp, os.path.join(ctx["config"], bot_id + ".yml"))
        except Exception:
            with contextlib.suppress(OSError):
                os.rename(channel, payload)
            raise
        manifest.update(phase="committed")
        _save_manifest(staging, manifest)
        _step(steps, "commit", "done")
        # 令牌从这一刻起已在 .env 里，内存里的这一份用完即丢
        token = None
        _step(steps, "install_schedule", "running")
        _install_schedule(bot_id, clean["chat_id"], ctx)
        _step(steps, "install_schedule", "done")
        if enabled:
            _step(steps, "start", "running")
            if _start(bot_id, ctx, deadline - time.monotonic()):
                _step(steps, "start", "done")
                if time.monotonic() >= deadline:
                    raise AdapterError("unavailable", 503, MESSAGES["timeout"])
                _step(steps, "first_spawn", "running")
                first_spawn(bot_id, clean["chat_id"], port, ctx, deadline)
                _step(steps, "first_spawn", "done")
            else:
                # dispatcher 此刻不在线不算新建失败：启动项已装好，服务起来后会自行拉起
                _step(steps, "start", "done", "dispatcher 未启动，等待服务启动")
                _step(steps, "first_spawn", "skipped", FIRST_SPAWN_SKIPPED)
                warnings.append(FIRST_SPAWN_SKIPPED)
        else:
            _step(steps, "start", "skipped", "新建时保持停用")
            _step(steps, "first_spawn", "skipped", "新建时保持停用")
        try:
            import bots_registry
            bots_registry.reload()
        except Exception:
            pass
        _mark(steps, "refresh", "done")
        running = bool(enabled and claude_proc.polling(bot_id, port))
        with contextlib.suppress(OSError, shutil.Error):
            shutil.rmtree(staging)
        for root in _staging_roots(bot_id, ctx, staging):   # 历史残留骨架：抹掉 .env 后一并清走
            _wipe_envs(root)
            with contextlib.suppress(OSError, shutil.Error):
                shutil.rmtree(root)
        return {"bot_id": bot_id, "person": bot_id, "username": clean["telegram_username"],
                "enabled": enabled, "running": running, "message": "", "steps": steps,
                "warnings": warnings}
    except Exception as exc:
        reason = getattr(exc, "message", None) or MESSAGES["unavailable"]
        code = getattr(exc, "code", None)
        status = getattr(exc, "http_status", None)
        if not isinstance(code, str) or not isinstance(status, int):
            code, status = "step_failed", 500
        if not touched and staging is None:
            raise
        problems = rollback(bot_id, ctx, enabled=enabled, port=port, plist=plist,
                            channel=channel, yml=os.path.join(ctx["config"], bot_id + ".yml"),
                            staging=staging, steps=steps)
        if problems:
            raise LifecycleError("step_failed", 500, "%s；%s" % (reason, "；".join(problems)),
                                 steps=steps, state="needs_attention") from None
        raise LifecycleError(code, status, reason, steps=steps,
                             state="rolled_back" if touched else "unchanged") from None


def resolve(op, *, ctx) -> dict:
    """按暂存清单收拾中断的新建：未提交的撤回；已提交的续完启动项与首次拉起。"""
    rec = op if isinstance(op, dict) else getattr(op, "rec", {})
    bot_id = rec.get("bot_id") if isinstance(rec, dict) else None
    op_id = rec.get("op_id") if isinstance(rec, dict) else None
    if not isinstance(bot_id, str) or not bot_id:
        return {"state": "needs_attention", "message": "认不出新建的是哪只"}
    channel = _channel_dir(bot_id, ctx)
    yml = os.path.join(ctx["config"], bot_id + ".yml")
    staging = None
    try:
        for name in os.listdir(ctx["channels"]):
            if name.startswith(".staging-%s-" % bot_id):
                path = os.path.join(ctx["channels"], name)
                meta = _read_json(os.path.join(path, "staging.json"))
                if meta and meta.get("op_id") == op_id:
                    staging = path
                    break
    except OSError:
        pass
    committed = os.path.isdir(channel) and os.path.isfile(yml)
    if committed:
        steps = []
        warnings = []
        meta = _read_json(os.path.join(staging, "staging.json")) if staging else None
        meta = meta or {}
        enabled = meta.get("enabled") is True if isinstance(meta.get("enabled"), bool) else True
        port = meta.get("port") if isinstance(meta.get("port"), int) else None
        chat_id = meta.get("chat_id")
        if port is None or not isinstance(chat_id, str):
            cfg = claude_files._load_yml(yml) or {}
            raw_port = cfg.get("dispatcher_port")
            if port is None and isinstance(raw_port, int) and not isinstance(raw_port, bool):
                port = raw_port
            if not isinstance(chat_id, str):
                raw_chat = cfg.get("chat_id")
                chat_id = str(raw_chat) if raw_chat not in (None, "") else None
        try:
            if isinstance(chat_id, str):
                _install_schedule(bot_id, chat_id, ctx)
            if enabled and port is not None:
                if not claude_proc.polling(bot_id, port):
                    if _start(bot_id, ctx, 40):
                        first_spawn(bot_id, chat_id, port, ctx, time.monotonic() + 20)
                    else:
                        # dispatcher 还没起来：保持 committed，等自启服务拉起后再续
                        warnings.append(FIRST_SPAWN_SKIPPED)
            if staging:
                with contextlib.suppress(OSError, shutil.Error):
                    shutil.rmtree(staging)
            return {"state": "committed", "bot_id": bot_id, "enabled": enabled,
                    "running": bool(enabled and port and claude_proc.polling(bot_id, port)),
                    "warnings": warnings}
        except Exception as exc:
            problems = rollback(bot_id, ctx, enabled=enabled, port=port, plist=None,
                                channel=channel, yml=yml, staging=staging, steps=steps)
            return {"state": "needs_attention" if problems else "rolled_back", "bot_id": bot_id,
                    "message": getattr(exc, "message", MESSAGES["unavailable"])}
    problems = rollback(bot_id, ctx, enabled=False, port=None, plist=None,
                        channel=channel, yml=yml, staging=staging, steps=[])
    return {"state": "needs_attention" if problems else "rolled_back", "bot_id": bot_id}
