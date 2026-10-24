#!/usr/bin/env python3
"""把四条 SessionStart 钩子合并进某个 bot 目录的项目级 .claude/settings.json（四条的唯一定义处，新建 bot 也从这里取）。

用法    python3 scripts/install_compact_hook.py ~/.claude/channels/<bot名> [--dry-run] [--recall]
        （bot 目录即 worker 的 cwd；Windows 在 %USERPROFILE%\\.claude\\channels\\<bot名>，改过 HUB_CHANNELS_DIR 的以它为准）
四条    按此顺序（见 HOOKS）：compact → compact_group_context.py（压缩后补回群聊）；compact → compact_private_context.py
        （压缩后补回私聊）；resume → recall_private.py（布防后的一次性找回，没布防什么都不做）；startup → clear_recap.py
        （/clear 后回顾清空前的摘要）。每条写成 {"matcher":…,"hooks":[{"type":"command",
          "command":"<运行本脚本的解释器绝对路径> <本仓>/scripts/<脚本>","timeout":10}]}
        两段路径各自按需加引号（见 quote_hook_path）：POSIX 只在含空格/shell 元字符时加（shlex.quote），
        Windows 一律加双引号——解释器默认装在 %LOCALAPPDATA%\\Programs\\Python 下（含用户名），用户名或仓路径
        含空格时裸拼接会被按空格切开；Windows 宿主用 cmd.exe 还是 Git Bash 跑 hook 未坐实，双引号两者都认，
        而不加引号的反斜杠路径在 POSIX shell 下会被吃成 C:Usersx。不含特殊字符的路径形态不变。
规则    幂等，四条同一套：按（matcher，脚本文件名）认同一项。"已装"指只有一条，它的脚本路径（去引号、解析链接）等于本仓
        脚本，且第一段（解释器）是存在的文件——解释器不同但都存在不算变化（新建 bot 用适配器进程的解释器、补装用 shell 里的
        python，逐字比较会每跑一次就替换、备份一次）；否则替换成当前解释器加本仓路径（多条并成一条）；没有就追加。
        模板 bot 自带的 `python3 "$CLAUDEBOTLIFE_REPO/scripts/compact_group_context.py"` 也按此认作同一项并替换成绝对路径；
        仓库挪了位置重跑一次即修好。只动 hooks.SessionStart，其它顶层键与其它事件原样保留。
        写前备份 settings.json.bak-desync-<ts>，同目录 tmp + rename 原子写。stdout 只打 hooks.SessionStart 的 diff，
        不是本安装器管理的钩子显示为占位（别人可能把凭证内联进命令；settings 其它键如 env 一个字符都不打印）。
        --dry-run 只打印，不写、不备份、不布防。
--recall 确保四条在之外，写一次性找回的布防标记 <bot_dir>/.claude/compact-recall.armed（一行本机时区 ISO 时刻，0600，
        已存在即刷新，72 小时内的下一次 resume 用掉）。标记先于 settings 写：标记写失败时 settings 还没动。
退出码  0 成功/已装；2 无 .claude 目录或无 settings.json（不创建）；3 JSON 非法或结构不合预期（不写不备份）；
        4 目标解析后是全局 ~/.claude/settings.json（任何情况不写）；5 标记/备份/写入失败（原文件字节不变）。
只写各 bot 目录的项目级 settings 与布防标记；全局 ~/.claude/settings.json 一个字节不碰。
"""
import argparse
import difflib
import json
import os
import shlex
import shutil
import sys
import time
from datetime import datetime

try:
    import pwd  # 仅 POSIX；Windows 没有，家目录只能信 USERPROFILE
except ImportError:
    pwd = None

SCRIPT_NAME = "compact_group_context.py"
PRIVATE_NAME = "compact_private_context.py"
RECALL_NAME = "recall_private.py"
RECAP_NAME = "clear_recap.py"
HOOKS = (("compact", SCRIPT_NAME), ("compact", PRIVATE_NAME), ("resume", RECALL_NAME), ("startup", RECAP_NAME))
MANAGED = tuple(script for _, script in HOOKS)
HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_NAME)
MARKER_NAME = "compact-recall.armed"
HIDDEN = "<hook not managed here, hidden>"


def quote_hook_path(path: str, windows: bool = (os.name == "nt")) -> str:
    """hook command 里的一段路径。Windows：一律双引号（cmd.exe 与 Git Bash 都认，Windows 路径不可能含 "，
    且能保住反斜杠）；POSIX：shlex.quote，安全字符集内原样、否则单引号。"""
    if windows:
        return f'"{path}"'
    return shlex.quote(path)


def script_path(script: str = SCRIPT_NAME) -> str:
    """本仓脚本的绝对路径；四个脚本与群聊脚本同目录（HOOK_SCRIPT 换了位置，其余跟着走）。"""
    return HOOK_SCRIPT if script == SCRIPT_NAME else os.path.join(os.path.dirname(HOOK_SCRIPT), script)


def hook_command(windows: bool = (os.name == "nt"), script: str = SCRIPT_NAME) -> str:
    return f"{quote_hook_path(sys.executable, windows)} {quote_hook_path(script_path(script), windows)}"


def hook_item(matcher: str = "compact", script: str = SCRIPT_NAME) -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command(script=script), "timeout": 10}]}


def _homes() -> set:
    homes = {os.path.expanduser("~")}
    if pwd is not None:  # HOME 被改写（env -i、sudo、测试）时照样护住账户真实家目录下的全局配置
        homes.add(pwd.getpwuid(os.getuid()).pw_dir)
    return homes


def is_global(target: str) -> bool:
    """解析符号链接后，目标就是 ~/.claude/settings.json 或父目录是 ~/.claude → 全局配置。
    normcase：Windows 路径大小写不敏感，C:\\Users 与 c:\\users 是同一处。"""
    t = os.path.normcase(os.path.realpath(target))
    for home in _homes():
        g = os.path.normcase(os.path.join(os.path.realpath(home), ".claude"))
        if t == os.path.join(g, "settings.json") or os.path.dirname(t) == g:
            return True
    return False


def validate(settings) -> str:
    """结构不合预期 → 返回原因；合法 → 空串。"""
    if not isinstance(settings, dict):
        return "top level is not an object"
    if "hooks" in settings and not isinstance(settings["hooks"], dict):
        return "hooks is not an object"
    ss = (settings.get("hooks") or {}).get("SessionStart")
    if "hooks" in settings and "SessionStart" in settings["hooks"] and not isinstance(ss, list):
        return "hooks.SessionStart is not an array"
    if isinstance(ss, list) and not all(isinstance(it, dict) for it in ss):
        return "hooks.SessionStart has a non-object item"
    if isinstance(ss, list) and not all(isinstance(it.get("hooks", []), list) for it in ss):
        return "hooks.SessionStart item has non-array hooks"  # 否则遍历它会 TypeError 崩成 exit 1
    return ""


def _tokens(cmd: str) -> list:
    """command 拆成 token 并去掉两端引号。POSIX 按 shell 规则（认 shlex.quote 加的单引号，带空格的路径是一整段）；
    Windows 不把反斜杠当转义。引号不成对拆不开时退回按空白切。"""
    try:
        toks = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        toks = cmd.split()
    return [t.strip("\"'") for t in toks]


def _script_command(item: dict, script: str):
    """该 SessionStart 项里脚本文件名为 script 的 command；没有 → None。"""
    for h in item.get("hooks") or []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and any(os.path.basename(tok) == script for tok in _tokens(cmd)):
            return cmd
    return None


def _our_command(item: dict):
    """群聊项的 command（保留旧名：模板写法与带引号的路径都要认得）。"""
    return _script_command(item, SCRIPT_NAME)


def _is_current(cmd: str, script: str) -> bool:
    """已装：脚本那段是绝对路径、解析链接后等于本仓脚本，且第一段（解释器）是存在的文件。"""
    toks = _tokens(cmd)
    hits = [i for i, tok in enumerate(toks) if os.path.basename(tok) == script]
    if not hits or hits[-1] == 0:
        return False
    tok = toks[hits[-1]]
    if not os.path.isabs(tok):
        return False
    same = os.path.normcase(os.path.realpath(tok)) == os.path.normcase(os.path.realpath(script_path(script)))
    return same and os.path.isfile(toks[0])


def merge_all(settings: dict) -> tuple:
    """返回 (合并后的新对象, 变化行列表)；不改入参、不触盘、不检查脚本是否存在。调用前须 validate 通过。
    变化行形如 `SessionStart[<matcher>] <installed|replaced>: <command>`，按四条顺序；四条都已装 → 空列表。"""
    new = json.loads(json.dumps(settings))  # 来自 JSON 的对象，round-trip 即深拷贝
    ss = new.setdefault("hooks", {}).setdefault("SessionStart", [])
    changes = []
    for matcher, script in HOOKS:
        ours = [i for i, it in enumerate(ss) if it.get("matcher") == matcher and _script_command(it, script) is not None]
        if len(ours) == 1 and _is_current(_script_command(ss[ours[0]], script), script):
            continue
        item = hook_item(matcher, script)
        if not ours:
            ss.append(item)
            action = "installed"
        else:
            ss[ours[0]] = item
            for i in reversed(ours[1:]):  # 同一脚本多条（不该有）合并成一条
                del ss[i]
            action = "replaced"
        changes.append(f"SessionStart[{matcher}] {action}: {item['hooks'][0]['command']}")
    return new, changes


def _diff_view(settings: dict) -> list:
    """diff 用的 hooks.SessionStart 视图：每项只留 matcher 与 hooks；不是本安装器管理的钩子（command 里没有四个脚本名
    之一）整条换成占位——别人可能把凭证内联进命令或 url，diff 的上下文行不能把它带到终端。"""
    def managed(h):
        cmd = h.get("command") if isinstance(h, dict) else None
        return isinstance(cmd, str) and any(os.path.basename(tok) in MANAGED for tok in _tokens(cmd))

    view = []
    for it in (settings.get("hooks") or {}).get("SessionStart") or []:
        hooks = it.get("hooks") if isinstance(it.get("hooks"), list) else []
        shown = {"matcher": it["matcher"]} if "matcher" in it else {}
        shown["hooks"] = [h if managed(h) else HIDDEN for h in hooks]
        view.append(shown)
    return view


def _write_marker(path: str) -> None:
    """找回布防标记：一行本机时区 ISO 时刻，0600；同目录临时文件 + 改名（已存在就覆盖，刷新 72 小时有效期）。
    临时文件 O_EXCL|O_NOFOLLOW（Windows 没有 O_NOFOLLOW，取 0）：同名路径若预先放了文件或链接就失败，不顺着链接写。"""
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(datetime.now().astimezone().isoformat(timespec="seconds") + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_path(target: str) -> str:
    base = f"{target}.bak-desync-{time.strftime('%Y%m%d-%H%M%S')}"
    path, n = base, 0
    while os.path.exists(path):  # 同一秒内重复安装不覆盖上一份备份
        n += 1
        path = f"{base}-{n}"
    return path


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="merge the four SessionStart hooks into <bot_dir>/.claude/settings.json")
    ap.add_argument("bot_dir")
    ap.add_argument("--dry-run", action="store_true", help="only print the diff; no backup, no write, no marker")
    ap.add_argument("--recall", action="store_true", help="also arm the one-shot recall for the next resume")
    args = ap.parse_args(argv)

    bot_dir = os.path.realpath(args.bot_dir)
    cfg_dir = os.path.join(bot_dir, ".claude")
    target = os.path.join(cfg_dir, "settings.json")
    marker = os.path.join(cfg_dir, MARKER_NAME)
    if is_global(target):
        print("refuse: global settings")
        return 4
    if not os.path.isdir(cfg_dir):
        print("no project settings dir, skip")
        return 2
    if not os.path.isfile(target):
        print("no settings.json, skip")
        return 2
    try:
        with open(target, encoding="utf-8") as f:
            settings = json.loads(f.read())
    except ValueError as e:  # 含 UnicodeDecodeError：非 UTF-8 也是非法 JSON，按 exit 3 报而不是 traceback
        print(f"bad settings.json: {type(e).__name__}")
        return 3
    why = validate(settings)
    if why:
        print(f"bad settings.json: {why}")
        return 3

    new, changes = merge_all(settings)
    if not changes:
        print(f"already installed: {target}")
        if args.recall and args.dry_run:
            print("dry-run: recall not armed")
        elif args.recall:
            try:
                _write_marker(marker)
            except OSError as e:
                print(f"write failed: {type(e).__name__}")
                return 5
            print(f"recall armed: {marker}")
        return 0
    diff = difflib.unified_diff(_dump(_diff_view(settings)).splitlines(), _dump(_diff_view(new)).splitlines(),
                                "hooks.SessionStart (before)", "hooks.SessionStart (after)", lineterm="")
    print("\n".join(diff))
    print("\n".join(changes))
    if args.dry_run:
        print("dry-run: nothing written")
        if args.recall:
            print("dry-run: recall not armed")
        return 0

    tmp = f"{target}.tmp-{os.getpid()}"
    try:
        if args.recall:  # 先布防后写 settings：标记写失败时 settings 与备份都还没动
            _write_marker(marker)
            print(f"recall armed: {marker}")
        bak = _backup_path(target)
        shutil.copy2(target, bak)  # 连权限位一起拷：0600 的配置备份出来不能变成人人可读
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_dump(new) + "\n")
        shutil.copymode(target, tmp)  # 新文件沿用原权限位，不按 umask 放宽
        os.replace(tmp, target)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"write failed: {type(e).__name__}")
        return 5
    print(f"backup: {bak}")
    print(f"written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
