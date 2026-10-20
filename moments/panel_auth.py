"""8767 面板登录门：口令哈希 + 可撤销会话 + 防爆破（INTERFACE §2.1）。

标准库 + werkzeug.security；凭据只落 ``~/.claude/secrets/``。
"""
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

COOKIE_NAME = "cb_provider"
COOKIE_MAX_AGE = 2592000  # 30 天
SECRET_PATH = os.path.expanduser("~/.claude/secrets/moments-panel.env")
SESSIONS_PATH = os.path.expanduser("~/.claude/secrets/moments-panel-sessions.json")
TRUSTED_PROXY_ADDRS = {"127.0.0.1", "::1"}

MAX_FAILURES = 5
FAILURE_WINDOW = 300
BLOCK_CAP = 300
SESSION_MAX_AGE = 2592000
_LOGIN_SEM = threading.Semaphore(4)

_SID_RE = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.Lock()
_fail: dict[str, dict] = {}


# ── 凭据文件 ───────────────────────────────────────────────────────────────

def _atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
    """E-05：先写同目录临时文件、chmod 到 0600，再 os.replace 原子替换。

    直接 O_TRUNC 写目标文件，写一半崩会留下空/半截的口令或会话文件。临时文件以 ``.tmp``
    结尾，失败时清理后原样抛出（原文件不被触碰）。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_secret() -> dict:
    try:
        with open(SECRET_PATH, encoding="utf-8") as f:
            out = {}
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
            return out
    except OSError:
        return {}


def _cookie_secret() -> str:
    return _read_secret().get("MOMENTS_PANEL_COOKIE_SECRET", "")


def secret_ready() -> bool:
    s = _read_secret()
    return bool(s.get("MOMENTS_PANEL_PASSWORD_HASH") and s.get("MOMENTS_PANEL_COOKIE_SECRET"))


def verify_password(pw: str) -> bool:
    h = _read_secret().get("MOMENTS_PANEL_PASSWORD_HASH", "")
    if not h:
        return False
    return check_password_hash(h, pw or "")


def set_password(pw: str) -> None:
    """写口令哈希 + 新随机 COOKIE_SECRET；同时清空 sessions（旧 cookie 全失效）。"""
    body = ("MOMENTS_PANEL_PASSWORD_HASH=%s\nMOMENTS_PANEL_COOKIE_SECRET=%s\n"
            % (generate_password_hash(pw), secrets.token_hex(32)))
    _atomic_write_text(SECRET_PATH, body, 0o600)
    with _lock:
        _save_sessions({})


# ── 会话 ───────────────────────────────────────────────────────────────────

def _load_sessions() -> dict:
    try:
        with open(SESSIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}  # 文件损坏 → 视为空（fail-closed，全部要求重登）
    v1 = data.get("v1") if isinstance(data, dict) else None
    return v1 if isinstance(v1, dict) else {}


def _save_sessions(v1: dict) -> None:
    now = time.time()
    kept = {}
    for sid, rec in v1.items():
        if not isinstance(rec, dict):
            continue
        created = rec.get("created_at") or 0
        if now - created > SESSION_MAX_AGE:
            continue  # 清 30 天以上
        if rec.get("revoked") and now - (rec.get("last_seen") or 0) > 7 * 86400:
            continue  # 清撤销超 7 天
        kept[sid] = rec
    _atomic_write_text(SESSIONS_PATH,
                       json.dumps({"v1": kept}, ensure_ascii=False, indent=2), 0o600)


def new_session() -> str:
    sid = secrets.token_hex(16)
    now = int(time.time())
    with _lock:
        v1 = _load_sessions()
        v1[sid] = {"created_at": now, "last_seen": now, "revoked": False}
        _save_sessions(v1)
    return sid


def session_valid(sid: str) -> bool:
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return False
    rec = _load_sessions().get(sid)
    if not isinstance(rec, dict) or rec.get("revoked"):
        return False
    return time.time() - (rec.get("created_at") or 0) <= SESSION_MAX_AGE


def revoke(sid: str) -> None:
    with _lock:
        v1 = _load_sessions()
        if sid in v1:
            v1[sid]["revoked"] = True
            _save_sessions(v1)


def revoke_all() -> int:
    with _lock:
        v1 = _load_sessions()
        n = 0
        for sid, rec in v1.items():
            if not rec.get("revoked"):
                rec["revoked"] = True
                n += 1
        _save_sessions(v1)
        return n


# ── cookie ─────────────────────────────────────────────────────────────────

def _sign(sid: str) -> str:
    return hmac.new(_cookie_secret().encode("utf-8"), sid.encode("utf-8"), hashlib.sha256).hexdigest()


def make_cookie(sid: str) -> str:
    return "v1.%s.%s" % (sid, _sign(sid))


def parse_cookie(v: str | None) -> str | None:
    if not isinstance(v, str):
        return None
    parts = v.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not _SID_RE.match(parts[1]):
        return None
    secret = _cookie_secret()
    if not secret:
        return None
    if not hmac.compare_digest(_sign(parts[1]), parts[2]):
        return None
    return parts[1]


# ── 防爆破 ─────────────────────────────────────────────────────────────────

def client_key(request) -> str:
    """限速键：仅当来源是可信代理（环回）时才采信 CF-Connecting-IP。"""
    remote = getattr(request, "remote_addr", "") or ""
    headers = getattr(request, "headers", None)
    if remote in TRUSTED_PROXY_ADDRS and headers is not None:
        cf = headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
    return remote


def backoff_seconds(failures: int) -> int:
    """0,0,0,0,1,2,4,8,… 封顶 300（第 5 次起封锁）。"""
    if failures < MAX_FAILURES:
        return 0
    return min(BLOCK_CAP, 2 ** (failures - MAX_FAILURES))


def _prune_failures_locked(now: float) -> None:
    """清掉「窗口已过且不在封锁中」的条目，避免 ``_fail`` 无界增长。

    条目原先只在**成功登录**时 ``clear_failures`` 清理 → 从未登录成功的来源会永久累积
    （一个来源一条）。判据与 ``register_failure`` 的窗口口径一致：窗口外且不再封锁。
    """
    for ip in [k for k, r in _fail.items()
               if now - max(r.get("last", 0), r.get("until", 0)) > FAILURE_WINDOW
               and now >= r.get("until", 0)]:
        _fail.pop(ip, None)


def register_failure(ip: str) -> int:
    now = time.time()
    with _lock:
        _prune_failures_locked(now)
        rec = _fail.get(ip) or {"n": 0, "last": 0.0, "until": 0.0}
        # E-04：窗口以上次失败与上次封锁结束时间的**较晚者**为起点。若只按 last 计，攻击者
        # 每次等封锁（含封顶 300s）一结束再打，now-last 一过 300 就被整份清零、永远只吃最小退避。
        if now - max(rec["last"], rec.get("until", 0.0)) > FAILURE_WINDOW:
            rec["n"] = 0  # 窗口外的旧失败不计
        rec["n"] += 1
        rec["last"] = now
        b = backoff_seconds(rec["n"])
        if b:
            rec["until"] = now + b
        _fail[ip] = rec
        return rec["n"]


def is_blocked(ip: str) -> int:
    rec = _fail.get(ip)
    if not rec:
        return 0
    rem = rec["until"] - time.time()
    if rem <= 0:
        return 0
    return max(1, math.ceil(rem))


def clear_failures(ip: str) -> None:
    with _lock:
        _fail.pop(ip, None)


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    if "--show-status" in argv:
        ready = secret_ready()
        print("口令：%s" % ("已配置" if ready else "未配置"))
        print("凭据路径：%s" % SECRET_PATH)
        print("活跃会话数：%d" % sum(1 for r in _load_sessions().values()
                                     if not r.get("revoked")))
        return 0
    if "--set-password" in argv:
        p1 = getpass.getpass("新口令：")
        p2 = getpass.getpass("再输一次：")
        if not p1 or p1 != p2:
            sys.stderr.write("两次输入不一致或为空，未修改\n")
            return 1
        set_password(p1)
        print("已设置口令；旧会话全部失效")
        return 0
    sys.stderr.write("usage: python3 -m moments.panel_auth --set-password|--show-status\n")
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
