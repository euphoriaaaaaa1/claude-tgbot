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
DEFAULT_BOT = "chenlulu"

#: 三把密钥的格式校验。**与 scripts/setup_keys.sh 里 ask 的正则逐字一致** —— 终端向导
#: 和 /hub/setup 网页向导必须一个口径。tests/unit/test_hub_setup.py 有一条用例直接从
#: .sh 源码里抠出这三条正则跟这里比对，两边改岔了就红。
FIELD_RE = {
    "token": re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{25,}$"),
    "userid": re.compile(r"^[0-9]{5,}$"),
    "deepseek": re.compile(r"^sk-[A-Za-z0-9_-]{16,}$"),
}
#: 校验失败时回给用户的格式说明（同样抄自 .sh）。**只描述形状，绝不回显用户输入的值。**
FIELD_FMT = {
    "token": "形如 123456789:AAxxxxxxxx，冒号前是数字",
    "userid": "纯数字，通常 8–10 位",
    "deepseek": "以 sk- 开头的一串字母数字",
}


def key_paths(bot: str = DEFAULT_BOT, repo=None, home=None) -> dict[str, Path]:
    """三把密钥各自的落点。CLI 与 moments/hub_routes.py 的 /hub/setup 共用这一份，
    别在调用方再算一遍——算岔了就是"网页说填好了、bot 读的还是空文件"。"""
    repo_dir = Path(repo) if repo else Path(__file__).resolve().parent.parent
    home_dir = Path(home) if home else Path(os.environ.get("HOME") or Path.home())
    bot_dir = home_dir / ".claude" / "channels" / bot
    return {"deepseek": repo_dir / "configs" / "_global.yml",
            "token": bot_dir / ".env",
            "userid": bot_dir / "access.json"}


def status(paths: dict[str, Path]) -> dict[str, bool]:
    """三项各自填了没（true = 已填）。CLI 的 status 子命令与网页 API 同一个来源。"""
    return {"deepseek": deepseek_filled(paths["deepseek"]),
            "token": token_filled(paths["token"]),
            "userid": userid_filled(paths["userid"])}


def write_key(field: str, value: str, paths: dict[str, Path]) -> None:
    """校验格式 → 写进对应文件。给 /hub/setup 用（CLI 的格式校验在 .sh 侧，路径不变）。

    🔴 抛出的异常消息里**绝不能出现 value**：它会被翻成 HTTP 响应体和日志。
    只抛错误码，人读文案由调用方从 FIELD_FMT 取。
    """
    if field not in FIELD_RE:
        raise ValueError("bad_field")
    v = (value or "").strip()
    if not FIELD_RE[field].match(v):
        raise ValueError("bad_format")
    if field == "deepseek":
        if not paths["deepseek"].exists():
            raise FileNotFoundError(paths["deepseek"])
        set_deepseek(paths["deepseek"], v)
    elif field == "token":
        if not paths["token"].parent.exists():        # .env 没有就建，bot 目录没有说明还没铺配置
            raise FileNotFoundError(paths["token"].parent)
        set_token(paths["token"], v)
    else:
        if not paths["userid"].exists():
            raise FileNotFoundError(paths["userid"])
        set_userid(paths["userid"], v)


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
        # 不用 SystemExit：它是 BaseException，在 Flask 请求线程里抛会绕过所有 except Exception，
        # 把 /hub/setup 的一次写入失败升级成整个 web 进程的意外退出。
        raise ValueError("configs/_global.yml 里没找到 jiwen.delta_llm.api_key 这一行（文件被改过结构？）")
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
    p.add_argument("--bot", default=DEFAULT_BOT)
    p.add_argument("--repo")
    p.add_argument("--home")
    a = p.parse_args(argv)
    paths = key_paths(a.bot, a.repo, a.home)
    yml, env, acc = paths["deepseek"], paths["token"], paths["userid"]
    if a.cmd == "status":
        print(json.dumps(status(paths)))
        return 0
    # value 传 "-" = 从 stdin 读：密钥放 argv 会进 `ps` 进程列表（多用户机上他人可见），
    # setup_keys.sh 一律走 stdin；argv 直传只留给单用户机手动救急。
    raw = sys.stdin.readline() if a.value == "-" else a.value
    if not raw or not raw.strip():
        print("缺少要写入的值", file=sys.stderr)
        return 2
    v = raw.strip()
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
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
