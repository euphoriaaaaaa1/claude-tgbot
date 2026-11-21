# -*- coding: utf-8 -*-
"""pub-nai-1121 · 路径默认值 / 令牌与画风写盘位置 / 仓库干净 / 脱敏（行为清单 1、3、4、5、6、24）。

写盘的用例（3、4、24）一律在仓库的临时副本里跑，真实仓库只读。
"""
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import pn_helpers as H

REPO_SKILLS = str(H.REPO / "skills")
REPO_SKILLS_REAL = os.path.realpath(REPO_SKILLS)


# 行为 1
def test_行为1_未设SKILLS_ROOT_image技能里novelai与comfyui引用都是仓库skills下的绝对路径():
    text = H.default_rendered()
    for name in ("novelai-skill", "comfyui-skill"):
        found = H.abs_prefixes(text, name)
        assert found, "渲染结果里没有任何指向 %s 的绝对路径" % name
        assert found <= {REPO_SKILLS, REPO_SKILLS_REAL}, \
            "%s 的引用前缀应是 %s，实际 %s" % (name, REPO_SKILLS, sorted(found))


def test_行为1_设了SKILLS_ROOT_image技能里两个引用都用设的值():
    alt = Path(H.tmp_home()) / "alt-skills"
    try:
        for name in ("novelai-skill", "comfyui-skill"):
            (alt / name).mkdir(parents=True)
        text = H.render_image_skill({"CLAUDEBOT_SKILLS_ROOT": str(alt)})
        for name in ("novelai-skill", "comfyui-skill"):
            assert H.abs_prefixes(text, name) == {str(alt)}, \
                "%s 的引用前缀应只有 %s，实际 %s" % (name, alt, sorted(H.abs_prefixes(text, name)))
    finally:
        shutil.rmtree(str(alt.parent), ignore_errors=True)


# 行为 3
def test_行为3_不设NOVELAI_SKILL_ROOT_设令牌写到仓库skills下的env_local且权限0600(tmp_path):
    repo = H.light_copy(tmp_path / "repo")
    cp = subprocess.run([sys.executable, "-B", "scripts/set_novelai_key.py"], cwd=str(repo), input=H.FAKE_TOKEN + "\n",
                        capture_output=True, text=True, timeout=60, env=H.clean_env(HOME=H.tmp_home()))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    env_file = repo / "skills" / "novelai-skill" / ".env.local"
    assert env_file.is_file(), "令牌没有写到 <仓库>/skills/novelai-skill/.env.local"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert H.FAKE_TOKEN in env_file.read_text(encoding="utf-8")


# 行为 4
def test_行为4_设令牌并保存一次画风后_仓库副本git_status为空(tmp_path):
    repo = H.light_copy(tmp_path / "repo", with_git=True)
    cp = subprocess.run([sys.executable, "-B", "scripts/set_novelai_key.py"], cwd=str(repo), input=H.FAKE_TOKEN + "\n",
                        capture_output=True, text=True, timeout=60, env=H.clean_env(HOME=H.tmp_home()))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    res = H.page_calls(repo, [("POST", "/api/styles", {"name": "状态检查用预设"})])
    assert res[0]["status"] == 200, res
    skill = repo / "skills" / "novelai-skill"
    assert (skill / ".env.local").is_file() and (skill / "assets" / "styles.local.json").is_file(), \
        "前提不成立：令牌或本地画风没有落在仓库副本的技能目录里"
    # 写盘过程中的两类临时文件也要被忽略
    (skill / ".env.local-abc123.tmp").write_text("x", encoding="utf-8")
    (skill / "assets" / ".styles.json-abc123.tmp").write_text("x", encoding="utf-8")
    status = H.git(repo, "status", "--porcelain")
    assert status == "", "副本里 git status 不为空：\n" + status


# 行为 5
def test_行为5_仓库内除测试夹具外搜不到旧的家目录技能路径():
    for needle in ("~/.claude/skills/novelai-skill", "~/.claude/skills/comfyui-skill"):
        cp = subprocess.run(
            ["git", "grep", "-l", "--untracked", "-F", "-e", needle, "--", ".",
             ":(exclude)tests", ":(exclude)dispatcher/tests", ":(exclude)voicecall/tests",
             ":(exclude,glob)**/restart-bots.sh*"],
            cwd=str(H.REPO), capture_output=True, text=True)
        assert cp.returncode == 1 and cp.stdout == "", "%s 仍出现在：\n%s" % (needle, cp.stdout)


# 行为 6
def test_行为6_SKILL_md里的出图命令一律以CLAUDEBOTLIFE_REPO开头():
    text = H.SKILL_MD.read_text(encoding="utf-8")
    tokens = [re.sub(r"^\w+=", "", t) for t in re.findall(r"(\S*)generate_novelai_image\.py", text)]
    tokens = [t for t in tokens if t.strip("`")]   # 正文里单独提到脚本名（没有路径）的不算命令
    assert tokens, "SKILL.md 里没有任何出图命令，无法验证"
    prefix = '"$CLAUDEBOTLIFE_REPO/skills/novelai-skill/'
    bad = [t for t in tokens if not t.startswith(prefix)]
    assert bad == [], "这些出图命令的脚本路径没有以 %s 开头：%s" % (prefix, bad)


# 行为 24
def test_行为24_scan_release脱敏扫描零命中(tmp_path):
    repo = H.light_copy(tmp_path / "repo")
    cp = subprocess.run([sys.executable, "-B", "scripts/scan_release.py", "--root", str(repo)], cwd=str(repo),
                        capture_output=True, text=True, timeout=300, env=H.clean_env(HOME=H.tmp_home()))
    assert cp.returncode == 0, "scan_release 退出码 %s（0=零命中）：\n%s" % (cp.returncode, (cp.stdout + cp.stderr)[-1500:])
