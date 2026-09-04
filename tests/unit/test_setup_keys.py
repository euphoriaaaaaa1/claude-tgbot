"""scripts/setup_keys.py：三把密钥写进各自文件，格式各不相同、其余内容不能被动。
跑：python3 -m pytest tests/unit/test_setup_keys.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup_keys.py"
SH = ROOT / "scripts" / "setup_keys.sh"


def run(*args, home: Path, repo: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--home", str(home), "--repo", str(repo)],
        capture_output=True, text=True,
    )


@pytest.fixture
def sandbox(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "_global.yml").write_text(
        "# 顶注释\njiwen:\n  tick_interval_min: 5\n  delta_llm:                   # 🔴 独立于主模型\n"
        "    base_url: https://api.deepseek.com/anthropic\n"
        '    api_key: "YOUR_DEEPSEEK_API_KEY"   # ← 填你自己的 DeepSeek key\n'
        "    model: deepseek-v4-flash\nother:\n  api_key: keep-me\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    bot = home / ".claude" / "channels" / "chenlulu"
    bot.mkdir(parents=True)
    (bot / ".env").write_text("# 注释\nTELEGRAM_BOT_TOKEN=123456789:AA-your-telegram-bot-token-here\n", encoding="utf-8")
    (bot / "access.json").write_text(json.dumps({
        "dmPolicy": "allowlist", "allowFrom": ["YOUR_TELEGRAM_USER_ID"], "groups": {}, "mentionPatterns": ["@x"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return repo, home


def test_status_all_unfilled(sandbox):
    repo, home = sandbox
    r = run("status", home=home, repo=repo)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"deepseek": False, "token": False, "userid": False}


def test_set_deepseek_replaces_only_delta_llm_key_and_keeps_comments(sandbox):
    repo, home = sandbox
    r = run("set-deepseek", "sk-abcdefghijklmnop", home=home, repo=repo)
    assert r.returncode == 0, r.stderr
    text = (repo / "configs" / "_global.yml").read_text(encoding="utf-8")
    assert '    api_key: "sk-abcdefghijklmnop"   # ← 填你自己的 DeepSeek key' in text
    assert "api_key: keep-me" in text          # 别的块的 api_key 不动
    assert "# 顶注释" in text and "🔴 独立于主模型" in text
    assert json.loads(run("status", home=home, repo=repo).stdout)["deepseek"] is True


def test_set_token_replaces_line_and_chmod_600(sandbox):
    repo, home = sandbox
    r = run("set-token", "123456789:AAtokentokentokentokentoken", home=home, repo=repo)
    assert r.returncode == 0, r.stderr
    env = home / ".claude" / "channels" / "chenlulu" / ".env"
    text = env.read_text(encoding="utf-8")
    assert text.count("TELEGRAM_BOT_TOKEN=") == 1
    assert "TELEGRAM_BOT_TOKEN=123456789:AAtokentokentokentokentoken" in text
    assert text.startswith("# 注释")
    if os.name != "nt":
        assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert json.loads(run("status", home=home, repo=repo).stdout)["token"] is True


def test_set_token_appends_when_missing(sandbox):
    repo, home = sandbox
    env = home / ".claude" / "channels" / "chenlulu" / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    assert run("set-token", "123456789:AAtokentokentokentokentoken", home=home, repo=repo).returncode == 0
    assert env.read_text(encoding="utf-8") == "OTHER=1\nTELEGRAM_BOT_TOKEN=123456789:AAtokentokentokentokentoken\n"


def test_set_userid_replaces_placeholder_keeps_other_keys(sandbox):
    repo, home = sandbox
    r = run("set-userid", "987654321", home=home, repo=repo)
    assert r.returncode == 0, r.stderr
    acc = json.loads((home / ".claude" / "channels" / "chenlulu" / "access.json").read_text(encoding="utf-8"))
    assert acc["allowFrom"] == ["987654321"]
    assert acc["dmPolicy"] == "allowlist" and acc["mentionPatterns"] == ["@x"]
    # 再填一个不同的：新的排最前，旧的保留
    assert run("set-userid", "111111111", home=home, repo=repo).returncode == 0
    acc = json.loads((home / ".claude" / "channels" / "chenlulu" / "access.json").read_text(encoding="utf-8"))
    assert acc["allowFrom"] == ["111111111", "987654321"]


def test_missing_value_and_missing_file_exit_codes(sandbox, tmp_path):
    repo, home = sandbox
    assert run("set-token", home=home, repo=repo).returncode == 2
    assert run("set-deepseek", "sk-x", home=home, repo=tmp_path / "nope").returncode == 3


def test_shell_guide_noninteractive_prints_where_and_how(sandbox):
    """stdin 不是 TTY 时只打说明不提问，且三项都说清"怎么拿""填哪"。"""
    repo, home = sandbox
    # 脚本 cd 到仓库根找 configs/_global.yml；这里把沙箱仓库的 configs 与 scripts 摆到一起
    (repo / "scripts").mkdir()
    for name in ("setup_keys.sh", "setup_keys.py"):
        (repo / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    r = subprocess.run(["bash", str(repo / "scripts" / "setup_keys.sh"), "--check"],
                       capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for must in ("@BotFather", "@userinfobot", "platform.deepseek.com", "TELEGRAM_BOT_TOKEN", "allowFrom", "jiwen.delta_llm.api_key"):
        assert must in out, f"说明里缺 {must}\n{out}"
    assert "👉" not in out  # --check 不提问
