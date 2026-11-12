#!/usr/bin/env python3
"""随包表情包安装（install.sh 与 windows/register-tasks.ps1 调用，也可手动跑）。

把仓里 assets/stickers/sfw/ 的图片复制到 <STICKERS_DIR>/sfw/，再生成
~/.claude/channels/_shared/stickers.json 的 sfw 档（gif 技能与 dispatcher 都读这两处）。

不覆盖原则：
  - stickers.json 已存在（含坏链接）→ 整步跳过，图片也不复制，只打印一句提示；
  - 某张图片的目标文件已存在 → 不覆盖，清单里照样指向它；
  - 有图片复制失败 → 不写 stickers.json，退 1；修好后重跑会补齐剩下的图片再写清单。

STICKERS_DIR 口径与 dispatcher.ts 的 stickerImageRoot() 一致：未设时缺省 ~/resource/stickers，
只展开 "~" 与 "~/" 开头；展开后必须是绝对路径。

用法：
  python3 scripts/install_stickers.py [--repo <dir>] [--home <dir>]
退出码：0 已安装或已跳过；1 素材缺失/复制或写入失败；2 参数错（含 STICKERS_DIR 不是绝对路径）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

DEFAULT_STICKERS_DIR = "~/resource/stickers"
TIER = "sfw"


def resolve_sticker_root(raw: str | None, home: Path) -> Path:
    """按 dispatcher 的口径解析表情库图片根；不是绝对路径抛 ValueError。"""
    raw = raw or DEFAULT_STICKERS_DIR
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError(f"STICKERS_DIR 必须是绝对路径或以 ~/ 开头：{raw}")
    return root


def load_bundle(bundle_dir: Path) -> list[dict]:
    """读素材 manifest，校验每条的文件名只是单层文件名且源文件存在；不合格抛 ValueError。"""
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest.json 缺 files 列表")
    seen = set()
    for entry in entries:
        name = entry.get("file") if isinstance(entry, dict) else None
        # manifest 是数据：文件名带目录分隔、盘符或 NTFS 流的冒号、..、或重复都不接受，
        # 防止写出 sfw 目录之外；两种路径语义下都必须是单层文件名，Windows 上同样成立。
        if (not isinstance(name, str) or not name or name in (".", "..")
                or "/" in name or "\\" in name or ":" in name
                or PureWindowsPath(name).name != name or PurePosixPath(name).name != name
                or name in seen):
            raise ValueError(f"manifest 里有非法或重复的文件名：{name!r}")
        if not isinstance(entry.get("label"), str) or not entry["label"].strip():
            raise ValueError(f"manifest 里 {name} 缺 label")
        if not (bundle_dir / "files" / name).is_file():
            raise ValueError(f"素材缺文件：files/{name}")
        seen.add(name)
    return entries


def copy_if_absent(src: Path, dest: Path) -> bool:
    """dest 不存在才复制（先写同目录临时文件再改名，中断不留半截图）；返回是否真的复制了。

    dest 已存在但不是普通文件（目录、坏链接等）抛 OSError：清单不能指向发不出去的路径。
    """
    if os.path.lexists(dest):
        if not dest.is_file():
            raise OSError(f"目标已存在但不是普通文件：{dest}")
        return False
    fd, tmp = tempfile.mkstemp(prefix=".sticker-", dir=dest.parent)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        if os.path.lexists(dest):  # 复制期间别处已放了同名文件：仍以它为准
            if not dest.is_file():
                raise OSError(f"目标已存在但不是普通文件：{dest}")
            return False
        os.chmod(tmp, 0o644)  # mkstemp 建的是 0600；表情图要能被别的进程读（Windows 上无害）
        os.replace(tmp, dest)
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def build_entry(entry: dict, dest: Path) -> dict:
    """转成 stickers.json 的条目：gif 技能按 label 挑、按 path 发；随包图片没有网络来源，url 留空。"""
    item = {
        # 用完整文件名：同名不同后缀（a.jpg / a.gif）不会撞 id。
        "id": f"{TIER}-{entry['file']}",
        "tier": TIER,
        "url": "",
        "label": entry["label"].strip(),
        "path": str(dest),
    }
    category = entry.get("category")
    if isinstance(category, str) and category.strip():
        item["category"] = category.strip()
    return item


def install(repo: Path, home: Path, raw_sticker_dir: str | None) -> int:
    manifest_path = home / ".claude" / "channels" / "_shared" / "stickers.json"
    if os.path.lexists(manifest_path):
        print(f"表情包清单已存在，跳过（不覆盖、不合并）：{manifest_path}")
        return 0
    try:
        sticker_root = resolve_sticker_root(raw_sticker_dir, home)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    bundle_dir = repo / "assets" / "stickers" / TIER
    try:
        entries = load_bundle(bundle_dir)
    except (OSError, ValueError) as exc:
        print(f"随包表情包素材不可用：{exc}", file=sys.stderr)
        return 1

    dest_dir = sticker_root / TIER
    items, copied, kept, failed = [], 0, 0, []
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"建不了图片目录 {dest_dir}：{exc}", file=sys.stderr)
        return 1
    for entry in entries:
        dest = dest_dir / entry["file"]
        try:
            if copy_if_absent(bundle_dir / "files" / entry["file"], dest):
                copied += 1
            else:
                kept += 1
        except OSError as exc:
            failed.append(f"{entry['file']}（{exc}）")
            continue
        items.append(build_entry(entry, dest))
    if failed:
        # 清单一旦写出，以后的安装就整步跳过；所以图片不全时宁可不写，等重跑补齐。
        print(f"有 {len(failed)} 张图片复制失败，未生成清单：{'；'.join(failed[:5])}", file=sys.stderr)
        return 1

    payload = json.dumps({"sfw": items, "nsfw": []}, ensure_ascii=False, indent=2) + "\n"
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".stickers.json.", dir=manifest_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            if os.path.lexists(manifest_path):  # 写的过程中别处生成了清单：以它为准
                print(f"表情包清单已存在，跳过（不覆盖、不合并）：{manifest_path}")
                return 0
            os.chmod(tmp, 0o644)
            os.replace(tmp, manifest_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        print(f"写不了表情包清单 {manifest_path}：{exc}", file=sys.stderr)
        return 1
    print(f"已安装随包表情包 {len(items)} 张（新复制 {copied}，已有未覆盖 {kept}）→ {dest_dir}；清单：{manifest_path}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", help="仓库根目录（缺省为本脚本上一级）")
    p.add_argument("--home", help="家目录（缺省为当前用户家目录）")
    args = p.parse_args(argv)
    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    home = Path(args.home) if args.home else Path.home()
    return install(repo, home, os.environ.get("STICKERS_DIR"))


if __name__ == "__main__":
    sys.exit(main())
