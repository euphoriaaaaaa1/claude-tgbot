"""缺陷③a（公开 scripts/install_compact_hook.py）：项目级 settings.json 合并器——正常路径。
错误路径见 test_install_hook_errors.py。隔离：bot_dir 与 HOME 都在临时目录。
"""
import glob
import json
import os
import subprocess
import sys

from _helpers import REPO

SCRIPT = os.path.join(REPO, "scripts", "install_compact_hook.py")
BASE = {"model": "haiku", "permissions": {"allow": ["Read"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}


def _bot(iso, name="bot1", settings=BASE, raw=None):
    d = os.path.join(iso["home"], name)
    os.makedirs(os.path.join(d, ".claude"))
    p = os.path.join(d, ".claude", "settings.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else json.dumps(settings, ensure_ascii=False, indent=2))
    return d, p


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, timeout=30)


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _backups(p):
    return glob.glob(p + ".bak-desync-*")


def _compact_items(s):
    return [h for h in s["hooks"]["SessionStart"] if h.get("matcher") == "compact"]


def test_首次安装_追加compact项_命令为解释器加脚本_timeout10(iso):
    d, p = _bot(iso)
    r = _run(d)
    assert r.returncode == 0, r.stdout + r.stderr
    items = _compact_items(_load(p))
    assert len(items) == 1
    cmd = items[0]["hooks"][0]["command"].split()
    assert cmd[0] == sys.executable and os.path.basename(cmd[-1]) == "compact_group_context.py"
    assert items[0]["hooks"][0]["type"] == "command" and items[0]["hooks"][0]["timeout"] == 10


def test_首次安装_其它顶层键与PreToolUse逐字保留(iso):
    d, p = _bot(iso)
    assert _run(d).returncode == 0
    s = _load(p)
    assert s["model"] == "haiku" and s["permissions"] == BASE["permissions"]
    assert s["hooks"]["PreToolUse"] == BASE["hooks"]["PreToolUse"]


def test_首次安装_写备份且备份内容等于原文件(iso):
    d, p = _bot(iso)
    orig = open(p, "rb").read()
    _run(d)
    b = _backups(p)
    assert len(b) == 1 and open(b[0], "rb").read() == orig


def test_重复安装_already_installed_exit0_不再备份_仍只一条(iso):
    d, p = _bot(iso)
    _run(d)
    r = _run(d)
    assert r.returncode == 0 and "already installed" in r.stdout
    assert len(_backups(p)) == 1 and len(_compact_items(_load(p))) == 1


def test_同名脚本不同命令_替换为一条(iso):
    s = json.loads(json.dumps(BASE))
    s["hooks"]["SessionStart"] = [{"matcher": "compact", "hooks": [{"type": "command", "command": "/old/python /old/compact_group_context.py", "timeout": 5}]}]
    d, p = _bot(iso, settings=s)
    assert _run(d).returncode == 0
    items = _compact_items(_load(p))
    assert len(items) == 1 and items[0]["hooks"][0]["command"].startswith(sys.executable)


def test_已有其它SessionStart项_保留并追加(iso):
    s = json.loads(json.dumps(BASE))
    s["hooks"]["SessionStart"] = [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo mem"}]}]
    d, p = _bot(iso, settings=s)
    assert _run(d).returncode == 0
    ss = _load(p)["hooks"]["SessionStart"]
    assert ss[0] == s["hooks"]["SessionStart"][0] and len(ss) == 2


def test_没有hooks键_创建hooks与SessionStart数组(iso):
    d, p = _bot(iso, settings={"model": "haiku"})
    assert _run(d).returncode == 0
    s = _load(p)
    assert s["model"] == "haiku" and len(_compact_items(s)) == 1


def test_dry_run_只打印不写不备份(iso):
    d, p = _bot(iso)
    orig = open(p, "rb").read()
    r = _run(d, "--dry-run")
    assert r.returncode == 0 and "compact_group_context.py" in r.stdout
    assert open(p, "rb").read() == orig and _backups(p) == []
