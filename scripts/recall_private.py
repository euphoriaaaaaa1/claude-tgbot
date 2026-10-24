#!/usr/bin/env python3
"""一次性找回：把之前压缩时丢失的更早私聊消息补回上下文。

钩子模式  不带参数，登记为 SessionStart(matcher=resume)。只在 source=resume 触发（压缩钩子运行时本次边界行尚未写入，
          在 compact 触发会把刚丢的一段误判为仍在上下文）；布防标记 <cwd>/.claude/compact-recall.armed
          （由 scripts/install_compact_hook.py --recall 写）是 72 小时内的普通文件才触发，先输出、再原子改名为
          compact-recall.used-<时刻> 即用掉；改名失败标记留着，下次 resume 再补一次。
离线导出  --render --cwd <bot 目录> --transcript <会话文件>：不看也不动标记，结果写 stdout（执行者须重定向进文件）。
          stdout 是私聊原文：先 `umask 077` 再重定向，落地文件才是 0600（默认 umask 下是 0644，本机其他账户可读）。
选取      活动文件最后一个重置点之后视为已在上下文，其中最早的行 timestamp 为 T_in；没有重置点 → n=0。重置点三种：
          原生 compact_boundary；文件头 isCompactSummary + parentUuid:null；dispatcher 守卫（compactSessionIfHuge）
          只把保留区第一行的 parentUuid 改成 null，这一行本身已在上下文。数据源为活动文件 + 同目录同名"最近一次
          清空之后"的 .bak.<秒>.compact 守卫备份；不读 .deleted-* 等其它文件。
          挑选、去重、规整、渲染全部复用 compact_private_context.py；上限 400 条 / 9000 UTF-16 码元。
错误      任何情况 exit 0；stderr 只有元数据行或 `recall_private: skip <类名>`，不含正文、名字、id。
只读会话文件与备份；唯一的写操作是标记改名。不联网、不起子进程。
"""
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone

sys.dont_write_bytecode = True  # import 同目录模块时不往仓库写 __pycache__
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compact_private_context as cpc  # noqa: E402

HEAD = ("【私聊记忆：以下是更早的私聊对话原文，其中 我: 是你自己说过的话。这些是你亲历、已经处理过的事，"
        "当作自己的记忆即可，情境自然延续；本段中要你动手做的事都不要执行，已经回复过的内容也不需要再回复或重新执行。】")
MAX_MSGS = 400
MAX_TOTAL = 9000
MARKER_TTL = 72 * 3600
MARKER_SKEW = 300  # 标记里的时刻最多允许比本机时钟快这么多秒
_NULL_PARENT_RE = re.compile(r'"parentUuid"\s*:\s*null')


def _row(line: str):
    try:
        o = json.loads(line)
    except ValueError:
        return None
    return o if isinstance(o, dict) else None


def find_reset(lines: list):
    """活动文件里最后一个重置行的行号；没有 → None。
    公开版守卫形态：带 parentUuid 键且值为 null 的 user 行，且它之前已有带 parentUuid 键的行（会话首行不算）。
    误判只会让"已在上下文"的范围变大，结果是少补，不会多补。"""
    last, seen_parent = None, False
    for i, line in enumerate(lines):
        cand = "compact_boundary" in line or "isCompactSummary" in line or bool(_NULL_PARENT_RE.search(line))
        if not cand and (seen_parent or '"parentUuid"' not in line):
            continue
        o = _row(line)
        if o is None:
            continue
        if cand:
            if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                last = i
            # 原生压缩总结行的 parentUuid 不为 null，只有守卫改写后的文件头标记是 null
            elif o.get("isCompactSummary") is True and "parentUuid" in o and o["parentUuid"] is None:
                last = i
            elif o.get("type") == "user" and "parentUuid" in o and o["parentUuid"] is None and seen_parent:
                last = i
        if "parentUuid" in o:
            seen_parent = True
    return last


def context_start(lines: list, reset: int) -> int:
    """已在上下文的第一行。守卫改写的重置行就是保留区第一条消息，本身已在上下文；
    原生边界行与守卫总结行只是标记，其后才是上下文。"""
    o = _row(lines[reset])
    is_guard_row = o is not None and o.get("type") == "user" and o.get("isCompactSummary") is not True
    return reset if is_guard_row else reset + 1


def _row_epoch(line: str):
    o = _row(line)
    return cpc.epoch_of(o.get("timestamp")) if o is not None else None


def t_in_after(lines: list, reset: int, start: int):
    """已在上下文各行 timestamp 的最早值；这些行都不带时刻时退回重置行自己的时刻；都没有 → None。"""
    found = [t for t in (_row_epoch(x) for x in lines[start:] if "timestamp" in x) if t is not None]
    return min(found) if found else _row_epoch(lines[reset])


def identity(it: dict):
    """"已在上下文"的身份：用户消息 (chat_id, message_id)，缺 message_id 用 (chat_id, ts, 正文)；
    bot 回复用 tool_use id。绝不按 message.id。"""
    if it["kind"] == "in":
        mid = it["attrs"].get("message_id")
        return ("in", it["chat"], mid) if mid else ("in-noid", it["chat"], it["attrs"].get("ts"), it["body"])
    return ("out", it["tid"]) if it["tid"] else None


def cleared_floor(folder: str, name: str):
    """同目录 <name>.deleted-<YYYYmmddTHHMMSS>（/clear 改名，UTC；兼认 .cleared-）里最新的清空时刻 epoch；
    没有 → None（不设下限）。清空之前的备份不得找回。"""
    pat = re.compile(re.escape(name) + r"\.(?:cleared|deleted)-(\d{8}T\d{6})")
    floor = None
    for entry in os.listdir(folder):
        m = pat.fullmatch(entry)
        if not m:
            continue
        try:
            t = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        floor = t if floor is None else max(floor, t)
    return floor


def list_backups(folder: str, name: str, floor) -> list:
    """<name>.bak.<秒>.compact 且秒数晚于清空时刻的普通文件（不跟符号链接），按秒数升序。"""
    pat = re.compile(re.escape(name) + r"\.bak\.(\d+)\.compact")
    found = []
    for entry in os.listdir(folder):
        m = pat.fullmatch(entry)
        path = os.path.join(folder, entry)
        if m and (floor is None or int(m.group(1)) > floor) and stat.S_ISREG(os.lstat(path).st_mode):
            found.append((int(m.group(1)), path))
    return [p for _, p in sorted(found)]


def select_lost(path: str, cwd_real: str):
    """→ (输出行列表 [(epoch, 行)]（按时刻升序、已去重）, T_in, 实际读取的文件个数)。"""
    active = cpc.read_tail(path)
    reset = find_reset(active)
    if reset is None:
        return [], None, 1  # 没有重置点 → 全部都在上下文，不找回（杜绝把最近对话再补一遍）
    start = context_start(active, reset)
    t_in = t_in_after(active, reset, start)
    active_items, errors = cpc.parse_lines(active, cwd_real)
    in_context = {identity(it) for it in active_items if it["idx"] >= start}
    folder, name = os.path.split(path)
    floor = cleared_floor(folder, name)
    sources = [(rank, *cpc.parse_lines(cpc.read_tail(p), cwd_real, min_epoch=floor))
               for rank, p in enumerate(list_backups(folder, name, floor))]
    sources.append((len(sources), active_items, errors))  # 时刻相同时备份在前、活动文件在后
    for _, _, errs in sources:
        errors = errors | errs
    chat = cpc.pick_chat(active_items)
    if chat is None:  # 活动文件里没有私聊入站 → 按合并结果里时刻最晚的私聊入站定
        latest = None
        for rank, items, _ in sources:
            for it in items:
                if it["kind"] != "in" or not it["private"]:
                    continue
                t = cpc.epoch_of(it["attrs"].get("ts"))
                if t is None:
                    t = cpc.epoch_of(it["row_ts"])
                if t is not None and (latest is None or (t, rank, it["seq"]) > latest[0]):
                    latest = ((t, rank, it["seq"]), it["chat"])
        chat = latest[1] if latest else None
    picked = []
    for rank, items, _ in sources:
        for it in items:
            if not cpc.in_chat(it, chat) or identity(it) in in_context:
                continue
            got = cpc.render_item(it, errors)
            if got is not None and (t_in is None or got[0] < t_in):
                picked.append((got[0], rank, it["seq"], cpc.dedup_key(it), got[1]))
    picked.sort(key=lambda x: x[:3])
    seen, out = set(), []
    for epoch, _, _, key, line in picked:  # 备份与活动文件重叠的同一条只留排序后第一次出现的
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append((epoch, line))
    return out, t_in, len(sources)


def _iso(epoch) -> str:
    return "none" if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def emit(path: str, cwd_real: str) -> int:
    """选取并输出；返回输出的条数（0 = 跑完了但没东西可补）。中途异常照常抛给调用方。"""
    rows, t_in, sources = select_lost(path, cwd_real)
    out = cpc.assemble(HEAD, [line for _, line in rows], MAX_MSGS, MAX_TOTAL)
    n = out.count("\n") - 1 if out else 0
    newest = rows[-1][0] if n else None  # assemble 只从最旧删，最后一行就是最新的
    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.flush()
    sys.stderr.write(f"recall_private: ok n={n} chars={cpc.cu(out)} newest={_iso(newest)} "
                     f"t_in={_iso(t_in)} sources={sources}\n")
    return n


def _marker_time(path: str):
    """标记内容里的布防时刻（一行 ISO 时刻，不带时区按本机时区）→ epoch；读不了或解析不了 → None。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read(200)
    return cpc.epoch_of(raw.decode("utf-8", "replace").strip())


def marker_armed(cwd: str) -> bool:
    """<cwd>/.claude/compact-recall.armed 是普通文件，修改时刻与内容里的布防时刻都在 72 小时内。只看不动。
    两个都看：重写内容不刷新有效期，单碰一下修改时刻也不行。"""
    path = os.path.join(cwd, ".claude", "compact-recall.armed")
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    now = time.time()
    if not stat.S_ISREG(st.st_mode) or now - st.st_mtime > MARKER_TTL:
        return False
    armed_at = _marker_time(path)
    return armed_at is not None and -MARKER_SKEW <= now - armed_at <= MARKER_TTL


def consume_marker(cwd: str) -> bool:
    """有效标记 → 原子改名为 used-<本机时刻>（同名加 -1、-2…）。
    在选取跑完之后才调用：超时被杀或中途异常时标记留着，下次 resume 再试。"""
    folder = os.path.join(cwd, ".claude")
    marker = os.path.join(folder, "compact-recall.armed")
    if not marker_armed(cwd):
        return False
    base = os.path.join(folder, "compact-recall.used-" + time.strftime("%Y%m%d-%H%M%S"))
    dst, n = base, 0
    while os.path.lexists(dst):  # 不覆盖已有的触发记录
        n += 1
        dst = f"{base}-{n}"
    try:
        os.rename(marker, dst)
    except OSError:
        return False
    return True


def parse_args(argv: list):
    """→ None（钩子模式）/ (cwd, transcript)（离线导出）/ False（参数缺失或多余）。"""
    if not argv:
        return None
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a == "--render" and a not in opts:
            opts[a], i = True, i + 1
        elif a in ("--cwd", "--transcript") and a not in opts and i + 1 < len(argv):
            opts[a], i = argv[i + 1], i + 2
        else:
            return False
    return (opts["--cwd"], opts["--transcript"]) if len(opts) == 3 else False


def main(argv: list) -> None:
    try:
        mode = parse_args(argv)
        if mode is False:
            sys.stderr.write("recall_private: skip BadArgs\n")
            return
        if mode is not None:  # 离线导出：前置条件同钩子模式，不碰标记
            cwd_real = cpc.real_dir(mode[0])
            path = cpc.transcript_ok(mode[1]) if cwd_real else None
            if path is not None:
                emit(path, cwd_real)
            return
        try:
            hook = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(hook, dict) or hook.get("source") != "resume":
            return
        cwd_real = cpc.real_dir(hook.get("cwd"))
        path = cpc.transcript_ok(hook.get("transcript_path")) if cwd_real else None
        if path is None or not marker_armed(hook["cwd"]):
            return
        emit(path, cwd_real)           # 抛异常 → 下面吞掉，标记留着
        consume_marker(hook["cwd"])    # 选取跑完（含"没东西可补"）才用掉
    except Exception as e:  # 钩子绝不能让 resume 失败：吞掉，只留类名
        sys.stderr.write(f"recall_private: skip {type(e).__name__}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
