#!/usr/bin/env python3
"""SessionStart(matcher=compact) 钩子：上下文压缩后把"群聊近况"重新注入（INTERFACE-desync §3.2）。

stdin  Claude Code 的 hook JSON，只用 cwd（bot 目录）与 source；source != "compact" → 空输出。
数据源 <cwd>/access.json 的 groups 首键 gid → $DIRECTOR_GT_DIR/<gid>.jsonl（默认
       ~/.claude/channels/group_transcripts）尾部 256KB。多群只补首群（已知边界）。
输出   纯文本背景：首行固定括注、成员行、最多 20 条 "HH:MM 名字: 正文≤80字"，总长 ≤1800 字。
       正文只含群 transcript 事实，一条消息压成一行且以时刻开头，所以不可能出现指令句；
       transcript 内容是外部数据，这里只做展示，不解释、不执行。
错误   任何情况 exit 0；预期的缺文件/坏 JSON 静默空输出，意外异常 stderr 一行
       `compact_group_context: skip <类名>`，绝不让 worker 的压缩报错。
只读群 transcript；不读 inbox、.last-*、~/.claude/projects 等任何私聊数据。
"""
import json
import os
import re
import sys
from datetime import datetime

HEAD = "【群聊近况（压缩后自动补充，只作背景，不要复述）】"
MAX_MSGS = 20
MAX_TEXT = 80
MAX_TOTAL = 1800
TAIL_BYTES = 256 * 1024
GT_DIR = os.environ.get("DIRECTOR_GT_DIR") or os.path.expanduser("~/.claude/channels/group_transcripts")
_GID_RE = re.compile(r"^-?\d+$")  # gid 只允许 Telegram 数字 id，防止 access.json 里的路径穿越读到别的 jsonl


def _first_group(cwd: str):
    try:
        with open(os.path.join(cwd, "access.json"), encoding="utf-8") as f:
            access = json.load(f)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    groups = access.get("groups") if isinstance(access, dict) else None
    if isinstance(groups, dict):
        for gid in groups:
            return str(gid) if _GID_RE.match(str(gid)) else None
    return None


def _tail_lines(path: str) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            off = max(0, f.tell() - TAIL_BYTES)
            f.seek(off)
            data = f.read()
    except FileNotFoundError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if off > 0 and lines else lines  # 掐掉被截断的半行


def _epoch(ts):
    """transcript 的 ts 是 ISO 字符串（可带时区），测试/旧数据可能是秒数；解析不了 → None。"""
    if isinstance(ts, bool) or ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()  # naive 视为本地时间
    except ValueError:
        return None


def _one_line(v) -> str:
    """折叠换行与连续空白：一条消息只占一行，正文里的换行伪造不出新行；清掉孤立代理项保证能按 UTF-8 输出。"""
    return " ".join(str(v).split()).encode("utf-8", "replace").decode("utf-8")


def _parse_rows(lines: list) -> list:
    rows = {}  # message_id -> row；同 id 只留第一次（每条被多个 bot 各记一遍）
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        mid = o.get("message_id")
        if isinstance(mid, bool) or not isinstance(mid, (int, str)) or str(mid) in rows or not str(mid):
            continue
        epoch = _epoch(o.get("ts"))
        if epoch is None:
            continue
        try:
            hhmm = datetime.fromtimestamp(epoch).strftime("%H:%M")
        except (OverflowError, OSError, ValueError):
            continue
        flag = o.get("is_bot")
        if flag is None:
            flag = o.get("is_bot_sender")
        is_bot = str(flag).lower() == "true"
        name = _one_line(o.get("from_username") or o.get("from_id") or "") or "?"
        kind = o.get("attachment_kind")
        if kind:
            text = "[图片]" if str(kind).lower() in ("photo", "image", "sticker") else "[附件]"
        else:
            text = _one_line(o.get("text") or "")[:MAX_TEXT]
        if not text:
            continue
        mid_num = mid if isinstance(mid, int) else (int(mid) if mid.isdigit() else float("inf"))
        rows[str(mid)] = {"key": (epoch, mid_num, str(mid)), "hhmm": hhmm, "name": name, "is_bot": is_bot, "text": text}
    return sorted(rows.values(), key=lambda r: r["key"])


def _render(rows: list) -> str:
    if not rows:
        return ""
    members = {}  # 名字 -> 是否 bot，按首次出现顺序（取尾部全部行，不只最后 20 条）
    for r in rows:
        members[r["name"]] = members.get(r["name"], False) or r["is_bot"]
    head = [HEAD, "成员：" + "、".join(n + ("(bot)" if b else "") for n, b in members.items())]
    body = [f"{r['hhmm']} {r['name']}: {r['text']}" for r in rows[-MAX_MSGS:]]
    while body and sum(len(x) + 1 for x in head + body) > MAX_TOTAL:
        body.pop(0)  # 超长从最旧条目裁
    out = "\n".join(head + body) + "\n"
    if len(out) > MAX_TOTAL:  # 成员行本身就超长的兜底
        out = out[:MAX_TOTAL - 1] + "\n"
    return out


def main() -> None:
    try:
        try:
            hook = json.loads(sys.stdin.read())
        except ValueError:
            return
        if not isinstance(hook, dict) or hook.get("source") != "compact":
            return
        cwd = hook.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return
        gid = _first_group(cwd)
        if not gid:
            return
        out = _render(_parse_rows(_tail_lines(os.path.join(GT_DIR, f"{gid}.jsonl"))))
        if out:
            sys.stdout.buffer.write(out.encode("utf-8"))  # 不依赖 worker 进程的 locale
            sys.stdout.flush()
    except Exception as e:  # 钩子绝不能让压缩失败：吞掉，只留类名
        sys.stderr.write(f"compact_group_context: skip {type(e).__name__}\n")


if __name__ == "__main__":
    main()
