#!/usr/bin/env python3
"""把三把必填密钥写进各自的配置文件（install.sh 第④步 / scripts/setup_keys.sh 调用，也可手动跑）。

只做"写对地方"这一件事，不联网、不校验密钥真伪（格式在 shell 侧校验）。三个文件三种格式：
  configs/_global.yml            → jiwen.delta_llm.api_key（YAML；用行级替换，保留文件里的注释）
  ~/.claude/channels/<bot>/.env  → TELEGRAM_BOT_TOKEN=…（dotenv；有则替换、无则追加）
  ~/.claude/channels/<bot>/access.json → allowFrom（JSON；整体读写，其余键原样保留）

用法：
  python3 scripts/setup_keys.py status  [--bot chenlulu] [--repo <dir>] [--home <dir>]
      → 打印 JSON {"deepseek": true|false, "token": …, "userid": …}（true = 已填）
  python3 scripts/setup_keys.py set-deepseek <key>       [--repo <dir>]
  python3 scripts/setup_keys.py set-token   <token>      [--bot chenlulu] [--home <dir>]
  python3 scripts/setup_keys.py set-userid  <user_id>    [--bot chenlulu] [--home <dir>]
退出码：0 成功；2 参数错；3 目标文件不存在（先跑 install.sh 铺配置）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEEPSEEK_PLACEHOLDER = "YOUR_DEEPSEEK_API_KEY"
TOKEN_PLACEHOLDER_RE = re.compile(r"^TELEGRAM_BOT_TOKEN=\s*$|your-telegram-bot-token|YOUR", re.M)
USERID_PLACEHOLDER = "YOUR_TELEGRAM_USER_ID"


def _repo(args) -> Path:
    return Path(args.repo or Path(__file__).resolve().parent.parent)


def _bot_dir(args) -> Path:
    home = Path(args.home or os.environ.get("HOME") or Path.home())
    return home / ".claude" / "channels" / args.bot


# ── DeepSeek key：YAML 行级替换，只动 delta_llm 块里的 api_key 那一行 ─────────────
def set_deepseek(path: Path, key: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    in_block = False
    done = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if re.match(r"delta_llm\s*:", stripped):
            in_block = True
            block_indent = indent
            continue
        if in_block and stripped and not stripped.startswith("#") and indent <= block_indent:
            in_block = False  # 离开 delta_llm 块
        if in_block:
            m = re.match(r"(\s*api_key\s*:\s*)(\"[^\"]*\"|'[^']*'|[^#\n]*?)(\s*(#.*)?)$", line)
            if m:
                lines[i] = f'{m.group(1)}"{key}"{m.group(3)}'
                done = True
                break
    if not done:
        raise SystemExit("configs/_global.yml 里没找到 jiwen.delta_llm.api_key 这一行（文件被改过结构？）")
    path.write_text("\n".join(lines), encoding="utf-8")


def deepseek_filled(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    if DEEPSEEK_PLACEHOLDER in text:
        return False
    m = re.search(r"^\s*api_key\s*:\s*(\"([^\"]*)\"|'([^']*)'|([^#\n]*?))\s*(#.*)?$", text, re.M)
    val = (m.group(2) or m.group(3) or m.group(4) or "").strip() if m else ""
    return bool(val)


# ── bot token：dotenv ──────────────────────────────────────────────────────
def set_token(path: Path, token: str) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"TELEGRAM_BOT_TOKEN={token}"
    if re.search(r"^TELEGRAM_BOT_TOKEN=.*$", text, re.M):
        text = re.sub(r"^TELEGRAM_BOT_TOKEN=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def token_filled(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    m = re.search(r"^TELEGRAM_BOT_TOKEN=(.*)$", text, re.M)
    if not m:
        return False
    val = m.group(1).strip()
    return bool(val) and not TOKEN_PLACEHOLDER_RE.search(m.group(0))


# ── user_id：JSON allowFrom ───────────────────────────────────────────────
def set_userid(path: Path, user_id: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    allow = [x for x in data.get("allowFrom", []) if str(x) not in (USERID_PLACEHOLDER, user_id, "")]
    data["allowFrom"] = [user_id] + allow  # 新填的排最前；已有的其它白名单保留
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def userid_filled(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    allow = [str(x) for x in data.get("allowFrom", [])]
    return any(x and x != USERID_PLACEHOLDER for x in allow)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["status", "set-deepseek", "set-token", "set-userid"])
    p.add_argument("value", nargs="?")
    p.add_argument("--bot", default="chenlulu")
    p.add_argument("--repo")
    p.add_argument("--home")
    a = p.parse_args(argv)
    yml = _repo(a) / "configs" / "_global.yml"
    env = _bot_dir(a) / ".env"
    acc = _bot_dir(a) / "access.json"
    if a.cmd == "status":
        print(json.dumps({"deepseek": deepseek_filled(yml), "token": token_filled(env), "userid": userid_filled(acc)}))
        return 0
    if not a.value or not a.value.strip():
        print("缺少要写入的值", file=sys.stderr)
        return 2
    v = a.value.strip()
    try:
        if a.cmd == "set-deepseek":
            if not yml.exists():
                print(f"{yml} 不存在，先跑 bash install.sh 铺配置", file=sys.stderr); return 3
            set_deepseek(yml, v)
        elif a.cmd == "set-token":
            if not acc.parent.exists():
                print(f"{acc.parent} 不存在，先跑 bash install.sh 铺配置", file=sys.stderr); return 3
            set_token(env, v)
        elif a.cmd == "set-userid":
            if not acc.exists():
                print(f"{acc} 不存在，先跑 bash install.sh 铺配置", file=sys.stderr); return 3
            set_userid(acc, v)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
