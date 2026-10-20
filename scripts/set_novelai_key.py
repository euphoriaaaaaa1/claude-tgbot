#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 NovelAI 令牌写入 novelai-skill 的 ``.env.local``。

用法：
    printf '%s\\n' "$NOVELAI_TOKEN" | python3 scripts/set_novelai_key.py
    python3 scripts/set_novelai_key.py --check   # 只输出 set: true|false

约束（INTERFACE §3.6）：
- 写入 ``$NOVELAI_SKILL_ROOT/.env.local``（默认
  ``~/.claude/skills/novelai-skill/.env.local``）的 ``NOVELAI_BEARER_TOKEN``；
- 同目录临时文件 + ``os.replace`` 原子替换，权限 0600；
- 全程不打印、不记录令牌；
- 退出码：0 成功；1 写失败/令牌为空；2 参数错。
"""
import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

NOVELAI_KEYS = ("NOVELAI_JWT", "NOVELAI_BEARER_TOKEN", "NOVELAI_TOKEN")


def _skill_root() -> Path:
    return Path(os.environ.get("NOVELAI_SKILL_ROOT")
                or os.path.expanduser("~/.claude/skills/novelai-skill"))


def _env_path() -> Path:
    return _skill_root() / ".env.local"


def _strip_export(line: str) -> str:
    s = line.strip()
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    return s


def _has_token(path: Path) -> bool:
    """文件里是否已有任一命名的非空令牌；读不出按未设置。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    for line in text.splitlines():
        s = _strip_export(line)
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        if key.strip() in NOVELAI_KEYS and value.strip().strip("'").strip('"'):
            return True
    return False


def _write_token(path: Path, token: str) -> None:
    """原子写入：保留无关行，删掉所有旧令牌行，再写一行 NOVELAI_BEARER_TOKEN。"""
    kept = []
    if path.exists():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            s = _strip_export(line)
            if "=" in s and s.split("=", 1)[0].strip() in NOVELAI_KEYS:
                continue  # 丢弃所有旧命名，避免优先级把新 key 盖掉
            kept.append(line)
    kept.append("NOVELAI_BEARER_TOKEN=%s" % token)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=".env.local-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(kept).strip() + "\n")
        os.chmod(tmp, 0o600)          # mkstemp 本就 0600；显式钉一次，防 umask 意外
        os.replace(tmp, path)         # os.replace 保留 tmp 的 0600
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把 NovelAI 令牌写入 novelai-skill 的 .env.local（0600）")
    parser.add_argument("--check", action="store_true",
                        help="只检查是否已设置，输出 set: true|false")
    args = parser.parse_args(argv)

    path = _env_path()
    if args.check:
        print("set: %s" % ("true" if _has_token(path) else "false"))
        return 0

    token = ""
    if not sys.stdin.isatty():
        token = (sys.stdin.readline() or "").strip()
    if not token:
        try:
            token = getpass.getpass("NovelAI token: ").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
    if not token:
        sys.stderr.write("令牌为空，未写入\n")
        return 1

    try:
        _write_token(path, token)
    except OSError as e:
        # 只报错误类别，绝不回显 token 或文件内容。
        sys.stderr.write("写入失败：%s\n" % (e.strerror or type(e).__name__))
        return 1
    sys.stderr.write("已写入 %s（权限 0600）\n" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
