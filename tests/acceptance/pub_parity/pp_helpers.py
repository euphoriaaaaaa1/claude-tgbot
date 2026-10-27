# -*- coding: utf-8 -*-
"""pub-parity 验收测试公共工具（只依据 pub-parity-接口.md 的对外约定）。

零副作用约定：所有子进程的 HOME/USERPROFILE 指向 pytest 的临时目录，令牌一律假值，
不联网、不起真 claude、不碰真实 ~/.claude。模块名特意不叫 _helpers，避免与 desync/_helpers.py 撞名。
"""
import datetime
import json
import os
import re
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SCRIPTS = os.path.join(REPO, "scripts")
INSTALLER = os.path.join(SCRIPTS, "install_compact_hook.py")
SAMPLE_PATH = os.path.join(REPO, "tests", "fixtures", "inbound_private.txt")

GROUP_HOOK = "compact_group_context.py"
PRIVATE_HOOK = "compact_private_context.py"
RECALL_HOOK = "recall_private.py"
RECAP_HOOK = "clear_recap.py"
# 接口文件 ①-7：四条 SessionStart 的唯一顺序，matcher 对应 source
FOUR_HOOKS = [("compact", GROUP_HOOK), ("compact", PRIVATE_HOOK), ("resume", RECALL_HOOK), ("startup", RECAP_HOOK)]

FAKE_TOKEN = "sk-FAKE-TEST-TOKEN-do-not-leak-0123456789"
REPLY_TOOL = "mcp__telegram-worker__reply"


def script_path(name):
    return os.path.join(SCRIPTS, name)


def child_env(home, **extra):
    """最小子进程环境：不继承任何外部变量（尤其是令牌），HOME 指向临时目录，时区固定 UTC。"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "USERPROFILE": str(home),
           "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


def run_py(script, home, args=(), stdin_text="", timeout=30):
    """跑一个 python 脚本；返回 (退出码, stdout 文本[严格 UTF-8 解码], stderr 文本)。"""
    r = subprocess.run([sys.executable, script, *args], input=stdin_text.encode("utf-8"),
                       capture_output=True, env=child_env(home), timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8", "replace")


def run_hook(name, home, payload, args=()):
    """按 CLI 方式喂 hook JSON（payload 为 dict 则序列化，为 str 则原样喂）。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return run_py(script_path(name), home, args=args, stdin_text=text)


def run_installer(home, bot_dir, *flags):
    return run_py(INSTALLER, home, args=(str(bot_dir), *flags))


# ---------- 临时家目录 / bot 目录 / 会话文件 ----------
def make_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def make_bot(tmp_path, name="bot1", settings=None, raw=None):
    """建 <tmp>/channels/<name>/.claude/settings.json；返回 (bot_dir, settings 路径)。"""
    bot = tmp_path / "channels" / name
    (bot / ".claude").mkdir(parents=True)
    sp = bot / ".claude" / "settings.json"
    sp.write_text(raw if raw is not None else json.dumps(settings if settings is not None else {}, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    return bot, sp


def projects_dir(home, slug="-bot1"):
    d = home / ".claude" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def hook_payload(source, cwd, transcript_path):
    return {"hook_event_name": "SessionStart", "session_id": "sess-1", "source": source,
            "cwd": str(cwd), "transcript_path": str(transcript_path)}


def iso_z(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def user_row(uuid, parent, content, ts):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "message": {"role": "user", "content": content}}


def reply_row(uuid, parent, chat_id, text, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_" + uuid, "name": REPLY_TOOL, "input": {"chat_id": str(chat_id), "text": text}}]}}


# ---------- 入站样例（tests/fixtures/inbound_private.txt，由实现提供，测试只读）----------
def load_sample():
    assert os.path.isfile(SAMPLE_PATH), "样例文件不存在（应由实现提供）: tests/fixtures/inbound_private.txt"
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return f.read()


_ATTR = re.compile(r'([A-Za-z_][\w-]*)="([^"]*)"')


def parse_inbound(text):
    """按接口 ①-11 的新规则拆样例：去尾部空白后以 </channel> 结尾；最后一个 <channel 为开标签；
    返回 dict(prefix, attrs[有序], body)。拆不开就让测试失败并说明。"""
    t = text.rstrip()
    assert t.endswith("</channel>"), "样例去尾部空白后应以 </channel> 结尾"
    i = t.rindex("<channel")
    prefix, rest = t[:i], t[i:]
    gt = rest.index(">")
    attrs = _ATTR.findall(rest[:gt])
    body = rest[gt + 1:-len("</channel>")].strip("\n")
    return {"prefix": prefix, "attrs": attrs, "body": body}


def build_inbound(sample, body, prefix=None, **attr_overrides):
    """以样例为底：换正文、可换前缀、可覆盖属性（值为 None 则删除该属性）。返回完整入站文本。"""
    p = parse_inbound(sample)
    attrs = [(k, v) for k, v in p["attrs"] if not (k in attr_overrides and attr_overrides[k] is None)]
    attrs = [(k, attr_overrides[k] if (k in attr_overrides and attr_overrides[k] is not None) else v) for k, v in attrs]
    for k, v in attr_overrides.items():
        if v is not None and k not in dict(attrs):
            attrs.append((k, v))
    tag = "<channel" + "".join(f' {k}="{v}"' for k, v in attrs) + ">"
    return (p["prefix"] if prefix is None else prefix) + tag + "\n" + body + "\n</channel>"


def sample_identity(sample):
    """样例里的 (名字, 时刻 ISO, chat_id)。缺字段就让测试明确失败。"""
    a = dict(parse_inbound(sample)["attrs"])
    for k in ("user", "ts", "chat_id"):
        assert k in a, f"样例开标签缺少属性 {k}（应有 chat_id/user/ts）"
    return a["user"], a["ts"], a["chat_id"]


def fmt_time(ts_iso):
    """输出行里的 MM-DD HH:MM（子进程 TZ=UTC，所以按 UTC 换算）。"""
    d = datetime.datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
    return d.strftime("%m-%d %H:%M")


def expect_line(name, ts_iso, body):
    return f"{fmt_time(ts_iso)} {name}: {body}"


def meta_stderr_ok(err, script_name):
    """stderr 至多一行，且形如 '<脚本>: ok ...' 或 '<脚本>: skip <类名>'。"""
    lines = [l for l in err.splitlines() if l.strip()]
    assert len(lines) <= 1, f"stderr 应至多一行元数据: {err!r}"
    if lines:
        assert re.fullmatch(re.escape(script_name) + r": (ok\b.*|skip [A-Za-z_][A-Za-z0-9_]*)", lines[0]), f"stderr 格式不符: {lines[0]!r}"
    return lines[0] if lines else ""


def root_user():
    return hasattr(os, "geteuid") and os.geteuid() == 0


skip_if_root = pytest.mark.skipif(root_user(), reason="root 不受目录权限限制，无法构造写失败")
skip_on_windows = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")


def arm_marker(home, bot_dir):
    """经补装命令 --recall 写布防标记（保证标记格式与产品一致）。返回标记路径。"""
    rc, out, err = run_installer(home, bot_dir, "--recall")
    assert rc == 0, f"前置失败：补装命令 --recall 退出码 {rc}: {out} {err}"
    return os.path.join(str(bot_dir), ".claude", "compact-recall.armed")
