# -*- coding: utf-8 -*-
"""CLAUDEBOT_DB_PATH 覆盖：测试/门禁可以把状态库放到临时目录。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _db_path(extra_env: dict) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDEBOT_DB_PATH"}
    env.update(extra_env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-c", "import db; print(db.DB_PATH)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_db_path_env_override(tmp_path):
    target = tmp_path / "nested" / "state.db"
    assert _db_path({"CLAUDEBOT_DB_PATH": str(target)}) == os.path.abspath(str(target))


def test_db_path_default_is_repo_root():
    assert _db_path({}) == os.path.join(str(ROOT), "state.db")
