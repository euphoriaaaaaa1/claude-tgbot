#!/usr/bin/env python3
"""bot worker 收到朋友圈通知后，决定"只点赞不评论"时调用本脚本。

用法：python3 moment_like.py <moment_id>

行为：
1. 从环境变量 TELEGRAM_WORKER_BOT 读 bot_id（spawn-worker.sh 设的）
2. 调 db.toggle_like(moment_id, bot_id) — 已点过会取消（toggle 语义），未点则添加
3. 防自赞：如果 moment 是自己发的，拒绝
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def main():
    if len(sys.argv) < 2:
        print("usage: moment_like.py <moment_id>", file=sys.stderr)
        sys.exit(2)

    moment_id = int(sys.argv[1])

    bot_id = os.environ.get("TELEGRAM_WORKER_BOT", "").strip()
    if not bot_id:
        print("TELEGRAM_WORKER_BOT 环境变量未设（应由 spawn-worker.sh 注入）",
              file=sys.stderr)
        sys.exit(3)

    moment = db.get_moment(moment_id)
    if not moment:
        print(f"moment {moment_id} not found", file=sys.stderr)
        sys.exit(5)

    # 防自赞：bot 不能赞自己发的圈
    if moment.get("bot_id") == bot_id:
        print(f"refused: bot {bot_id} 不能赞自己的圈", file=sys.stderr)
        sys.exit(6)

    liked = db.toggle_like(moment_id, bot_id)
    print(f"OK: bot={bot_id} moment={moment_id} liked={liked}")


if __name__ == "__main__":
    main()
