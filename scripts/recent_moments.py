#!/usr/bin/env python3
"""bot worker 在 user 提到"朋友圈/那条/前几天发的"等关键词时调用本脚本，
查最近朋友圈作为对话上下文。

用法:
    python3 recent_moments.py <whose> [days]
    whose: self / user / <bot_id>
    days: 默认 7，最大 30

示例:
    # 查自己（用 TELEGRAM_WORKER_BOT 环境变量）最近 7 天发过的圈
    python3 recent_moments.py self

    # 查 user 最近 7 天发过的圈
    python3 recent_moments.py user

    # 查 bot3 最近 14 天发过的圈
    python3 recent_moments.py bot3 14

输出（一行一条，按时间倒序）：
    [05-04 09:01] (public) 五四青年节快乐...
    [05-03 23:30] (public) 下午窝在沙发上...
"""
import sys
import os
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


USER_PROFILE_KEY = "__user__"


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    whose = sys.argv[1].strip().lower()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    days = min(days, 30)

    if whose == "self":
        bot_id = os.environ.get("TELEGRAM_WORKER_BOT", "").strip()
        if not bot_id:
            print("ERROR: TELEGRAM_WORKER_BOT 环境变量未设（spawn-worker.sh 应注入）",
                  file=sys.stderr)
            sys.exit(3)
    elif whose == "user":
        bot_id = USER_PROFILE_KEY
    else:
        bot_id = whose

    since_ts = int(time.time()) - days * 86400
    rows = db.list_moments(bot_id=bot_id, limit=30, since_ts=since_ts)

    if not rows:
        print(f"（{whose} 最近 {days} 天没发朋友圈）")
        return

    for r in rows:
        ts = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
        vis = r.get("visibility", "public")
        text = r.get("text", "")[:120]
        print(f"[{ts}] ({vis}) {text}")


if __name__ == "__main__":
    main()
