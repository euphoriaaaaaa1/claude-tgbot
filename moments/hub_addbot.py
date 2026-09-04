# -*- coding: utf-8 -*-
"""加 bot 三段流程（INTERFACE-hub2 §4 / PLAN D3）。

A 只读校验（失败零副作用）→ B 落盘（可回滚）→ C 注册常驻 + 拉起（失败不回滚 B，回 207）。

改本文件前先读这三条红线：

1. **token 的唯一落点是 `channels/<bot>/.env`**（F5）。`configs/<bot>.yml` 不含它；
   响应、stdout/stderr、异常文本一律不含它 —— 所以对外只抛 ``type(e).__name__``，
   绝不抛 ``str(e)``：requests 的异常消息里带完整请求 URL，而 URL 里就有 token。
2. **回滚按精确清单 unlink**（R8-3），绝不 ``rm -rf`` 任何已存在目录 ——
   用户的 bot 目录可能早就在那儿，一次手滑就是别人的聊天记录没了。
3. **重启成功判定看探活，不看退出码**（F4）。老用户的 `restart-bots.sh` 是
   `install.sh` 从 `.example` 拷出来的工作副本，里面硬编码 `BOTS=("chenlulu:17801")`，
   门户调它会"正常重启了别人、退出码 0"，据此回 201 就是成功响应撒谎。
"""
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit

import requests
from flask import current_app

from moments import bots_client, hub_auth, redact
from moments.provider_model import HubError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_API_BASE = "https://api.telegram.org"
#: §4.1 R2-1：主机 + 可选端口，**不带路径、不带 query**。
BASE_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?/?$")
#: 小写起头；禁 `_` 开头（`list_enabled_bots()` 会跳过它，建了也看不见）
BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{30,}$")
#: A7：既有 bot 的 telegram id = `.env` 里 token 的 `:` 前数字段，不新增字段、不落第二份 token
ENV_TOKEN_RE = re.compile(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*[\"']?(\d{8,10}):", re.M)
#: 上游 description 里的请求 URL 片段 `/bot<token>/getMe`：抹掉 token 后仍留着 `/bot…`，
#: 那还是把「原始请求 URL」回显给了页面（S6 明令不许），所以整段一并删掉。
URL_PATH_RE = re.compile(r"/bot\S*")

GETME_TIMEOUT = 10               # §4.1
MAX_BODY = 64 * 1024             # §4.1 R2-2：读满即停
MAX_PERSONA = 64 * 1024          # §4.2 A 段
MAX_DISPLAY_NAME = 64            # R8-1
PROBE_SECONDS = 30               # §4.2 C 段第 7 步
REQUIRED_FIELDS = ("bot_id", "telegram_token", "dispatcher_port", "chat_id")
BLANK_TEMPLATE = "_persona_template"
MEMORY_SEED = "# 记忆\n\n（还没聊过，记忆会自己长出来。）\n"
#: A3：门户/朋友圈进程持有 bot 列表，不重启它新 bot 在页面与朋友圈侧一直不可见
RESTART_FLAGS = {"bot": True, "moments_web": True}
#: B 段产物清单 = 回滚的精确 unlink 清单（R8-3），两处共用这一份，不许各写各的
TREE_FILES = (".env", "access.json", "CLAUDE.md", os.path.join("memory", "MEMORY.md"))
TREE_DIRS = ("memory", "logs")

HINT_OUTDATED = ("重启脚本还是旧版（硬编码 bot 列表），它带不上新 bot。执行这两条："
                 "cp restart-bots.example.sh restart-bots.sh ； bash restart-bots.sh")
HINT_RESTART = "文件都已就位，手动拉起：bash restart-bots.sh"


# ---------------------------------------------------------------- 测试接缝（§7）

def _registry():
    """延迟 import 仓库根模块 —— 与 `bots_client` 同款做法，别在 moments 包导入期就拉进来。"""
    import bots_registry
    return bots_registry


def channels_dir():
    """§7 `HUB_CHANNELS_DIR`（A2）：B 段产物树、模板扫描、A7 的 `.env` 比对全经它。"""
    return os.environ.get("HUB_CHANNELS_DIR") or os.path.expanduser("~/.claude/channels")


def configs_dir():
    """配置根与注册表同源（`HUB_CONFIGS_DIR` 在 bots_registry 里已解析），不另起一张表。"""
    return str(_registry().configs_dir())


def launchagents_dir():
    return (os.environ.get("HUB_LAUNCHAGENTS_DIR")
            or os.path.expanduser("~/Library/LaunchAgents"))


def restart_script():
    """§7 `HUB_RESTART_SCRIPT`（A2）：**只**决定 C 段第 5 步版本自检读哪个文件；
    「执行什么来重启」是 `HUB_RESTART_CMD` 的事，二者分工写死、互不覆盖。"""
    env = os.environ.get("HUB_RESTART_SCRIPT")
    if env:
        return env
    name = os.path.join("windows", "start-bots.ps1") if os.name == "nt" else "restart-bots.sh"
    return os.path.join(REPO_ROOT, name)


def api_base():
    """校验过形状的 getMe base（已 rstrip `/`）。非法 → 抛，蓝图注册期就炸（§7 R2-1）。

    它是环境变量不是请求参数，配错要在启动期暴露；等到用户点「测试 token」才 4xx，
    中间那段时间每一次调用都在往一个陌生主机送 token。
    """
    raw = (os.environ.get("HUB_TELEGRAM_API_BASE") or DEFAULT_API_BASE).strip()
    if not BASE_RE.match(raw):
        # 不回显原文：它可能被写成 https://user:pass@host 这种自带凭据的形状
        raise RuntimeError("HUB_TELEGRAM_API_BASE 形状非法："
                           "只接受 http(s)://主机[:端口]，不带路径与 query")
    return raw.rstrip("/")


@contextlib.contextmanager
def hold_addbot_lock(app=None):
    """整个创建流程持一把进程内非阻塞锁（R4），抢不到 → `409 config_busy`。

    与参数页的 `hub_config.hold_config_lock` **有意分开**：创建里有 10 秒 getMe
    加 30 秒探活，共用一把锁会让「改个参数」也被顶 409 半分钟。
    ponytail: 单进程前提（同 hub_config）；真上多 worker 就换 flock，接口不变。
    """
    ext = (app or current_app).extensions
    lock = ext.setdefault("hub_addbot", threading.Lock())     # setdefault 在 GIL 下原子
    if not lock.acquire(blocking=False):
        raise HubError(409, "config_busy", "另一次创建正在进行，请稍候重试")
    try:
        yield
    finally:
        lock.release()


# ---------------------------------------------------------------- getMe（§4.1）

def _sanitize(raw):
    """上游可控字符串的消毒：抹凭据 → 去请求 URL 片段 → 剔控制字符 → 截 200 字。

    页面那端一律 `textContent` 渲染，这端先消毒 —— 否则「配了个恶意 base」就是一条 XSS。
    """
    text = redact.scrub_text("" if raw is None else str(raw))
    text = URL_PATH_RE.sub("", text)
    text = "".join(ch for ch in text if 0x20 <= ord(ch) != 0x7f)
    return text.strip()[:200]


def _error_code(body, fallback=None):
    code = body.get("error_code") if isinstance(body, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else fallback


def _classify(status, body):
    """HTTP 状态 + body → §4.1 分型表的一行。"""
    if 300 <= status < 400 or not isinstance(body, dict):
        return {"ok": False, "stage": "bad_response"}
    if status in (401, 404):
        return {"ok": False, "stage": "invalid_token", "error_code": _error_code(body, status),
                "description": _sanitize(body.get("description"))}
    if status >= 500:
        return {"ok": False, "stage": "upstream", "error_code": _error_code(body, status)}
    # R9-1：HTTP 200 但 body `ok:false`（Telegram 限流的常见形态）。原稿缺这格，
    # 代码会落进"成功"分支取 result.username → KeyError → 500。
    if status >= 400 or body.get("ok") is not True:
        return {"ok": False, "stage": "rejected", "error_code": _error_code(body, status),
                "description": _sanitize(body.get("description"))}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    username, tg_id = result.get("username"), result.get("id")
    if not isinstance(username, str) or not username:      # R9-2：base 指向任意 HTTP 服务时必然遇到
        return {"ok": False, "stage": "bad_response"}
    return {"ok": True, "username": username[:64],
            "bot_id": tg_id if isinstance(tg_id, int) and not isinstance(tg_id, bool) else None}


def getme(token):
    """§4.1：**只回业务结果，从不抛**（HTTP 恒 200 由路由层保证）。

    三条外发面硬约束都在这里：形状预检 / 明文 http 非回环拒发 / 64 KiB 截断；
    外加写死 `allow_redirects=False` —— 跟随重定向会把带 token 的 URL 跟到 Location 主机。
    """
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return {"ok": False, "stage": "malformed"}       # 预检不过，一个请求都不发
    base = api_base()
    parts = urlsplit(base)
    loopback = hub_auth.is_loopback(parts.hostname or "")
    if parts.scheme == "http" and not loopback:
        # A1：一次配置手滑（明文 http）就把 token 明文送给第三方。本地预检，不发请求、不回显 base
        return {"ok": False, "stage": "blocked_base"}
    sess = requests.Session()
    if loopback:
        sess.trust_env = False        # 回环流量绝不经系统代理（机主常年开着 http_proxy）
    try:
        with sess.get("%s/bot%s/getMe" % (base, token), timeout=GETME_TIMEOUT,
                      allow_redirects=False, stream=True) as resp:
            if 300 <= resp.status_code < 400:            # R9-3
                return {"ok": False, "stage": "bad_response"}
            buf = bytearray()
            for chunk in resp.iter_content(8192):
                buf += chunk
                if len(buf) > MAX_BODY:                  # R2-2：读满即停，不给大文件灌 OOM 的机会
                    return {"ok": False, "stage": "bad_response"}
            body = json.loads(bytes(buf).decode("utf-8", "replace"))
        return _classify(resp.status_code, body)
    except requests.exceptions.Timeout:
        return {"ok": False, "stage": "timeout"}
    except ValueError:                                   # 响应不是合法 JSON
        return {"ok": False, "stage": "bad_response"}
    except (requests.exceptions.RequestException, OSError) as e:
        # detail 只给异常类名 —— requests 的异常消息里带完整请求 URL（= 带 token）
        return {"ok": False, "stage": "network", "detail": type(e).__name__}
    finally:
        sess.close()


def test_token(token):
    """§4.1 端点的完整结果：成功时附打码 token（`****` + 末 4 位）。"""
    out = getme(token)
    return dict(out, token_masked=redact.mask(token)) if out.get("ok") else out


# ---------------------------------------------------------------- A 段 · 只读校验（§4.2）

def templates():
    """§4.4：`channels/` 下的目录名即可选值。目录读不到是**正常态**，回空列表不是错误。"""
    try:
        names = sorted(n for n in os.listdir(channels_dir())
                       if not n.startswith(".")
                       and os.path.isdir(os.path.join(channels_dir(), n)))
    except OSError:
        return []
    labels = {b["id"]: b["display_name"] for b in _registry().bots()}
    return [{"id": n, "display_name": "空白模板" if n == BLANK_TEMPLATE else labels.get(n, n)}
            for n in names]


def _need(body, name):
    """A4 裁决：键缺失、空串、纯空白一律按「缺必填」→ `bad_body`，`detail` 点名字段。
    非空但不合形状才是 `bad_field`（用户先看到真正该改的东西）。"""
    if name not in body:
        raise HubError(400, "bad_body", "缺少必填字段：%s" % name)
    value = body[name]
    if isinstance(value, str) and not value.strip():
        raise HubError(400, "bad_body", "必填字段不能为空：%s" % name)
    return value


def _is_int(value):
    """bool 是 int 的子类，`True` 当端口用会一路走到 bind —— 这里必须把它挡掉。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _bad(why):
    raise HubError(400, "bad_field", why)


def _check_free(bot_id):
    """同名判定看两处：`configs/<id>.yml` 与 `channels/<id>/`。

    只看 yml 会在频道目录已存在时把 `shutil.move` 变成"搬进人家目录里"
    （`channels/<id>/<id>/`），而那个目录里可能是用户手工建的另一个 bot。
    """
    if os.path.exists(os.path.join(configs_dir(), bot_id + ".yml")):
        raise HubError(409, "bot_exists", "已经有一个叫 %s 的 bot 了" % bot_id)
    if os.path.exists(os.path.join(channels_dir(), bot_id)):
        raise HubError(409, "bot_exists", "频道目录 %s 已存在，换个名字或先手动清理" % bot_id)


def _listening(port):
    """本机是否已有进程在监听。R6-4：这与 bot 存在与否无关，所以它不是 `bot_exists` ——
    回 `bot_exists` 会让用户去找一个根本不存在的同名 bot。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _check_port(port, skip=None):
    """端口占用两问：注册表里有没有别人占（含派生值）、本机是不是已经有人在听。"""
    for bot in _registry().bots():
        if bot["port"] == port and bot["id"] != skip:
            raise HubError(409, "bot_exists", "端口 %d 已被 %s 占用" % (port, bot["id"]))
    if skip is None and _listening(port):
        raise HubError(409, "port_in_use", "端口 %d 已被本机其它进程监听" % port)


def _persona(body):
    """人设来源二选一，返回要写进 `CLAUDE.md` 的正文。"""
    tmpl, text = body.get("persona_template"), body.get("persona_text")
    has_tmpl = isinstance(tmpl, str) and bool(tmpl.strip())
    has_text = isinstance(text, str) and bool(text.strip())
    if has_tmpl == has_text:
        _bad("人设来源二选一：persona_template 或 persona_text，给一个")
    if has_text:
        if len(text.encode("utf-8")) > MAX_PERSONA:
            raise HubError(413, "payload_too_large", "人设正文超过 64 KiB")
        return text
    if tmpl not in {t["id"] for t in templates()}:
        # 只认扫出来的目录名，`../` 这类穿越串自然落不进集合里
        _bad("没有这个人设模板：%s" % tmpl[:64])
    try:
        with open(os.path.join(channels_dir(), tmpl, "CLAUDE.md"), encoding="utf-8") as f:
            return f.read(MAX_PERSONA)
    except OSError:
        return ""            # 模板目录在但没有 CLAUDE.md（空白模板就是这样）→ 空人设，不是错误


def _telegram_ids():
    """既有各 bot 的 telegram id（A7）：`channels/<bot>/.env` 里 token 的 `:` 前数字段。

    读到的整串只在内存里过一遍正则，函数出去时只剩数字段 ——
    绝不进响应/日志/异常文本。`.env` 缺失或形状不符 → 跳过该 bot 继续，不报错。
    """
    out = {}
    for bot in _registry().bots():
        try:
            with open(os.path.join(channels_dir(), bot["id"], ".env"),
                      encoding="utf-8", errors="ignore") as f:
                hit = ENV_TOKEN_RE.search(f.read(MAX_BODY))
        except OSError:
            continue
        if hit:
            out[bot["id"]] = int(hit.group(1))
    return out


def _validate(body):
    """A 段的纯校验部分（不含 getMe 与 token 比对），顺序照 §4.2 的表逐条来。

    **纯输入错优先于冲突错**：先把用户自己能改的挑干净，再报"跟别人撞了"。
    """
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    fields = {k: _need(body, k) for k in REQUIRED_FIELDS}
    bot_id, port, chat_id = fields["bot_id"], fields["dispatcher_port"], fields["chat_id"]
    if not isinstance(bot_id, str) or not BOT_ID_RE.match(bot_id):
        _bad("bot_id 只能是小写字母开头的 2-32 位小写字母/数字/下划线")
    _check_free(bot_id)
    if not _is_int(port) or not 1024 <= port <= 65535:
        _bad("dispatcher_port 必须是 1024-65535 的整数")
    _check_port(port)
    name = body.get("display_name")
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_DISPLAY_NAME
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in name)):
        # R8-1：它会进 yml、进页面渲染、可能进 plist 的 Label，控制字符一路带进去必炸
        _bad("display_name 必须是 1-64 字的字符串，且不含控制字符")
    if not _is_int(chat_id):
        _bad("chat_id 必须是整数")
    return {"bot_id": bot_id, "token": fields["telegram_token"], "port": port,
            "chat_id": chat_id, "display_name": name, "persona": _persona(body)}


def _check_token_free(bot_id, telegram_id):
    """R1：同一个 Telegram bot 被两个 dispatcher 同时 long-poll，Telegram 回 409，
    **用户消息静默丢一半**，日志里只有零星 409 —— 极难定位，必须在落盘前拦住。"""
    if telegram_id is None:
        return
    for other, known in _telegram_ids().items():
        if known == telegram_id and other != bot_id:
            raise HubError(409, "token_in_use", "这个 Telegram bot 已经被 %s 用着了" % other)


# ---------------------------------------------------------------- B 段 · 落盘（§4.2）

def _create_file(path, text, mode=0o600):
    """`O_CREAT|O_EXCL|O_WRONLY` 一次建成：没有"先建再 chmod"的 0644 窗口，
    也顺带挡住同名并发创建（R4）。"""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))


def _unlink(path):
    with contextlib.suppress(OSError):
        os.unlink(path)


def _rmdir(path):
    # 只删空目录：里面若有别人的东西就留着。回滚里绝不 rm -rf（R8-3）
    with contextlib.suppress(OSError):
        os.rmdir(path)


def _rollback(made):
    """按创建清单逆序精确删除。回滚过程里再抛异常会把真正的错因盖掉，所以全程吞。"""
    for is_tree, path in reversed(made):
        if not is_tree:
            _unlink(path)
            continue
        for rel in TREE_FILES:
            _unlink(os.path.join(path, rel))
        for rel in TREE_DIRS:
            _rmdir(os.path.join(path, rel))
        _rmdir(path)


def _fix_modes(target):
    """F5：`shutil.move` 跨文件系统会走 copy 路径、权限取决于实现，
    而 macOS 的 TMPDIR 在 `/var/folders/...`，与 `~/.claude/` 大概率不同挂载点 —— 就位后逐个复核。"""
    os.chmod(target, 0o700)
    for rel in TREE_DIRS:
        os.chmod(os.path.join(target, rel), 0o700)
    for rel in TREE_FILES:
        os.chmod(os.path.join(target, rel), 0o600)


def _yaml_str(value):
    """写进 yml 的字符串字面量。`json.dumps` 出的双引号串是 YAML 的合法子集：
    emoji / 引号 / 换行全都不用自己转义，也就没有"手写转义漏一种"的洞。"""
    return json.dumps(value, ensure_ascii=False)


def _channel_path_literal(target):
    """`bot_channel_path`：能收进 `~` 就收（与既有 `configs/*.yml` 同形），否则给绝对路径。"""
    home = os.path.expanduser("~")
    if target == home or target.startswith(home + os.sep):
        return "~" + target[len(home):]
    return target


def _new_namespace():
    """uuid4 + 全表去重。namespace 撞车 = 两个 bot 共用会话历史，所以去重是契约的一部分
    （撞的概率是 0，这个 while 只是把契约写进代码里）。"""
    used = {b["namespace_uuid"] for b in _registry().bots()}
    ns = str(uuid.uuid4())
    while ns in used:
        ns = str(uuid.uuid4())
    return ns


def _render_yml(fields, target):
    """`configs/<bot>.yml`：**不含 token**（F5），只有注册表要的那几个键。"""
    return ("id: %s\n"
            "display_name: %s\n"
            "bot_channel_path: %s\n"
            "chat_id: %d\n"
            "dispatcher_port: %d\n"
            "namespace_uuid: %s\n"
            % (fields["bot_id"], _yaml_str(fields["display_name"]),
               _yaml_str(_channel_path_literal(target)),
               fields["chat_id"], fields["port"], _new_namespace()))


def lay_down(fields):
    """B 段：tmp 里建好整棵树 → 一次 `move` 就位 → 写 yml → 复查端口。

    任一步失败：按精确清单逆序删干净，磁盘回到操作前（可断言 yml 不在、频道目录不在、
    注册表查不到它），然后把异常原样抛给调用方定码。
    """
    bot_id = fields["bot_id"]
    target = os.path.join(channels_dir(), bot_id)
    yml = os.path.join(configs_dir(), bot_id + ".yml")
    made = []                                  # 精确回滚清单：(是否整棵树, 路径)，按创建顺序
    tmp = tempfile.mkdtemp()                   # mkdtemp 本身就是 0700
    try:
        src = os.path.join(tmp, bot_id)
        os.mkdir(src, 0o700)
        for rel in TREE_DIRS:
            os.mkdir(os.path.join(src, rel), 0o700)
        # token 的唯一落点（F5）。yml 里一个字都没有它
        _create_file(os.path.join(src, ".env"), "TELEGRAM_BOT_TOKEN=%s\n" % fields["token"])
        _create_file(os.path.join(src, "access.json"),
                     json.dumps({"allowFrom": [fields["chat_id"]]}, ensure_ascii=False) + "\n")
        _create_file(os.path.join(src, "CLAUDE.md"), fields["persona"])
        _create_file(os.path.join(src, "memory", "MEMORY.md"), MEMORY_SEED)
        shutil.move(src, target)
        made.append((True, target))
        _fix_modes(target)
        _create_file(yml, _render_yml(fields, target))
        made.append((False, yml))
        _check_port(fields["port"], skip=bot_id)   # R4：A 段那次 bind 试探到这里已经有窗口
    except BaseException:
        _rollback(made)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)     # 只删自己 mkdtemp 出来的那个
    return target


# ---------------------------------------------------------------- C 段 · 注册 + 拉起（§4.2）

def _step(name, state, detail=None, **extra):
    step = {"name": name, "state": state}
    if detail:
        step["detail"] = detail
    step.update(extra)
    return step


def _plutil_ok(path):
    """有 `plutil` 就 lint（install.sh 的第 3 步），没有就不拦 —— 它只在 macOS 上有。
    工具本身跑不起来也不判 plist 坏：那是环境问题，不是文件问题。"""
    exe = shutil.which("plutil")
    if not exe:
        return True
    try:
        return subprocess.run([exe, "-lint", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def _launchctl(label, plist, hint):
    """先 `bootout` 再 `bootstrap`（幂等，照抄 install.sh）。SSH 里没有图形会话时
    bootstrap 必拒 —— plist 已经写好了，把那条命令给用户，让他图形登录后自己补。"""
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
        code = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, plist],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return _step("register", "failed", type(e).__name__, manual_hint=hint)
    if code != 0:
        return _step("register", "failed", "launchctl_bootstrap_failed", manual_hint=hint)
    return _step("register", "done")


def _register_launchd(fields):
    """macOS：渲染 self-initiate plist → lint → 就位 →（可选）launchctl。

    同目录 `mkstemp` + `os.replace`：跨文件系统的 EXDEV 和半截文件都不可能出现，
    目录不可写时也当场就是 PermissionError（正是我们要的 `failed`）。
    """
    label = "com.example.%s-self-initiate" % fields["bot_id"]
    plist = os.path.join(launchagents_dir(), label + ".plist")
    hint = "launchctl bootstrap gui/$UID %s" % plist
    try:
        with open(os.path.join(REPO_ROOT, "plist-templates", "self-initiate.plist.tmpl"),
                  encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return _step("register", "skipped", type(e).__name__, manual_hint=hint)
    text = (text.replace("{REPO_DIR}", REPO_ROOT)
                .replace("{BOT_ID}", fields["bot_id"])
                .replace("{CHAT_ID}", str(fields["chat_id"])))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=launchagents_dir(), prefix="." + label, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
        if not _plutil_ok(tmp):
            return _step("register", "failed", "plist_lint_failed", manual_hint=hint)
        os.chmod(tmp, 0o644)
        os.replace(tmp, plist)
        tmp = None
    except OSError as e:
        return _step("register", "failed", type(e).__name__, manual_hint=hint)
    finally:
        if tmp:
            _unlink(tmp)
    if os.environ.get("HUB_LAUNCHAGENTS_DIR"):
        # §7：目录被注入到非默认值 = 测试态，不实调 launchctl（不许动机主的 gui domain）
        return _step("register", "done", "skipped_launchctl")
    return _launchctl(label, plist, hint)


def _register_task(fields):
    """Windows：`register-tasks.ps1` 已按 D1-⑥ 遍历注册表，跑一次就把新 bot 带上，
    不必写单 bot 注册逻辑（它先 Unregister 再注册，天然幂等）。"""
    script = os.path.join(REPO_ROOT, "windows", "register-tasks.ps1")
    hint = "powershell -NoProfile -ExecutionPolicy Bypass -File %s" % script
    if not os.path.isfile(script):
        return _step("register", "skipped", "register_tasks_missing", manual_hint=hint)
    try:
        code = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", script], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=120).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return _step("register", "failed", type(e).__name__, manual_hint=hint)
    if code != 0:
        return _step("register", "failed", "register_tasks_failed", manual_hint=hint)
    return _step("register", "done")


def register(fields):
    """C 段第 4 步：注册**可选的** self-initiate 常驻单元。

    dispatcher 本身不需要 per-bot 常驻（D3）：`restart-bots.sh` 遍历注册表，
    新 bot 一落盘，既有的全局 autostart 下次触发就带上它。
    """
    if sys.platform == "darwin":
        return _register_launchd(fields)
    if os.name == "nt":
        return _register_task(fields)
    # B3：非 macOS/Windows → `skipped` + 确切命令走 207。**不打 TODO 到 stdout** ——
    # 门户是 web 进程，用户根本看不到，那等于"看起来做了"的静默跳过。
    # install.sh 在这条路径上也是同一句提示（本仓库不提供 systemd 单元模板）。
    return _step("register", "skipped", "no_launchd",
                 manual_hint="本机没有 launchd/计划任务；想让它主动开口，自己挂个定时器跑："
                             "python3 %s/scripts/self_initiate.py %s %d"
                             % (REPO_ROOT, fields["bot_id"], fields["chat_id"]))


def _script_reads_registry():
    """F4 自检：`grep -q bots_registry <脚本>`。**文件不存在 = 自检不通过**（§7）。"""
    try:
        with open(restart_script(), encoding="utf-8", errors="ignore") as f:
            return "bots_registry" in f.read(1024 * 1024)
    except OSError:
        return False


def wait_alive(port, seconds=PROBE_SECONDS):
    """F4：成功判定改探活，不看退出码。轮询 `POST /status`，最多 30 秒。

    复用 `bots_client._probe_one`（同一套 `POST /status` + 绕开系统代理的 opener），
    不另写一份探活 —— 两份探活迟早在超时/方法上分叉。
    """
    deadline = time.monotonic() + seconds
    while True:
        if bots_client._probe_one(port) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def restart(fields, jobs):
    """C 段 5-7 步：脚本版本自检 → 触发重启 → 探活判成。"""
    if not _script_reads_registry():
        # 老副本硬编码 BOTS=(...)：它会"正常重启了别人、退出码 0"，据此回 201 就是撒谎
        return _step("restart", "skipped", "restart_script_outdated", manual_hint=HINT_OUTDATED)
    argv, available = bots_client.restart_plan()
    if not available:
        return _step("restart", "skipped", "restart_script_missing", manual_hint=HINT_OUTDATED)
    acc = jobs.start(argv)                     # 复用一期 §6.2 的异步 job 与 HUB_RESTART_CMD 接缝
    if acc.job_id is None:
        return _step("restart", "failed", "restart_in_progress", manual_hint=HINT_RESTART)
    if not wait_alive(fields["port"]):
        return _step("restart", "failed", "probe_timeout", job_id=acc.job_id,
                     manual_hint=HINT_RESTART)
    return _step("restart", "done", job_id=acc.job_id)


# ---------------------------------------------------------------- 三段总装（§4.3）

def _respond(fields, probe, steps):
    """全成功 201；C 段有任一步没 done → `207 bot_created_partial` + 能自己补的确切命令。

    207 不删 B 段产物：文件已经是对的、bot 手动跑得起来，把它删掉等于用户白填一遍表单。
    """
    body = {"ok": True, "bot_id": fields["bot_id"], "username": probe.get("username"),
            "steps": steps, "restart_hint": dict(RESTART_FLAGS)}
    stuck = [s for s in steps if s["state"] != "done"]
    if not stuck:
        return body, 201
    hints = [s["manual_hint"] for s in stuck if s.get("manual_hint")]
    body.update(ok=False, error="bot_created_partial",
                manual_hint=" ；".join(hints) or HINT_RESTART)
    return body, 207


def create(body, jobs):
    """三段全流程。返回 `(响应体, HTTP 码)`。

    A 段的失败一律以 `HubError` 抛出（零副作用）；getMe 的业务失败**不是错误码** ——
    回 `200 {"ok": false, "stage": ...}` 且不落盘（§4.1 同款风味）。
    """
    fields = _validate(body)
    probe = getme(fields["token"])
    if not probe.get("ok"):
        return probe, 200
    _check_token_free(fields["bot_id"], probe.get("bot_id"))
    steps = [_step("validate", "done"), _step("getme", "done")]
    try:
        lay_down(fields)
    except HubError:
        raise                              # 端口复查这类：已回滚干净，照原码抛
    except Exception as e:
        # detail 只给异常类名：路径与 token 都不出站
        raise HubError(500, "internal", type(e).__name__)
    steps.append(_step("files", "done"))
    steps.append(register(fields))
    steps.append(restart(fields, jobs))
    return _respond(fields, probe, steps)
