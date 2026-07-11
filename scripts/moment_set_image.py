#!/usr/bin/env python3
"""bot 给朋友圈追加图片：worker 用 novelai-skill 生完图后调用本脚本。

用法：python3 moment_set_image.py <moment_id> <img1> [img2] [img3] ...
- img1 写入 moments.image_path（主图/缩略，兼容所有读单图的旧代码）
- 全部路径写入 metadata_json.image_paths（列表，web 据此渲染多图九宫格）
"""
import sys
import os
import json as _json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import sqlite3


def main():
    if len(sys.argv) < 3:
        print("usage: moment_set_image.py <moment_id> <img1> [img2 ...]", file=sys.stderr)
        sys.exit(2)
    try:
        mid = int(sys.argv[1])
    except ValueError:
        print(f"invalid moment_id: {sys.argv[1]}", file=sys.stderr); sys.exit(3)

    imgs = [a.strip() for a in sys.argv[2:] if a.strip()]
    imgs = [p for p in imgs if os.path.exists(p)]
    if not imgs:
        print(f"no valid image in: {sys.argv[2:]}", file=sys.stderr); sys.exit(4)

    m = db.get_moment(mid)
    if not m:
        print(f"moment {mid} not found", file=sys.stderr); sys.exit(5)

    with sqlite3.connect(db.DB_PATH) as conn:
        row = conn.execute("SELECT metadata_json FROM moments WHERE id=?", (mid,)).fetchone()
        meta = {}
        if row and row[0]:
            try:
                meta = _json.loads(row[0])
            except Exception:
                meta = {}
        meta["image_paths"] = imgs
        conn.execute(
            "UPDATE moments SET image_path=?, metadata_json=? WHERE id=?",
            (imgs[0], _json.dumps(meta, ensure_ascii=False), mid),
        )
        conn.commit()
    print(f"OK moment {mid}: {len(imgs)} 张图，主图={imgs[0]}")


if __name__ == "__main__":
    main()
