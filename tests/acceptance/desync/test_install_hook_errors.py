"""缺陷③a（公开 scripts/install_compact_hook.py）：错误契约，逐条对应 INTERFACE §3.3。
对外入口只有一个：`python3 scripts/install_compact_hook.py <bot_dir> [--dry-run]`，退出码 + stdout 文案 + 文件字节。
隔离：bot_dir 与 HOME 都在 CLAUDEBOTLIFE_TEST_ROOT 下（iso 夹具）。
"""
import glob
import json
import os
import stat
import subprocess
import sys

from _helpers import REPO

SCRIPT = os.path.join(REPO, "scripts", "install_compact_hook.py")


def _run(bot_dir):
    return subprocess.run([sys.executable, SCRIPT, bot_dir], capture_output=True, text=True, timeout=30)


def _bot(iso, raw, name="bot1"):
    """建 <bot_dir>/.claude/settings.json，内容为 raw（字符串原样写入）。返回 (bot_dir, settings 路径)。"""
    d = os.path.join(iso["home"], name)
    os.makedirs(os.path.join(d, ".claude"))
    p = os.path.join(d, ".claude", "settings.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw)
    return d, p


def _backups(p):
    return glob.glob(p + ".bak-desync-*")


# ---- `<bot_dir>/.claude/` 不存在 → 打印 no project settings dir, skip；exit 2；不创建目录 ----
def test_无claude子目录_exit2_打印skip_不创建目录(iso):
    d = os.path.join(iso["home"], "nobot")
    os.makedirs(d)
    r = _run(d)
    assert r.returncode == 2 and "no project settings dir, skip" in r.stdout
    assert not os.path.exists(os.path.join(d, ".claude"))


def test_bot目录本身不存在_同样exit2_不创建任何东西(iso):
    d = os.path.join(iso["home"], "ghost")
    r = _run(d)
    assert r.returncode == 2 and "no project settings dir, skip" in r.stdout
    assert not os.path.exists(d)


# ---- settings.json 非法 JSON / hooks 非对象 / SessionStart 非数组 / 元素非对象 → exit 3，不写、不备份 ----
def test_settings非法JSON_exit3_文件字节不变_无备份(iso):
    d, p = _bot(iso, "{bad json")
    r = _run(d)
    assert r.returncode == 3
    assert open(p, encoding="utf-8").read() == "{bad json" and _backups(p) == []


def test_hooks存在但非对象_exit3_不写不备份(iso):
    raw = json.dumps({"hooks": []})
    d, p = _bot(iso, raw)
    assert _run(d).returncode == 3
    assert open(p, encoding="utf-8").read() == raw and _backups(p) == []


def test_SessionStart存在但非数组_exit3_不写不备份(iso):
    raw = json.dumps({"hooks": {"SessionStart": {"matcher": "compact"}}})
    d, p = _bot(iso, raw)
    assert _run(d).returncode == 3
    assert open(p, encoding="utf-8").read() == raw and _backups(p) == []


def test_SessionStart数组元素非对象_exit3_不写不备份(iso):
    raw = json.dumps({"hooks": {"SessionStart": ["x", 1]}})
    d, p = _bot(iso, raw)
    assert _run(d).returncode == 3
    assert open(p, encoding="utf-8").read() == raw and _backups(p) == []


# ---- 目标解析后 = ~/.claude/settings.json（全局）→ exit 4 + refuse: global settings，任何情况下不写 ----
def test_bot_dir为HOME_目标是全局settings_exit4_refuse_不写不备份(iso):
    home = iso["home"]
    os.makedirs(os.path.join(home, ".claude"))
    gp = os.path.join(home, ".claude", "settings.json")
    with open(gp, "w") as f:
        f.write("{}")
    r = _run(home)
    assert r.returncode == 4 and "refuse: global settings" in r.stdout
    assert open(gp).read() == "{}" and _backups(gp) == []


def test_bot_dir经过点点回到HOME_仍exit4_不写(iso):
    home = iso["home"]
    os.makedirs(os.path.join(home, ".claude"))
    gp = os.path.join(home, ".claude", "settings.json")
    with open(gp, "w") as f:
        f.write("{}")
    r = _run(os.path.join(home, "bot1", ".."))
    assert r.returncode == 4 and "refuse: global settings" in r.stdout
    assert open(gp).read() == "{}" and _backups(gp) == []


# ---- 备份 / tmp 写入 / rename 失败（权限）→ exit 5 + write failed: <类名>，原文件字节不变 ----
def test_claude目录只读_exit5_write_failed_原文件字节不变(iso):
    d, p = _bot(iso, json.dumps({"model": "haiku"}))
    orig = open(p, "rb").read()
    os.chmod(os.path.join(d, ".claude"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        r = _run(d)
    finally:
        os.chmod(os.path.join(d, ".claude"), stat.S_IRWXU)
    assert r.returncode == 5 and "write failed:" in r.stdout
    assert open(p, "rb").read() == orig


# ---- 边界：中文+空格目录名照常安装（exit 0，恰一条 compact 项）----
def test_bot目录名含中文与空格_照常安装_恰一条compact项(iso):
    d, p = _bot(iso, json.dumps({"model": "haiku"}), name="陈 璐璐")
    assert _run(d).returncode == 0
    with open(p, encoding="utf-8") as f:
        items = [h for h in json.load(f)["hooks"]["SessionStart"] if h.get("matcher") == "compact"]
    assert len(items) == 1
