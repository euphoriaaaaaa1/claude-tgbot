#!/usr/bin/env python3
"""把 SessionStart(matcher=compact) 钩子合并进某个 bot 目录的项目级 .claude/settings.json（INTERFACE-desync §3.3）。

用法    python3 scripts/install_compact_hook.py ~/.claude/channels/<bot名> [--dry-run]
        （bot 目录即 worker 的 cwd；Windows 在 %USERPROFILE%\\.claude\\channels\\<bot名>，改过 HUB_CHANNELS_DIR 的以它为准）
写入项  {"matcher":"compact","hooks":[{"type":"command",
          "command":"<运行本脚本的解释器绝对路径> <本仓>/scripts/compact_group_context.py","timeout":10}]}
规则    幂等：已有 command 里脚本 basename 为 compact_group_context.py 的 compact 项时，command 逐字相同 →
        already installed 不改；不同 → 替换该项（结果仍只一条）；没有 → 追加。模板 bot 自带的
        `python3 "$CLAUDEBOTLIFE_REPO/scripts/compact_group_context.py"` 也按此认作同一项并替换成绝对路径，不会装出第二条。
        只动 hooks.SessionStart，其它顶层键与其它事件原样保留。写前备份 settings.json.bak-desync-<ts>，
        同目录 tmp + rename 原子写，stdout 打合并前后 diff。--dry-run 只打印，不写、不备份。
退出码  0 成功/已装；2 无 .claude 目录或无 settings.json（不创建）；3 JSON 非法或结构不合预期（不写不备份）；
        4 目标解析后是全局 ~/.claude/settings.json（任何情况不写）；5 备份/写入失败（原文件字节不变）。
只写各 bot 目录的项目级 settings；全局 ~/.claude/settings.json 一个字节不碰。
"""
import argparse
import difflib
import json
import os
import shutil
import sys
import time

try:
    import pwd  # 仅 POSIX；Windows 没有，家目录只能信 USERPROFILE
except ImportError:
    pwd = None

SCRIPT_NAME = "compact_group_context.py"
HOOK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCRIPT_NAME)


def hook_command() -> str:
    return f"{sys.executable} {HOOK_SCRIPT}"


def hook_item() -> dict:
    return {"matcher": "compact", "hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]}


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
        return "hooks.SessionStart item has non-array hooks"  # 否则 _our_command 遍历它会 TypeError 崩成 exit 1
    return ""


def _our_command(item: dict):
    """该 SessionStart 项里脚本 basename 为 compact_group_context.py 的 command；没有 → None。
    token 去掉两端引号再取 basename：模板写法把路径包在双引号里。"""
    for h in item.get("hooks") or []:
        cmd = h.get("command") if isinstance(h, dict) else None
        if isinstance(cmd, str) and any(os.path.basename(tok.strip("\"'")) == SCRIPT_NAME for tok in cmd.split()):
            return cmd
    return None


def merge(settings: dict) -> tuple:
    """返回 (合并后的新对象, 动作)，动作 ∈ installed / replaced / already installed；不改入参。"""
    new = json.loads(json.dumps(settings))  # 来自 JSON 的对象，round-trip 即深拷贝
    ss = new.setdefault("hooks", {}).setdefault("SessionStart", [])
    ours = [i for i, it in enumerate(ss) if it.get("matcher") == "compact" and _our_command(it) is not None]
    if not ours:
        ss.append(hook_item())
        return new, "installed"
    if len(ours) == 1 and _our_command(ss[ours[0]]) == hook_command():
        return new, "already installed"
    ss[ours[0]] = hook_item()
    for i in reversed(ours[1:]):  # 多条同脚本项（不该有）合并成一条
        del ss[i]
    return new, "replaced"


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
    ap = argparse.ArgumentParser(description="merge SessionStart(compact) hook into <bot_dir>/.claude/settings.json")
    ap.add_argument("bot_dir")
    ap.add_argument("--dry-run", action="store_true", help="only print the diff; no backup, no write")
    args = ap.parse_args(argv)

    bot_dir = os.path.realpath(args.bot_dir)
    cfg_dir = os.path.join(bot_dir, ".claude")
    target = os.path.join(cfg_dir, "settings.json")
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

    new, action = merge(settings)
    if action == "already installed":
        print(f"already installed: {target}")
        return 0
    diff = difflib.unified_diff(_dump(settings).splitlines(), _dump(new).splitlines(),
                                "settings.json (before)", "settings.json (after)", lineterm="")
    print("\n".join(diff))
    print(f"SessionStart[compact] {action}: {hook_command()}")
    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    tmp = f"{target}.tmp-{os.getpid()}"
    try:
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
