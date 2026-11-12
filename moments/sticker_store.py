# -*- coding: utf-8 -*-
"""表情库：读、校验、添加、改、移除、按链接下载。只用标准库与仓内的 platform_lock。

表情库由 dispatcher 与管理台共用：目录文件（缺省 ~/.claude/channels/_shared/stickers.json）与图片根（缺省
~/resource/stickers，口径同 scripts/install_stickers.py）。两处路径每次调用时从环境变量 STICKERS_JSON / STICKERS_DIR
现取；STICKERS_JSON 只作测试接缝，读取方只认缺省位置。

读这个库的各方按 label / path 挑图、按条目先后现算序号，所以：新条目只追加在该档末尾；改标签、改分类原地改；
只有移除会让后面条目的序号变。目录文件的写法：json.dumps(indent=2, ensure_ascii=False)，结尾不加换行，
权限 0644；顶层的其他键、条目里多出来的键、不完整的条目都原样保留。

所有改动走 _mutate：加锁 → 锁内重读 → 改 → 整体校验 → 备份 → 同目录临时文件 + fsync + os.replace。任何一步失败
原文件不变、不留临时文件。添加时图片先于目录文件落盘，目录文件那步失败就删掉刚写的这一张。移除只把图片挪进
<图片根>/.removed/<档>/，绝不删除。日志里只记档与文件名，不记标签、图片内容、完整链接。
"""
import contextlib
import datetime
import errno
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import platform_lock

_log = logging.getLogger(__name__)

GROUPS = ("sfw", "nsfw")
MAX_BYTES = 10 * 1024 * 1024
TYPES = ("gif", "jpg", "png", "webp")
MIMETYPES = {"gif": "image/gif", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
MAX_TEXT = 60
BACKUP_KEEP = 2   # 备份只留最近 2 份
BACKUP_DIR = ".stickers-backups"
REMOVED_DIR = ".removed"
LOCK_WAIT_SEC = 10.0
LOCK_RETRY_SEC = 0.05
# Windows 上目标文件正被别的进程打开（杀毒、索引）时 os.replace 报 PermissionError：锁内短重试，仍失败才报错
_REPLACE_TRIES = 10
_REPLACE_RETRY_SEC = 0.05
_CHUNK = 64 * 1024
IMG_PREFIX = "/console/stickers/img/"
# 图片路由与改、移除的文件名规则；用 fullmatch：`$` 会放过结尾的换行
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PREFIX_RE = re.compile(r"(\d+)_")
# 标签、分类里不许的字符：控制字符、代理对残片（写不成 UTF-8）、行分隔符与段分隔符。
# 不拦 Cf（格式字符）：表情符号里的零宽连接符就是 Cf。
_BAD_CHAR_CATEGORIES = frozenset({"Cc", "Cs", "Zl", "Zp"})
_HEIC_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"})
# 不支持硬链接的文件系统给的错误：退回"先查在不在、再改名"。Windows 上 FAT/exFAT、部分网络盘回的是
# winerror 1/50/120（与 platform_lock 的"不支持"同口径），对应的 errno 不在前一组里
_NO_LINK_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK})
_NO_LINK_WINERRORS = frozenset({1, 50, 120})
_PATH_SEP_RE = re.compile(r"[/\\]")

PAGE_LINK_MESSAGE = "这个链接不是图片本身，请复制图片的地址"
HEIC_MESSAGE = "HEIC 不支持，请先转成 JPG"
TYPES_MESSAGE = "只支持 GIF、JPG、PNG、WEBP 格式的图片"

# ── 按链接下载 ───────────────────────────────────────────────────────────────
MAX_URL = 2048
MAX_REDIRECTS = 3
IO_TIMEOUT = 15.0       # 连接、每次读取
TOTAL_TIMEOUT = 45.0    # 整体
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HEX_OR_NUM_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class StickerError(Exception):
    """code 取接口错误码表里的值；duplicate 时 existing = {"group", "label"}。message 给人看。"""

    def __init__(self, code: str, message: str, existing=None):
        super().__init__(message)
        self.code, self.message, self.existing = code, message, existing


# ── 配置 ─────────────────────────────────────────────────────────────────────

def json_path() -> str:
    return os.path.abspath(os.path.expanduser(
        os.environ.get("STICKERS_JSON") or "~/.claude/channels/_shared/stickers.json"))


def stickers_dir() -> str:
    """图片根。口径同 dispatcher 与 scripts/install_stickers.py：只展开开头的 ``~`` 与 ``~/``（``~user`` 不认），
    展开后必须是绝对路径（Windows 上不带盘符的 ``\\stickers`` 不算）；不合规 → library_broken，什么都不写。"""
    raw = os.environ.get("STICKERS_DIR") or "~/resource/stickers"
    expanded = os.path.expanduser(raw)
    if expanded != raw and raw[1:2] not in ("", "/"):   # 被展开的是 ~user 或 ~\ 这类写法
        expanded = raw
    if not Path(expanded).is_absolute():
        raise _broken_config()
    return os.path.abspath(expanded)


def _broken_config() -> StickerError:
    return StickerError("library_broken", "表情库图片目录配置不对：STICKERS_DIR 必须是绝对路径或以 ~/ 开头，"
                        "已停止一切写入，请先改好它")


def _real_group_dir(group: str) -> str:
    return os.path.realpath(os.path.join(stickers_dir(), group))


def _directly_in(path: str, real_dir: str) -> bool:
    """真实路径是否就在 real_dir 里（不含子目录）：挡住 ..、符号链接、绝对路径指到别处。
    两边都 normcase：Windows 上大小写或分隔符写法不同不算档外；POSIX 上 normcase 什么也不做。"""
    return os.path.normcase(os.path.dirname(os.path.realpath(path))) == os.path.normcase(real_dir)


# ── 读 ───────────────────────────────────────────────────────────────────────

def _broken(reason: str) -> StickerError:
    return StickerError("library_broken", "表情库文件坏了（%s），已停止一切写入，请先修好它" % reason)


def _parse(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _broken("不是 UTF-8 文本")
    try:
        lib = json.loads(text)
    except json.JSONDecodeError as e:
        raise _broken("不是合法的 JSON，第 %d 行第 %d 列" % (e.lineno, e.colno))
    except (ValueError, RecursionError):
        raise _broken("不是合法的 JSON")
    if not isinstance(lib, dict):
        raise _broken("顶层不是对象")
    for g in GROUPS:
        if not isinstance(lib.get(g), list):
            raise _broken("%s 不是列表" % g)
    return lib


def _load():
    """返回 (原始字节或 None, 库对象, 文件权限)。文件不存在 → (None, 空库, 0o644)。
    图片根配置不合规时先报 library_broken：列表、添加、改、删都靠它判断条目在不在档目录里。"""
    stickers_dir()
    path = json_path()
    try:
        with open(path, "rb") as f:
            mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
            raw = f.read()
    except FileNotFoundError:
        return None, {"sfw": [], "nsfw": []}, 0o644
    except OSError:
        _log.exception("表情库：目录文件读不出来")
        raise StickerError("internal", "表情库文件读不出来")
    return raw, _parse(raw), mode


def _complete(entry) -> bool:
    """读这个库的各方只认带 label 与 path 的对象；其余不进列表、只计数、写入时原样保留。"""
    return (isinstance(entry, dict) and isinstance(entry.get("label"), str)
            and isinstance(entry.get("path"), str) and entry["path"] != "")


def _file_of(entry: dict) -> str:
    """path 的最后一段；/ 与 \\ 都当分隔符（Windows 上随包安装写的是反斜杠路径）。"""
    return _PATH_SEP_RE.split(entry["path"])[-1]


def item_of(group: str, entry: dict) -> dict:
    """条目 → 接口里的 item。"""
    name = _file_of(entry)
    size, url, cat = entry.get("size"), entry.get("url"), entry.get("category")
    return {
        "id": group + "/" + name,
        "group": group,
        "label": entry["label"],
        "category": cat if group == "nsfw" and isinstance(cat, str) else None,
        "url": url if isinstance(url, str) else "",
        "file": name,
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "missing": not os.path.isfile(entry["path"]),
        "img": IMG_PREFIX + group + "/" + urllib.parse.quote(name, safe=""),
    }


def library() -> dict:
    """列表接口的数据部分：groups / categories / incomplete / limits。库坏了 → library_broken。"""
    _raw, lib, _mode = _load()
    groups, incomplete, categories, seen = {}, {}, [], set()
    for g in GROUPS:
        groups[g] = [item_of(g, e) for e in lib[g] if _complete(e)]
        incomplete[g] = sum(1 for e in lib[g] if not _complete(e))
    for e in lib["nsfw"]:
        cat = e.get("category") if _complete(e) else None
        if isinstance(cat, str) and cat and cat not in seen:   # 按首次出现的先后
            seen.add(cat)
            categories.append(cat)
    return {"groups": groups, "categories": categories, "incomplete": incomplete,
            "limits": {"max_bytes": MAX_BYTES, "types": list(TYPES)}}


# ── 写 ───────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked(path: str):
    """同目录 ``stickers.json.lock`` 上的跨平台排他锁（platform_lock）。锁文件不删（删了会与别人的锁错开）。"""
    try:
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        _log.exception("表情库：拿不到锁文件")
        raise StickerError("internal", "拿不到表情库的锁")
    try:
        try:
            got = platform_lock.acquire(fd, deadline=time.monotonic() + LOCK_WAIT_SEC, poll=LOCK_RETRY_SEC)
        except platform_lock.LockUnsupported:
            _log.error("表情库：文件系统不支持文件锁")
            raise StickerError("internal", "拿不到表情库的锁")
        if not got:
            raise StickerError("internal", "表情库正被别的操作占用，请稍后再试")
        try:
            yield
        finally:
            platform_lock.release(fd)
    finally:
        os.close(fd)


def _dump(lib) -> bytes:
    """整体校验：结构仍合规、能写成 UTF-8、写出来的文字读回再写一遍逐字相同。"""
    if not isinstance(lib, dict) or any(not isinstance(lib.get(g), list) for g in GROUPS):
        raise StickerError("internal", "改动后的表情库结构不对，文件没有改动")
    text = json.dumps(lib, indent=2, ensure_ascii=False)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise StickerError("internal", "表情库里有存不下的字符，文件没有改动")
    if json.dumps(json.loads(text), indent=2, ensure_ascii=False) != text:
        raise StickerError("internal", "改动后的表情库读回来对不上，文件没有改动")
    return data


def _backup(path: str, raw: bytes, mode: int) -> None:
    """改动前的文件存到 ``<目录>/.stickers-backups/<文件名>.<YYYYmmdd-HHMMSS-ffffff>``，只留最新 BACKUP_KEEP 份。"""
    d = os.path.join(os.path.dirname(path), BACKUP_DIR)
    os.makedirs(d, exist_ok=True)
    name = os.path.basename(path)
    base = os.path.join(d, "%s.%s" % (name, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    bak, n = base, 0
    while True:
        try:
            fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
            bak = "%s.%d" % (base, n)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(bak, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(bak)   # 写了一半的备份不留
        raise
    pattern = re.compile(re.escape(name) + r"\.\d{8}-\d{6}-\d{6}(\.\d+)?")
    names = sorted(x for x in os.listdir(d) if pattern.fullmatch(x))
    for old in names[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(d, old))


def _replace(src: str, dst: str) -> None:
    """os.replace；只在 Windows 上对 PermissionError 短重试（调用方在库锁内），仍失败原样抛，原文件不变。"""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_TRIES - 1:
                raise
            time.sleep(_REPLACE_RETRY_SEC)


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".%s." % os.path.basename(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):   # 改名之后的事都不许再抛：目录文件已经换成新的了
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _mutate(change, after=None):
    """一切改动的唯一入口：加锁 → 锁内重读 → ``change(lib)`` → 整体校验 → 备份 → 原子写。

    ``after(结果)`` 在目录文件写好之后、仍在锁内调用（移除时挪图），它自己兜住一切失败，不许抛。
    库坏了 → library_broken，文件不动；任何一步的 OSError → internal，原文件不变、不留临时文件。
    """
    stickers_dir()   # 图片根配置不合规时连目录与锁文件都不建
    path = json_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _locked(path):
            raw, lib, mode = _load()
            result = change(lib)
            data = _dump(lib)
            if raw is not None:
                _backup(path, raw, mode)
            _write_atomic(path, data, mode)
            if after is not None:
                after(result)
            return result
    except OSError:
        _log.exception("表情库：写入失败")
        raise StickerError("internal", "表情库写不进去，文件没有改动")


# ── 添加 ─────────────────────────────────────────────────────────────────────

def _text(value, what: str) -> str:
    if not isinstance(value, str):
        raise StickerError("invalid", "%s必须是文字" % what)
    v = value.strip()
    if not v:
        raise StickerError("invalid", "%s不能是空的" % what)
    if len(v) > MAX_TEXT:
        raise StickerError("invalid", "%s最多 %d 个字" % (what, MAX_TEXT))
    if any(unicodedata.category(c) in _BAD_CHAR_CATEGORIES for c in v):
        raise StickerError("invalid", "%s里不能有换行或控制字符" % what)
    return v


def check_fields(group, label, category):
    """档、标签、分类的校验；返回 (档, 去首尾空白的标签, 分类或 None)。sfw 带了分类也不用。"""
    if not isinstance(group, str) or group not in GROUPS:
        raise StickerError("invalid", "档只能是 sfw 或 nsfw")
    label = _text(label, "标签")
    if group == "nsfw":
        if category is None:
            raise StickerError("invalid", "成人档要选一个分类")
        category = _text(category, "分类")
    else:
        category = None
    return group, label, category


def sniff(data: bytes):
    """按文件头判断真实类型：gif / png / jpg / webp，别的一律 None。"""
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _check_content(data: bytes, from_link: bool) -> str:
    if len(data) > MAX_BYTES:
        raise StickerError("too_large", "图片超过 10 MiB")
    ext = sniff(data)
    if ext:
        return ext
    if data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        raise StickerError("unsupported_type", HEIC_MESSAGE)
    head = data[:1024].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith(b"<"):
        if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head:
            raise StickerError("unsupported_type", "不支持 SVG，" + TYPES_MESSAGE)
        # 网页：贴链接时多半是复制了图片所在页面的地址；上传的文件里出现就是文字文件
        raise StickerError("unsupported_type", PAGE_LINK_MESSAGE if from_link else "这不是图片文件，" + TYPES_MESSAGE)
    raise StickerError("unsupported_type", TYPES_MESSAGE)


def _duplicate(group: str, entry: dict) -> StickerError:
    return StickerError("duplicate", "这张已经在表情库里了（%s 档，标签：%s）" % (group, entry["label"]),
                        existing={"group": group, "label": entry["label"]})


def _find_url_duplicate(lib: dict, url: str):
    for g in GROUPS:
        for e in lib[g]:
            if _complete(e) and isinstance(e.get("url"), str) and e["url"].strip() == url:
                return g, e
    return None


def _sha1_file(path: str):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _find_content_duplicate(lib: dict, digest: str, size: int):
    """两档里内容相同的图：先比大小，只对大小相同的算 sha1。只看真实路径就在档目录里的文件。"""
    for g in GROUPS:
        base = _real_group_dir(g)
        for e in lib[g]:
            if not _complete(e) or not _directly_in(e["path"], base):
                continue
            try:
                st = os.stat(e["path"])
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size == size and _sha1_file(e["path"]) == digest:
                return g, e
    return None


def _max_prefix(d: str) -> int:
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return -1
    best = -1
    for n in names:
        m = _PREFIX_RE.match(n)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _move_no_clobber(src: str, dst: str) -> None:
    """把 src 改名成 dst；dst 已存在 → FileExistsError，绝不覆盖任何文件。

    用"硬链接 + 删旧名"做不覆盖的改名；文件系统不支持硬链接时退回"先查在不在、再改名"（调用方都在库锁内）。
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _NO_LINK_ERRNOS and getattr(e, "winerror", None) not in _NO_LINK_WINERRORS:
            raise
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
        os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(dst)   # 撤回刚建的新名字：内容还在 src，一张图都不少
        raise


def _place_image(group: str, data: bytes, h: str, ext: str, taken: set) -> str:
    """先在档目录里写临时文件，再改名成 ``NNN_<h>.<ext>``；返回最终的绝对路径。

    NNN = 档目录里现有文件名前缀序号的最大值 + 1，至少 3 位；名字在磁盘上已有、或已被库里本档的条目用着，就继续加 1。
    """
    d = os.path.join(stickers_dir(), group)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sticker-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)   # mkstemp 建的是 0600；关文件后用 chmod（os.fchmod 在旧版 Windows Python 上没有）
        n = _max_prefix(d) + 1
        for _ in range(10000):
            name = "%03d_%s.%s" % (n, h, ext)
            final = os.path.join(d, name)
            if name not in taken and not os.path.lexists(final):
                try:
                    _move_no_clobber(tmp, final)
                    return final
                except FileExistsError:
                    pass
            n += 1
        raise StickerError("internal", "给新图片取不出文件名")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)   # 成功时临时名已不在；失败时清掉


def _new_entry(group, label, category, url, path, size) -> dict:
    """新条目的键顺序：sfw 是 url, label, path, size；nsfw 是 url, label, category, path, size。"""
    entry = {"url": url, "label": label}
    if group == "nsfw":
        entry["category"] = category
    entry["path"] = path
    entry["size"] = size
    return entry


def add(group, label, category, data: bytes, url: str = "") -> dict:
    """上传与链接共用的添加。url 为空 = 本地导入。返回新条目的 item。"""
    group, label, category = check_fields(group, label, category)
    url = url.strip() if isinstance(url, str) else ""
    ext = _check_content(data, from_link=bool(url))
    digest = hashlib.sha1(data).hexdigest()
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10] if url else digest[:10]
    placed = []

    def change(lib):
        dup = (_find_url_duplicate(lib, url) if url else None) or _find_content_duplicate(lib, digest, len(data))
        if dup:
            raise _duplicate(*dup)
        taken = {_file_of(e) for e in lib[group] if isinstance(e, dict) and isinstance(e.get("path"), str)}
        path = _place_image(group, data, h, ext, taken)
        placed.append(path)
        entry = _new_entry(group, label, category, url, path, len(data))
        lib[group].append(entry)   # 只追加在该档末尾
        return entry

    try:
        entry = _mutate(change)
    except BaseException:
        for p in placed:   # 目录文件没写成：只删刚写的这一张
            with contextlib.suppress(OSError):
                os.unlink(p)
        raise
    _log.info("表情包：已加入 %s/%s", group, _file_of(entry))
    return item_of(group, entry)


# ── 按链接下载 ───────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转：3xx 原样变成 HTTPError 交回 _fetch，由它逐跳重新核对。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _SocketWatch:
    """一次下载建过的 TCP 连接。整体期限到了由等结果的线程 close_all：shutdown 让卡在读响应头或正文的下载线程
    立即读到连接断开、以异常结束，不再占着线程和套接字等单次限时一轮轮过去。

    记的是 dup 出来的描述符：TLS 包装会把原套接字对象 detach 掉，而 shutdown 作用在底层连接上，经 dup 照样生效；
    下载线程手里的描述符由它自己关，这里只关自己的 dup，不会关到被系统复用给别人的描述符。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._dups = []
        self._closed = False

    def add(self, sock) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._dups.append(sock.dup())
                    return
                except OSError:
                    sock.close()
                    raise
        sock.close()   # 期限已过才连上：直接断掉，不再往下发请求
        raise OSError("下载已被放弃")

    def close_all(self) -> None:
        with self._lock:
            self._closed, dups, self._dups = True, self._dups, []
        for d in dups:
            with contextlib.suppress(OSError):
                d.shutdown(socket.SHUT_RDWR)
            d.close()


_tls = threading.local()   # fetch 的下载线程在这里放本次下载的 _SocketWatch；别的线程没有


def _connector(addr):
    """顶替 http.client 的 ``_create_connection``：addr 不为 None 时 TCP 直连它、不再按主机名解析；为 None 时照常
    连给定地址（走代理时就是代理）。连上的套接字登记给本线程的 _SocketWatch。"""
    def create(address, *args):
        sock = socket.create_connection(address if addr is None else (addr, address[1]), *args)
        watch = getattr(_tls, "watch", None)
        if watch is not None:
            watch.add(sock)
        return sock
    return create


def _pinned(conn_class, addr):
    """造连接对象的工厂。连接对象的 host 仍是原主机名，所以 Host 头（非默认端口带端口）、TLS 的 SNI 与证书校验
    都按原主机名走标准库的原有逻辑。"""
    def make(host, **kwargs):
        conn = conn_class(host, **kwargs)
        # http.client 建 TCP 连接只经这一个属性（HTTPS 与代理隧道也是先经它连上再套 TLS）；换掉它只改"连到哪、记下谁"
        conn._create_connection = _connector(addr)
        return conn
    return make


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, addr):
        super().__init__()
        self._addr = addr

    def http_open(self, req):
        return self.do_open(_pinned(http.client.HTTPConnection, self._addr), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, addr):
        super().__init__()
        self._addr = addr

    def https_open(self, req):
        return self.do_open(_pinned(http.client.HTTPSConnection, self._addr), req, context=self._context)


# 测试替身挂点：解析主机名（签名同 socket.getaddrinfo）、打开连接（签名 (Request, timeout, addr) → 响应）。
resolve = socket.getaddrinfo


def open_url(req, timeout, addr):
    """缺省的打开连接，不自动跟随跳转。

    addr 是这一跳核对过的地址：TCP 直连它、不走代理，核对与建连之间不再解析主机名（防 DNS 重绑定）。
    addr 为 None（进程环境对该协议设了代理）时经环境里的代理去连，目标由代理解析。
    两条路建的连接都登记给 fetch 下载线程的 _SocketWatch，期限到了能被主线程断开。
    """
    proxies = {} if addr is not None else urllib.request.getproxies_environment()
    handlers = [urllib.request.ProxyHandler(proxies), _PinnedHTTPHandler(addr), _PinnedHTTPSHandler(addr)]
    return urllib.request.build_opener(*handlers, _NoRedirect()).open(req, timeout=timeout)


def _via_proxy(req) -> bool:
    """进程环境对这一跳的协议设了代理，且主机不在 no_proxy 里。判断口径与 urllib 的 ProxyHandler 相同。"""
    proxies = urllib.request.getproxies_environment()
    return bool(proxies.get(req.type)) and not urllib.request.proxy_bypass_environment(req.host, proxies)


def url_host(url) -> str:
    """只给日志用：链接的主机名；拆不出来就是空串。"""
    try:
        return (urllib.parse.urlsplit(url.strip()).hostname or "")[:253]
    except (ValueError, AttributeError):
        return ""


def _blocked_ip(addr) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return True   # 认不出的地址一律当被拦
    if ip.version == 6:
        ip = ip.ipv4_mapped or ip.sixtofour or ip
    # 198.18.0.0/15（基准测试段，也是部分代理 fake-ip 模式的假地址）ipaddress 算私有，这里照拦，不设例外。
    # is_global 为假还覆盖 100.64.0.0/10（运营商级 NAT）
    return (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or ip.is_private or not ip.is_global
            or (ip.version == 6 and ip.is_site_local))


def _check_url(url):
    """链接本身与主机名的规则（不解析）。返回 (拆好的链接, ASCII 主机名, 端口)。"""
    if not isinstance(url, str):
        raise StickerError("bad_url", "链接必须是文字")
    url = url.strip()
    if not url or len(url) > MAX_URL:
        raise StickerError("bad_url", "链接是空的或太长（最多 %d 个字符）" % MAX_URL)
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise StickerError("bad_url", "链接里不能有空格或控制字符")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise StickerError("bad_url", "链接格式不对")
    if parts.scheme not in ("http", "https"):
        raise StickerError("bad_url", "只支持 http 或 https 链接")
    if "@" in parts.netloc:
        raise StickerError("bad_url", "链接里不能带用户名或密码")
    if port not in (None, 80, 443):
        raise StickerError("bad_url", "链接只能用默认端口（80 或 443）")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise StickerError("bad_url", "链接里没有网站地址")
    try:
        host = host.encode("idna").decode("ascii").lower()   # 先规整（全角字母、数字等），再按名字判断
    except UnicodeError:
        raise StickerError("bad_url", "链接里的网站地址不对")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = bool(_HEX_OR_NUM_LABEL.fullmatch(host.rsplit(".", 1)[-1]))   # 127.1、0x7f.1 这类简写也算
    if is_ip:
        raise StickerError("blocked_host", "不能用直接写 IP 地址的链接，请用带域名的图片地址")
    if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
        raise StickerError("blocked_host", "不能从本机或内网地址下载")
    return parts, host, port or (443 if parts.scheme == "https" else 80)


def _check_resolved(host: str, port: int) -> str:
    """解析主机名（每跳只解析这一次），任一地址被拦就拒；返回要实际连接的地址（解析结果里的第一个）。"""
    try:
        infos = resolve(host, port, 0, socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        raise StickerError("fetch_failed", "下载失败：找不到这个网站")
    addrs = [info[4][0] for info in infos or () if len(info) >= 5 and info[4]]
    if not addrs:
        raise StickerError("fetch_failed", "下载失败：找不到这个网站")
    if any(_blocked_ip(a) for a in addrs):
        raise StickerError("blocked_host", "这个链接指向本机或内网地址，不能下载")
    return addrs[0]


def _wire_url(parts, host: str) -> str:
    """真正发出去的地址：主机名用 ASCII 形式，路径与查询里的非 ASCII 字符按 UTF-8 转义，去掉 # 后面的部分。"""
    port = parts.port
    netloc = host + (":%d" % port if port and port != _DEFAULT_PORTS[parts.scheme] else "")   # Host 头只在非默认端口时带端口
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


def _timeout_error() -> StickerError:
    return StickerError("timeout", "下载超时（整体限 %d 秒，连接与每次读取限 %d 秒）" % (TOTAL_TIMEOUT, IO_TIMEOUT))


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _timeout_error()
    return min(IO_TIMEOUT, left)


def _read_body(resp, deadline: float) -> bytes:
    length = (resp.headers.get("Content-Length") or "").strip()
    if length.isdigit() and (len(length) > 12 or int(length) > MAX_BYTES):
        raise StickerError("too_large", "图片超过 10 MiB")
    buf = bytearray()
    while True:
        _left(deadline)
        try:
            chunk = resp.read1(_CHUNK)   # 每次至多一次系统调用，限时才管得住
        except (TimeoutError, socket.timeout):
            raise _timeout_error()
        except (http.client.HTTPException, OSError):
            raise StickerError("fetch_failed", "下载失败：传到一半断了")
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise StickerError("too_large", "图片超过 10 MiB")


def _fetch(url: str, deadline: float) -> bytes:
    current = url.strip()
    for hop in range(MAX_REDIRECTS + 1):
        parts, host, port = _check_url(current)
        addr = _check_resolved(host, port)
        req = urllib.request.Request(_wire_url(parts, host), headers={
            "User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
        if _via_proxy(req):
            addr = None   # 经代理时目标由代理解析，本机的核对管不到那一侧（残余风险见 README §8.1）
        try:
            resp = open_url(req, _left(deadline), addr)
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if e.headers is not None else None
            e.close()
            if e.code not in _REDIRECT_CODES:
                raise StickerError("fetch_failed", "下载失败：对方回了 HTTP %d" % e.code)
            if not location:
                raise StickerError("fetch_failed", "下载失败：对方要求跳转但没给地址（HTTP %d）" % e.code)
            if hop >= MAX_REDIRECTS:
                raise StickerError("fetch_failed", "下载失败：跳转超过 %d 次" % MAX_REDIRECTS)
            current = urllib.parse.urljoin(current, location.strip())
            continue
        except (TimeoutError, socket.timeout):
            raise _timeout_error()
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise _timeout_error()
            raise StickerError("fetch_failed", "下载失败：连不上对方")
        except (http.client.HTTPException, OSError, ValueError):
            raise StickerError("fetch_failed", "下载失败：连不上对方")
        with resp:
            return _read_body(resp, deadline)
    raise StickerError("fetch_failed", "下载失败：跳转超过 %d 次" % MAX_REDIRECTS)   # 走不到：最后一跳已在上面拦下


def fetch(url: str) -> bytes:
    """按链接下载，返回原始字节；类型由 add 按文件头判断。

    下载放在单独的守护线程里，这里最多等到整体时限：对方逐字节慢慢回响应头时，http.client 读状态行与头部只受
    单次 IO 限时约束，管不住总时长。期限一到，这里先 shutdown 下载线程建过的连接，它正卡着的读取随即出错、线程
    很快退出，不会挂着线程和套接字慢慢等对方。缺省 open_url 建的连接（直连与走代理）才登记得到；测试替换了
    open_url 时没有这层，只剩单次限时与 10 MiB 上限约束。守护线程不拖住进程退出。
    """
    deadline = time.monotonic() + TOTAL_TIMEOUT
    box, done, watch = {}, threading.Event(), _SocketWatch()

    def run():
        _tls.watch = watch
        try:
            box["data"] = _fetch(url, deadline)
        except BaseException as e:   # 原样交回调用方线程
            box["error"] = e
        finally:
            watch.close_all()   # 正常结束时只关登记用的 dup；连接本身已由 _fetch 关掉
            done.set()

    threading.Thread(target=run, name="sticker-fetch", daemon=True).start()
    if not done.wait(max(0.0, deadline - time.monotonic()) + 1.0):
        watch.close_all()
        raise _timeout_error()
    if "error" in box:
        raise box["error"]
    return box["data"]


def add_from_url(group, label, category, url) -> dict:
    """按链接添加：先核对字段、链接、库与同链接查重，再下载，最后走 add（锁内再查一遍重）。"""
    group, label, category = check_fields(group, label, category)
    _check_url(url)
    url = url.strip()
    _raw, lib, _mode = _load()   # 库坏了先报，不白下载
    dup = _find_url_duplicate(lib, url)
    if dup:
        raise _duplicate(*dup)
    return add(group, label, category, fetch(url), url=url)


# ── 改与移除 ─────────────────────────────────────────────────────────────────

def _check_target(group, name) -> None:
    if not isinstance(group, str) or group not in GROUPS or not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise StickerError("not_found", "没有这一张")


def _locate(lib: dict, group: str, name: str) -> int:
    hits = [i for i, e in enumerate(lib[group]) if _complete(e) and _file_of(e) == name]
    if not hits:
        raise StickerError("not_found", "库里没有这一张（可能已被移除），请刷新后再试")
    if len(hits) > 1:
        raise StickerError("ambiguous", "这一档里有不止一条用这个文件名，请先手工处理表情库文件")
    if not _directly_in(lib[group][hits[0]]["path"], _real_group_dir(group)):
        raise StickerError("not_found", "这一条的图片不在本档的图片目录里，不能在这里操作")
    return hits[0]


def update(group, name, changes: dict) -> dict:
    """原地改 label（nsfw 另可改 category），其他键与条目位置都不动。changes 里只看 label、category。"""
    _check_target(group, name)
    new = {}
    if "label" in changes:
        new["label"] = _text(changes["label"], "标签")
    if group == "nsfw" and "category" in changes:
        new["category"] = _text(changes["category"], "分类")
    if not new:
        raise StickerError("invalid", "没有要改的内容")

    def change(lib):
        i = _locate(lib, group, name)
        entry = lib[group][i]
        if "category" in new and "category" not in entry:
            # 原来没有分类：补在 label 后面（同新条目的键顺序），其余键先后不变
            rebuilt = {}
            for k, v in entry.items():
                rebuilt[k] = v
                if k == "label":
                    rebuilt["category"] = new["category"]
            entry = lib[group][i] = rebuilt
        entry.update(new)   # 已有的键原地换值，位置不变
        return entry

    entry = _mutate(change)
    _log.info("表情包：已修改 %s/%s", group, name)
    return item_of(group, entry)


def _move_to_removed(path: str, group: str) -> bool:
    """把图片挪到 ``<图片根>/.removed/<档>/<文件名>``，重名在扩展名前加 ``-<YYYYmmdd-HHMMSS>``。挪不了 → False。"""
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or not _directly_in(path, _real_group_dir(group)):
            return False   # 只挪本档目录里的普通文件；符号链接、不在了的都不动
        dst_dir = os.path.join(stickers_dir(), REMOVED_DIR, group)
        os.makedirs(dst_dir, exist_ok=True)
        name = os.path.basename(path)
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        candidates = [name] + ["%s-%s%s%s%s" % (stem, stamp, "-%d" % k if k else "", dot, ext) for k in range(100)]
        for cand in candidates:
            try:
                _move_no_clobber(path, os.path.join(dst_dir, cand))
                return True
            except FileExistsError:
                continue
        return False
    except FileNotFoundError:
        return False
    except Exception:
        _log.exception("表情包：移除后挪图失败 %s/%s", group, os.path.basename(path))
        return False


def remove(group, name) -> bool:
    """先从目录文件里删掉这一条，再把图片挪进 .removed/。挪不了条目照样已删，返回 False。绝不删除图片。"""
    _check_target(group, name)
    out = {}

    def change(lib):
        out["path"] = lib[group].pop(_locate(lib, group, name))["path"]

    def after(_result):
        out["moved"] = _move_to_removed(out["path"], group)

    _mutate(change, after=after)
    _log.info("表情包：已移除 %s/%s moved=%s", group, name, out["moved"])
    return out["moved"]


# ── 图片路由 ───────────────────────────────────────────────────────────────────

def image_file(group, name):
    """库里有条目、真实路径就在该档目录里、文件头是四种之一 → (真实路径, mimetype)；其余一律 None。"""
    if not isinstance(group, str) or group not in GROUPS or not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None
    try:
        _raw, lib, _mode = _load()
    except StickerError:
        return None
    base = _real_group_dir(group)
    for e in lib[group]:
        if not _complete(e) or _file_of(e) != name:
            continue
        real = os.path.realpath(e["path"])
        if not _directly_in(real, base) or not os.path.isfile(real):
            continue
        try:
            with open(real, "rb") as f:
                ext = sniff(f.read(16))
        except OSError:
            continue
        if ext:
            return real, MIMETYPES[ext]
    return None
