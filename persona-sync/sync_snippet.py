#!/usr/bin/env python3
"""把多个 bot 共用的规则段（snippet）同步进各自的 persona（CLAUDE.md）。

每个 snippet 用一对 HTML 注释标记包住（<!-- XXX-START ... --> 到 <!-- XXX-END -->）：
- 目标文件里已有整段 → 替换（内容一致则跳过）
- 没有 → 追加到文件末尾

标记之外的文本（各 bot 自己的 override）本脚本不动。幂等：重复跑不产生重复内容。

用法：
    python3 sync_snippet.py [--config snippets.json] [--dry-run] [--check]

配置默认读脚本同目录的 snippets.json。路径先展开 ~，相对路径按配置文件所在
目录解析。字段说明见 snippets.json 里的 _说明。

退出码：0 成功（--check 时无差异）；1 --check 时存在差异；2 配置或文件错误。
"""
import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path


class SyncError(Exception):
    """单个文件同步不了的原因（标记重复/不成对等）。"""


class ConfigError(Exception):
    """配置缺失或格式不对。"""


def resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else base / p


def load_config(config_path: Path):
    """读配置，返回 (targets, snippets)。snippets 元素为 (源路径, start, end)。"""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"配置文件不存在: {config_path}（照 snippets.json 填一份）")
    except json.JSONDecodeError as e:
        raise ConfigError(f"配置文件不是合法 JSON: {config_path}（{e}）")
    if not isinstance(data, dict):
        raise ConfigError(f"配置顶层必须是对象: {config_path}")

    targets_raw = data.get("targets")
    snippets_raw = data.get("snippets")
    if not isinstance(targets_raw, list) or not targets_raw:
        raise ConfigError("targets 必须是非空数组（要同步的 persona 文件列表）")
    if not isinstance(snippets_raw, list) or not snippets_raw:
        raise ConfigError("snippets 必须是非空数组（每条含 source/start/end）")

    base = config_path.resolve().parent
    targets = []
    for t in targets_raw:
        if not isinstance(t, str) or not t.strip():
            raise ConfigError(f"targets 里有非法项: {t!r}")
        targets.append(resolve_path(t.strip(), base))

    snippets = []
    for item in snippets_raw:
        if not isinstance(item, dict):
            raise ConfigError(f"snippets 里有非法项: {item!r}")
        source, start, end = item.get("source"), item.get("start"), item.get("end")
        if not all(isinstance(x, str) and x.strip() for x in (source, start, end)):
            raise ConfigError(f"snippet 的 source/start/end 都必须是字符串: {item!r}")
        snippets.append((resolve_path(source.strip(), base), start.strip(), end.strip()))
    return targets, snippets


def plan_change(content: str, snippet_text: str, start: str, end: str):
    """算出该怎么改。返回 (action, new_content)，action ∈ {skip, replace, append}。

    标记不成对（或重复出现）时报 SyncError——多半是手工编辑出了岔子，
    不猜、不写，留给人工处理。
    """
    start_re = re.compile(r"<!--\s*" + re.escape(start) + r"\b")
    end_re = re.compile(r"<!--\s*" + re.escape(end) + r"\s*-->")
    start_pos = [m.start() for m in start_re.finditer(content)]
    end_pos = [m.end() for m in end_re.finditer(content)]

    if not start_pos and not end_pos:
        # 用切片而非 re.sub 替换内容：替换串里的 \n 等字面量会被 re.sub 当转义处理
        new = (content.rstrip() + "\n\n" if content.strip() else "") + snippet_text + "\n"
        return "append", new

    if len(start_pos) != 1 or len(end_pos) != 1 or end_pos[0] <= start_pos[0]:
        raise SyncError(
            f"标记不成对或重复（START x{len(start_pos)}, END x{len(end_pos)}），请手工检查后重跑"
        )

    new = content[: start_pos[0]] + snippet_text + content[end_pos[0]:]
    return ("skip", content) if new == content else ("replace", new)


def atomic_write(path: Path, text: str) -> None:
    """临时文件 + rename 的原子写，中断不会写坏人设；保留原文件权限。"""
    tmp = path.with_name(path.name + ".tmp-persona-sync")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把共用的规则段同步到多个 bot 的 persona（幂等）",
        epilog="先用 --dry-run 预览再真跑。配置格式见 snippets.json。",
    )
    parser.add_argument("--config", default=None, help="配置文件（默认：脚本同目录 snippets.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印会怎么改，不写任何文件")
    parser.add_argument("--check", action="store_true", help="不写文件；存在差异时退出码 1（可挂 CI）")
    args = parser.parse_args(argv)

    if args.config:
        config_path = Path(args.config).expanduser()
    else:
        config_path = Path(__file__).resolve().parent / "snippets.json"
    try:
        targets, snippets = load_config(config_path)
    except ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2

    dry = args.dry_run or args.check
    prefix = "[check]" if args.check else "[dry-run]"
    changed = errors = 0

    for target in targets:
        if not target.is_file():
            print(f"✗ 目标不存在: {target}（改 snippets.json 里的 targets？）")
            errors += 1
            continue
        for source, start, end in snippets:
            if not source.is_file():
                print(f"✗ snippet 源缺失: {source}")
                errors += 1
                continue
            snippet_text = source.read_text(encoding="utf-8").strip()
            if not snippet_text:
                print(f"✗ snippet 源为空: {source}")
                errors += 1
                continue
            try:
                content = target.read_text(encoding="utf-8")
                action, new_content = plan_change(content, snippet_text, start, end)
            except SyncError as e:
                print(f"✗ [{start}] {target}: {e}")
                errors += 1
                continue
            except OSError as e:
                print(f"✗ 读写失败 {target}: {e}")
                errors += 1
                continue

            if action == "skip":
                print(f"= [{start}] 已是最新: {target}")
                continue
            changed += 1
            verb = "追加" if action == "append" else "替换"
            if dry:
                print(f"{prefix} 将{verb} [{start}] → {target}")
            else:
                try:
                    atomic_write(target, new_content)
                except OSError as e:
                    print(f"✗ 写盘失败 {target}: {e}")
                    errors += 1
                    continue
                print(f"✓ [{start}] {verb} → {target}")

    if args.check:
        print(f"[check] {'有 %d 处差异' % changed if changed else '无差异'}")
    elif dry:
        print(f"dry-run 完成：将变更 {changed} 次（未写盘）")
    else:
        print(f"同步完成：变更 {changed} 次")
    if errors:
        print(f"✗ 有 {errors} 个错误，未全部同步成功", file=sys.stderr)
        return 2
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
