#!/usr/bin/env python3
"""bot 自删评论（用于补救：发现自己之前的评论泄密了亲密关系）。

用法：python3 moment_delete_comment.py <comment_id>

权限：只允许删除 from_user 是该 bot id 的评论（不会误删用户的评论）。
bot id 自动从评论本身查出（comment 表 from_user 字段）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def main():
    if len(sys.argv) < 2:
        print("usage: moment_delete_comment.py <comment_id>", file=sys.stderr)
        sys.exit(2)
    try:
        cid = int(sys.argv[1])
    except ValueError:
        print(f"invalid comment_id: {sys.argv[1]}", file=sys.stderr)
        sys.exit(3)

    # 查这条评论的 from_user
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    row = conn.execute("SELECT from_user FROM moment_comments WHERE id=?", (cid,)).fetchone()
    if not row:
        print(f"comment {cid} not found", file=sys.stderr)
        sys.exit(4)

    from_user = row[0]
    # 只允许删 bot 自己发的评论（前缀不是 user_display_name 的）
    import config_loader
    g = config_loader.load_global()
    user_display = g.get("user_display_name") or "我"
    if from_user == user_display:
        print(f"refused: 不能删除用户({user_display})的评论", file=sys.stderr)
        sys.exit(5)

    ok = db.delete_comment(cid)
    if ok:
        print(f"OK deleted comment_id={cid} (from={from_user})")
    else:
        print(f"delete failed", file=sys.stderr)
        sys.exit(6)


if __name__ == "__main__":
    main()
