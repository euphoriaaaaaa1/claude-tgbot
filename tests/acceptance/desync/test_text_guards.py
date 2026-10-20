"""文本守卫（公开仓）：CLAUDE.md 不得加回 Compact Instructions；README 须说明停用后计划任务仍触发与卸载方式。"""
import glob
import os

from _helpers import REPO


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_chenlulu_CLAUDE_md_无Compact_Instructions标题():
    t = _read(os.path.join(REPO, "channels", "chenlulu", "CLAUDE.md"))
    assert "Compact Instructions" not in t and "摘要必须保留" not in t


def test_所有bot目录CLAUDE_md_都无Compact_Instructions():
    files = glob.glob(os.path.join(REPO, "channels", "*", "CLAUDE.md"))
    assert files
    for p in files:
        t = _read(p)
        assert "Compact Instructions" not in t and "摘要必须保留" not in t, p


def test_README_说明停用后计划任务仍触发与卸载命令():
    t = _read(os.path.join(REPO, "README.md"))
    assert "Unregister-ScheduledTask" in t
    assert "launchctl bootout" in t
    assert "claude-tgbot-self-initiate-" in t
