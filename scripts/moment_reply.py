#!/usr/bin/env python3
"""bot worker 收到 [moment-interaction] inbox 后，调用本脚本回写朋友圈评论。

用法：
    python3 moment_reply.py <moment_id> <parent_comment_id> "<reply_text>" [--image <path>]

行为：
1. 写入 moment_comments 表，from_user = bot_id，parent_id = parent_comment_id
2. 把父评论的 pending 标志清掉
3. 可选附图（bot 用 novelai-skill 生完图后 --image <path>）
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def main():
    args = sys.argv[1:]
    image_path = None
    if "--image" in args:
        i = args.index("--image")
        if i + 1 >= len(args):
            print("--image needs a path", file=sys.stderr); sys.exit(2)
        image_path = args[i + 1]
        args = args[:i] + args[i + 2:]
    if len(args) < 3:
        print("usage: moment_reply.py <moment_id> <parent_comment_id> <reply_text> [--image <path>]",
              file=sys.stderr); sys.exit(2)

    moment_id = int(args[0])
    parent_id_raw = int(args[1])
    parent_id = None if parent_id_raw == 0 else parent_id_raw
    reply_text = args[2].strip()

    if not reply_text and not image_path:
        print("both empty", file=sys.stderr); sys.exit(3)

    if image_path and not os.path.exists(image_path):
        print(f"image not found: {image_path}", file=sys.stderr); sys.exit(4)

    moment = db.get_moment(moment_id)
    if not moment:
        print(f"moment {moment_id} not found", file=sys.stderr); sys.exit(5)

    # 真正的 bot_id 必须从 worker 环境读（spawn-worker.sh 设了 TELEGRAM_WORKER_BOT）
    # 不能用 moment.bot_id —— 用户朋友圈 moment.bot_id='__user__'，会让 bot 评论被记成用户
    env_bot = os.environ.get("TELEGRAM_WORKER_BOT")
    if env_bot:
        bot_id = env_bot
    else:
        # 兜底：moment 是 bot 自己发的（commenting on own moment）
        bot_id = moment["bot_id"]
        if bot_id == "__user__":
            print("ERROR: TELEGRAM_WORKER_BOT 未设且 moment.bot_id=__user__，无法识别评论 bot",
                  file=sys.stderr); sys.exit(7)

    new_id = db.add_comment(moment_id, bot_id, reply_text,
                            parent_id=parent_id, pending=False,
                            image_path=image_path)
    if parent_id:  # 不是直接评 moment（parent=None 时跳过 mark_pending）
        db.mark_pending(parent_id, False)
    db.insert_call_log(int(time.time()), "moment_reply", 1)
    print(f"OK comment_id={new_id} bot={bot_id} moment={moment_id} image={'yes' if image_path else 'no'}")


if __name__ == "__main__":
    main()
