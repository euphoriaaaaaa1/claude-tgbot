#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开版脱敏扫描（T24）。

只报告 **数量、路径、行号、规则名**，绝不打印命中的原文或密钥片段。

规则（PLAN §9.1）：
  home_path       /Users/<name>、/home/<name>、C:\\Users\\<name>
  username        wsxwj、*.wsxwj123.top
  telegram_token  <数字>:<30-64 位 token 字符>
  chat_id         9-10 位数字出现在 chat_id / allowFrom / user_id 等赋值位置
  api_key         sk-… / ghp_… / AIza… / Bearer …（长串）
  real_bot_name   yasuna / bot2 / bot3 / bot4 / lili / chenlulu / tuniang
  persona_keyword 陈露露 / 陈璐璐

白名单：
  - 测试夹具目录（tests/、dispatcher/tests/、voicecall/tests/）整体跳过；
  - 占位符值（yourname、YOUR_…、example、FAKE、123456789 等）跳过；
  - 源文件行尾写 ``scan-release: allow`` 的兼容性代码跳过（必须写明原因）。

用法：
    python3 scripts/scan_release.py [--root DIR] [--json]
退出码：0 = 0 命中；1 = 有命中；2 = 参数/根目录错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "venv", ".venv", "downloads", "dist", "build",
}
# 测试夹具里的假值按 T24 约定保留。测试目录整体不扫。
SKIP_PREFIXES = (
    "tests/", "dispatcher/tests/", "voicecall/tests/", ".devflow/",
)
SKIP_FILES = {
    "scripts/scan_release.py",  # 本脚本，规则本身含关键词
    "PROJECT.md",
}
SKIP_SUFFIXES = (".db", ".db-shm", ".db-wal", ".sqlite", ".png", ".jpg", ".jpeg",
                 ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2")

# ── 占位符白名单（小写比较） ───────────────────────────────────────────────
PATH_NAMES = {
    "you", "yourname", "your_name", "user", "username", "me", "example",
    "john", "john smith", "john_smith", "jane", "zhang", "alice", "bob",
    "test", "runner", "x", "name", "someone",
}
CHAT_IDS = {
    "123456789", "987654321", "111111111", "1234567890", "-1001234567890",
    "-1009999999999", "123456", "1700000000", "1700000000000",
}
TOKEN_OK = ("your-telegram-bot-token-here", "fake", "test", "example",
            "placeholder", "stdin-secret", "dummy")
KEY_OK = ("fake", "test", "example", "placeholder", "secret", "legacy", "leak",
          "xxx", "inst", "keep", "existing", "plain", "stdin", "your", "1234567890",
          "abcdefghijklmnop")

HOME_RE = re.compile(r"(?:/Users/|/home/|/root/)([^/\"'\s\\]+)")
WIN_HOME_RE = re.compile(r"(?i)[A-Z]:\\Users\\([^\\\"'\s]+)")
USERNAME_RE = re.compile(r"(?i)\bwsxwj\b|wsxwj123\.top")
TOKEN_RE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{30,64}\b")
CHAT_KEY_RE = re.compile(
    r"(?i)(?:chat[_-]?id|allowFrom|user[_-]?id|telegram_user_id|from_id|"
    r"TELEGRAM_CHAT_ID)\s*[\"']?\s*[:=]\s*(?:\[\s*)?[\"']?(-?\d{9,10})\b")
API_KEY_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
)
BOT_NAME_RE = re.compile(r"(?i)\b(yasuna|bot2|bot3|bot4|lili|chenlulu|tuniang)\b")
PERSONA_RE = re.compile(r"陈露露|陈璐璐")

ALLOW_MARK = "scan-release: allow"


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _scan_line(line: str):
    """返回该行命中的规则名列表（不含原文）。"""
    hits = []

    for m in HOME_RE.finditer(line):
        if m.group(1).strip().lower() not in PATH_NAMES:
            hits.append("home_path")
            break
    for m in WIN_HOME_RE.finditer(line):
        if m.group(1).strip().lower() not in PATH_NAMES:
            hits.append("home_path")
            break

    if USERNAME_RE.search(line):
        hits.append("username")

    for m in TOKEN_RE.finditer(line):
        if not any(ok in m.group(0).lower() for ok in TOKEN_OK):
            hits.append("telegram_token")
            break

    for m in CHAT_KEY_RE.finditer(line):
        if m.group(1) not in CHAT_IDS:
            hits.append("chat_id")
            break

    for rx in API_KEY_RES:
        found = False
        for m in rx.finditer(line):
            if not any(ok in m.group(0).lower() for ok in KEY_OK):
                hits.append("api_key")
                found = True
                break
        if found:
            break

    if BOT_NAME_RE.search(line):
        hits.append("real_bot_name")
    if PERSONA_RE.search(line):
        hits.append("persona_keyword")
    return hits


def scan(root: str):
    hits = []
    files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = _rel(full, root)
            if rel in SKIP_FILES or rel.startswith(SKIP_PREFIXES):
                continue
            # TS 单测/端到端夹具：与 Python tests/ 同等对待。
            if name.endswith(".test.ts") or name == "test-e2e.ts":
                continue
            if name.lower().endswith(SKIP_SUFFIXES):
                continue
            try:
                if os.path.getsize(full) > 2 * 1024 * 1024:
                    continue
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue
            text = raw.decode("utf-8", errors="ignore")
            files += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                if ALLOW_MARK in line:
                    continue
                for rule in _scan_line(line):
                    hits.append({"path": rel, "line": lineno, "rule": rule})
    return hits, files


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="公开版脱敏扫描（只输出数量/路径/形状）")
    p.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p.add_argument("--json", action="store_true", help="机器可读输出")
    a = p.parse_args(argv)

    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        print("根目录不存在：%s" % root, file=sys.stderr)
        return 2

    hits, files = scan(root)
    counts = Counter(h["rule"] for h in hits)
    if a.json:
        print(json.dumps({"scanned_files": files, "hits": hits,
                          "counts": dict(sorted(counts.items()))},
                         ensure_ascii=False, sort_keys=True))
    else:
        for h in hits:
            print("%s:%d [%s]" % (h["path"], h["line"], h["rule"]))
        print()
        print("扫描文件：%d；命中：%d" % (files, len(hits)))
        for rule in sorted(counts):
            print("  %-16s %d" % (rule, counts[rule]))
        if hits:
            print("FAIL：先按 PLAN §9.1 脱敏，或在确属占位/兼容的源文件行尾写 'scan-release: allow'。")
        else:
            print("OK：0 命中")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
