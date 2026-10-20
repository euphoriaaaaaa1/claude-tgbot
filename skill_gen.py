"""技能模板生成器（公开版 T08；INTERFACE §3.3、§4.5）。

模板在 ``configs/templates/skills/<通用名>/SKILL.md``，六个通用名固定为 ``image``、``voice``、``gif``、
``moments``、``proactive``、``group``（不含 ``nsfw``：自动生成从不写它，也从不碰它）。每个模板恰有一对
``<!-- portable:start -->`` / ``<!-- portable:end -->``：圈内是这只 bot 自己的内容，``refresh`` 逐字节保留；
圈外由模板按当前设置重新渲染。

两种模式：
- ``fill``：只写给定通用名里还不存在的文件；已有文件一个字节都不动。
- ``refresh``：只重写已存在文件的圈外；圈内（含标记）逐字节保留。

公开版适配（与 CBL 的差异）：
- 不依赖 ``bot_profile`` / ``voice_languages``（尚未移植）：技能文件锁直接走 ``platform_lock`` 的
  ``.persona.lock``，与适配器的人设写入口径一致；语音模板的 VOICE-REPLY 段按模板原样渲染，等 T21
  移植 ``voice_languages`` 后再接语言设置；
- 名单与频道目录走 ``bot_features``（``bots_registry`` 唯一注册表）；
- ``--all`` 不含个人 bot 例外。

写入规矩：渲染全部先算完，任何一处渲染失败整批一个文件都不写；写前备份只留 2 份；同目录临时文件 +
原子改名；重复跑同样的命令全部报"没变"。技能手管（``.skills-manual``）整只跳过；软链不写。
"""
import contextlib
import datetime
import logging
import os
import re
import stat
import sys
import tempfile

_log = logging.getLogger(__name__)

START = "<!-- portable:start -->"
END = "<!-- portable:end -->"
APPEARANCE_PH = "{{appearance}}"
DEFAULT_APPEARANCE = "按人设里的外貌描写来画。"
SKILL_FILE = "SKILL.md"
BACKUP_INFIX = ".bak-"
BACKUP_KEEP = 2
NEW_FILE_MODE = 0o600
SKILLS_MANUAL_NAME = ".skills-manual"
LOCK_WAIT_SEC = 3.0
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs", "templates", "skills")

SKILL_NAMES = ("image", "voice", "gif", "moments", "proactive", "group")

PLACEHOLDERS = ("name", "prefix", "skill", "display_name", "appearance",
                "bot_id", "channel_dir", "repo_dir", "python", "skills_root", "media_root")
_PATH_KEYS = frozenset(("channel_dir", "repo_dir", "skills_root", "media_root"))
_PH_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

_TRIM_SPACE = "\u0009\u000a\u000d 　"
_CTRL_EXTRA = "".join(chr(c) for c in range(0x20) if c != 0x0A) + "".join(
    chr(c) for c in range(0x7F, 0xA0))
_MAX_APPEARANCE = 500


class SkillGenError(Exception):
    """``code``：``usage``（参数不对，命令行退出 2）、``busy``（人设锁）、``error``（渲染/读写失败，退出 1）。"""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


# ── 渲染 ─────────────────────────────────────────────────────────────────────

def _check_value(key: str, value) -> None:
    if not isinstance(value, str):
        raise SkillGenError("渲染值 %s 不是字符串" % key)
    if key == "appearance":
        if len(value) > _MAX_APPEARANCE:
            raise SkillGenError("外貌去掉首尾空白后最多 500 个字")
        if any(ch in value for ch in ("<!--", "-->", "{{", "}}")):
            raise SkillGenError("外貌不能含 <!--、-->、{{、}}")
        if any(ch in _CTRL_EXTRA for ch in value):
            raise SkillGenError("外貌不能含换行以外的控制字符")
        return
    if "\n" in value or "\r" in value:
        raise SkillGenError("渲染值 %s 含换行" % key)
    if any(ch in value for ch in ("<!--", "-->", "{{", "}}")):
        raise SkillGenError("渲染值 %s 含注释标记或占位符" % key)
    if key in _PATH_KEYS and not os.path.isabs(value):
        raise SkillGenError("渲染值 %s 展开 ~ 后不是绝对路径" % key)


def _env_path(env_name: str, default: str) -> str:
    return os.path.expanduser(os.environ.get(env_name) or default)


def _repo_dir() -> str:
    return _env_path("BOTLIFE_DIR", os.path.dirname(os.path.abspath(__file__)))


def _builtin_values(bot, bot_dir: str) -> dict:
    return {
        "bot_id": str(bot) if bot else os.path.basename(os.path.normpath(bot_dir)),
        "channel_dir": os.path.realpath(bot_dir),
        "repo_dir": _repo_dir(),
        "python": sys.executable,
        "skills_root": _env_path("CLAUDEBOT_SKILLS_ROOT", "~/.claude/skills"),
        "media_root": _env_path("MOMENTS_MEDIA_ROOT", "~/resource/media"),
    }


def render(template: str, values: dict) -> str:
    if not isinstance(template, str):
        raise SkillGenError("模板不是文本")
    found = [(m.start(), m.end(), m.group(1)) for m in _PH_RE.finditer(template)]
    leftover = _PH_RE.sub("", template)
    if "{{" in leftover or "}}" in leftover:
        raise SkillGenError("模板里有不闭合或不合规的占位符")
    for _a, _b, key in found:
        if key not in PLACEHOLDERS:
            raise SkillGenError("模板里有白名单外的占位符：{{%s}}" % key)
        if key not in values:
            raise SkillGenError("模板需要占位符 {{%s}} 的值" % key)
        _check_value(key, values[key])
    return _PH_RE.sub(lambda m: values[m.group(1)], template)


def _read_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name, SKILL_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise SkillGenError("读不出模板 %s：%s" % (name, type(e).__name__))


def _marker_span(text: str) -> tuple:
    if text.count(START) != 1 or text.count(END) != 1:
        raise SkillGenError("模板的 portable 标记不是正好一对")
    a, b = text.index(START), text.index(END)
    if a >= b:
        raise SkillGenError("模板的 portable 标记顺序不对")
    return a, b + len(END)


def _render_skill(name: str, values: dict, portable: bool = True) -> str:
    template = _read_template(name)
    a, b = _marker_span(template)
    appearance_count = template.count(APPEARANCE_PH)
    if appearance_count:
        if name != "image":
            raise SkillGenError("{{appearance}} 只许出现在 image 模板里")
        if appearance_count != 1:
            raise SkillGenError("{{appearance}} 在 image 模板里必须恰好一处")
        start = template.find(APPEARANCE_PH)
        if not a <= start < b:
            raise SkillGenError("{{appearance}} 只许出现在 image 模板的圈内")
    if name != "image" and "appearance" in values:
        values = {k: v for k, v in values.items() if k != "appearance"}
    rendered = render(template, values)
    if portable:
        return rendered
    ra, rb = _marker_span(rendered)
    return rendered[:ra] + rendered[rb:]


# ── 文件 ─────────────────────────────────────────────────────────────────────

def _skills_root(bot_dir: str) -> str:
    return os.path.join(bot_dir, ".claude", "skills")


def _skills_root_ok(bot_dir: str) -> bool:
    base = os.path.realpath(bot_dir)
    root = _skills_root(bot_dir)
    if os.path.islink(os.path.join(bot_dir, ".claude")) or os.path.islink(root):
        return False
    return os.path.realpath(root) == os.path.join(base, ".claude", "skills")


def _skill_dir(bot_dir: str, name: str) -> str:
    prefix = os.path.basename(os.path.normpath(bot_dir))
    return os.path.join(_skills_root(bot_dir), "%s-%s" % (prefix, name))


def skill_path(bot_dir: str, name: str) -> str:
    return os.path.join(_skill_dir(bot_dir, name), SKILL_FILE)


def _is_symlink(path: str) -> bool:
    return os.path.islink(path)


def _backup(path: str, raw: bytes, mode: int) -> None:
    base = path + BACKUP_INFIX + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bak, n = base, 0
    while os.path.lexists(bak):
        n += 1
        bak = "%s.%d" % (base, n)
    with open(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as f:
        f.write(raw)
    os.chmod(bak, mode)
    d, prefix = os.path.dirname(path), os.path.basename(path) + BACKUP_INFIX
    for old in sorted(x for x in os.listdir(d) if x.startswith(prefix))[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(d, old))


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".%s.tmp-" % os.path.basename(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _skills_manual(bot_dir: str) -> bool:
    return os.path.lexists(os.path.join(bot_dir, SKILLS_MANUAL_NAME))


# ── 计划与执行 ───────────────────────────────────────────────────────────────

def _plan_skip(path: str, name: str, action: str, reason: str) -> dict:
    return {"path": path, "name": name, "action": action, "reason": reason, "new": None, "mode": None}


def _plan_one(bot_dir: str, mode: str, name: str, values: dict, portable: bool) -> dict:
    path = skill_path(bot_dir, name)
    d = os.path.dirname(path)
    if _is_symlink(d) or _is_symlink(path):
        return _plan_skip(path, name, "软链，跳过", "symlink")
    try:
        inside_root = os.path.commonpath([os.path.realpath(path), os.path.realpath(_skills_root(bot_dir))]) \
            == os.path.realpath(_skills_root(bot_dir))
    except ValueError:
        inside_root = False
    if not inside_root:
        return _plan_skip(path, name, "软链，跳过", "symlink")
    if mode == "fill":
        if os.path.lexists(path):
            return _plan_skip(path, name, "没变", "unchanged")
        return {"path": path, "name": name, "action": "将新写", "reason": "placed",
                "new": _render_skill(name, values, portable).encode("utf-8"), "mode": NEW_FILE_MODE}
    if not os.path.lexists(path):
        return _plan_skip(path, name, "不存在，跳过", "not_found")
    if not os.path.isfile(path):
        return _plan_skip(path, name, "不是普通文件，跳过", "not_file")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return _plan_skip(path, name, "读不出，跳过", "unreadable")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return _plan_skip(path, name, "不是 UTF-8，跳过", "invalid_utf8")
    if portable:
        text = raw.decode("utf-8")
        if text.count(START) != 1 or text.count(END) != 1:
            return _plan_skip(path, name, "没有成对的 portable 标记（legacy），跳过", "legacy")
        ra, rb = text.index(START), text.index(END)
        if ra >= rb:
            return _plan_skip(path, name, "技能文件异常（portable 标记顺序不对），跳过", "anomaly")
    rendered = _render_skill(name, values, portable)
    if not portable:
        new = rendered.encode("utf-8")
    else:
        raw_rendered = rendered.encode("utf-8")
        start_b, end_b = START.encode("utf-8"), END.encode("utf-8")
        circle = raw[raw.index(start_b):raw.index(end_b) + len(end_b)]
        a = raw_rendered.index(start_b)
        b = raw_rendered.index(end_b) + len(end_b)
        new = raw_rendered[:a] + circle + raw_rendered[b:]
    if new == raw:
        return _plan_skip(path, name, "没变", "unchanged")
    return {"path": path, "name": name, "action": "将改圈外", "reason": "rewrite", "new": new,
            "mode": stat.S_IMODE(os.stat(path).st_mode)}


def _apply_plan(item: dict, apply: bool) -> str:
    if item["new"] is None:
        return item["action"]
    action = item["action"]
    if not apply:
        return action
    path = item["path"]
    if _is_symlink(os.path.dirname(path)) or _is_symlink(path):
        return "软链，跳过"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if action == "将改圈外":
        try:
            with open(path, "rb") as f:
                raw = f.read()
            _backup(path, raw, item["mode"])
        except OSError as e:
            raise SkillGenError("备份失败：%s" % type(e).__name__)
    _write_atomic(path, item["new"], item["mode"])
    return "新写" if action == "将新写" else "改圈外"


def _resolve_bot(bot, staging_dir, bot_dir=None):
    if bot_dir is not None:
        bot_dir = os.path.abspath(os.path.expanduser(str(bot_dir)))
        return bot_dir, str(bot or os.path.basename(bot_dir))
    if staging_dir is not None:
        bot_dir = os.path.abspath(os.path.expanduser(str(staging_dir)))
        return bot_dir, str(bot or os.path.basename(bot_dir))
    if not isinstance(bot, str) or not bot:
        raise SkillGenError("要给 bot 编号", "usage")
    import bot_features
    bot_dir = bot_features.channel_dir(bot)
    if bot_dir is None:
        raise SkillGenError("没有这只 bot：%s" % bot, "usage")
    return bot_dir, bot


def _current_display(bot, fallback: str):
    if not bot:
        return fallback, None
    try:
        import config_loader
        cfg = config_loader.load_bot(str(bot))
    except FileNotFoundError:
        return fallback, None
    except Exception as e:  # noqa: BLE001 损坏的配置不能拿编号顶替
        return None, "显示名读不出（%s），跳过" % type(e).__name__
    value = cfg.get("display_name") if isinstance(cfg, dict) else None
    if isinstance(value, str) and value.strip():
        return value, None
    return None, "配置里没有可用的显示名，跳过"


def _discover_skills(bot_dir: str, prefix: str) -> list:
    root = _skills_root(bot_dir)
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    token = prefix + "-"
    for entry in entries:
        if not entry.startswith(token):
            continue
        path = os.path.join(root, entry, SKILL_FILE)
        if not os.path.lexists(path):
            continue
        out.append((entry, entry[len(token):], path))
    return out


@contextlib.contextmanager
def _bot_locked(bot, bot_dir: str):
    """技能文件与人设共用 ``<频道目录>/.persona.lock``（T03 的跨平台锁）。"""
    import platform_lock
    try:
        with platform_lock.file_lock(os.path.join(bot_dir, ".persona.lock"), timeout=LOCK_WAIT_SEC):
            yield
    except platform_lock.LockBusy:
        raise SkillGenError("别的操作正在改这只 bot 的人设，请稍后再试", "busy") from None
    except platform_lock.LockError:
        raise SkillGenError("人设锁建不出来，文件没有改动") from None


def write_all(bot, mode, names=None, appearance=None, portable=True, *, display_name=None,
              staging_dir=None, bot_dir=None, apply=True) -> dict:
    """按 ``mode``（fill/refresh）处理 ``names`` 里的通用名，返回 ``{"results", "placed", "errors"}``。"""
    if mode not in ("fill", "refresh"):
        raise SkillGenError("mode 只能是 fill 或 refresh", "usage")
    if names is None:
        if mode == "fill":
            raise SkillGenError("fill 要带 names", "usage")
        names = list(SKILL_NAMES)
    names = list(dict.fromkeys(names))
    for name in names:
        if name not in SKILL_NAMES:
            raise SkillGenError("不在模板里的通用名：%s" % name, "usage")
    bot_dir, fallback_display = _resolve_bot(bot, staging_dir, bot_dir)
    prefix = os.path.basename(os.path.normpath(bot_dir))
    if _skills_manual(bot_dir):
        return {"results": [{"path": skill_path(bot_dir, n), "name": n, "action": "技能手管，跳过",
                             "reason": "skills_manual", "error": False} for n in names],
                "placed": [], "errors": 0}

    results, placed, errors = [], [], 0
    not_managed = []
    with _bot_locked(bot, bot_dir):
        if not _skills_root_ok(bot_dir):
            return {"results": [{"path": skill_path(bot_dir, n), "name": n,
                                 "action": "技能目录是软链，跳过", "reason": "symlink", "error": False}
                                for n in names],
                    "placed": [], "errors": 0}
        if isinstance(display_name, str) and display_name:
            display, display_error = display_name, None
        else:
            display, display_error = _current_display(bot, fallback_display)
        if display_error:
            return {"results": [{"path": skill_path(bot_dir, n), "name": n, "action": display_error,
                                 "reason": "display_name_unreadable", "error": False} for n in names],
                    "placed": [], "errors": 0}
        if mode == "refresh":
            for entry, suffix, path in _discover_skills(bot_dir, prefix):
                if suffix in SKILL_NAMES:
                    if suffix not in names:
                        names.append(suffix)
                else:
                    not_managed.append({"path": path, "name": entry, "action": "not_managed，跳过",
                                        "reason": "not_managed", "error": False})
        base_values = {"prefix": prefix, "skill": "", "display_name": display}
        base_values.update(_builtin_values(bot, bot_dir))
        plans = []
        for name in names:
            values = dict(base_values, name="%s-%s" % (prefix, name), skill=name)
            if name == "image":
                raw_appearance = DEFAULT_APPEARANCE if appearance is None else appearance
                if not isinstance(raw_appearance, str):
                    raise SkillGenError("外貌不是字符串")
                raw_appearance = raw_appearance.strip(_TRIM_SPACE)
                values["appearance"] = raw_appearance or DEFAULT_APPEARANCE
            plans.append(_plan_one(bot_dir, mode, name, values, portable))

        for item in plans:
            try:
                action = _apply_plan(item, apply)
            except OSError as e:
                errors += 1
                results.append({"path": item["path"], "name": item["name"], "reason": "write_failed",
                                "action": "写失败：%s" % type(e).__name__, "error": True})
                _log.warning("技能文件写失败 %s：%s", item["path"], type(e).__name__)
                continue
            except SkillGenError as e:
                errors += 1
                results.append({"path": item["path"], "name": item["name"], "reason": "write_failed",
                                "action": str(e), "error": True})
                continue
            reason = item.get("reason")
            if action != item["action"]:
                reason = "symlink" if action == "软链，跳过" else reason
            results.append({"path": item["path"], "name": item["name"], "action": action,
                            "reason": reason, "error": False})
            if apply and action == "新写":
                placed.append(item["name"])
    results.extend(not_managed)
    return {"results": results, "placed": placed, "errors": errors}


# ── 命令行 ───────────────────────────────────────────────────────────────────

def _parse_names(text: str) -> list:
    names = [part.strip() for part in text.split(",")]
    if not any(names):
        raise SkillGenError("--names 是空的", "usage")
    if any(not part for part in names):
        raise SkillGenError("--names 里有空项", "usage")
    return names


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m skill_gen")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bot")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--mode", required=True, choices=("fill", "refresh"))
    parser.add_argument("--names")
    parser.add_argument("--apply", action="store_true")
    try:
        ns = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)

    try:
        names = None
        if ns.names is not None:
            names = _parse_names(ns.names)
        elif ns.mode == "fill":
            raise SkillGenError("fill 必须带 --names", "usage")
        if names is not None:
            bad = [n for n in names if n not in SKILL_NAMES]
            if bad:
                raise SkillGenError("不在模板里的通用名：%s" % "、".join(bad), "usage")
        if ns.all:
            import bot_features
            bots = [bot_id for bot_id, _ in bot_features._listed_bots()]
        else:
            bots = [ns.bot]
    except SkillGenError as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    rc = 0
    for bot in bots:
        try:
            result = write_all(bot, ns.mode, names, apply=ns.apply)
        except SkillGenError as e:
            if e.code == "usage":
                sys.stderr.write(str(e) + "\n")
                return 2
            sys.stderr.write(str(e) + "\n")
            rc = 1
            continue
        for item in result["results"]:
            line = "%s %s" % (item["path"], item["action"])
            if item.get("error"):
                sys.stderr.write(line + "\n")
                rc = 1
            else:
                sys.stdout.write(line + "\n")
        if result["errors"]:
            rc = 1
    return rc


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8")
    logging.getLogger().addHandler(logging.NullHandler())
    import skill_gen
    sys.exit(skill_gen.main())
