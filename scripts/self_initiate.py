#!/usr/bin/env python3
"""self_initiate.py <bot> <chat_id> — 跨平台主动消息触发器（替代 self-initiate.sh）。

流程：随机间隔闸（30min-24h，开口时机不可预测像活人）→ 调 life-context.py 富情境
判定（SKIP/FALLBACK/TEXT）→ 写 unified inbox → POST /ensure_worker 拉起 worker。

新架构下 inbox 是持久队列（dispatcher 内嵌 worker-manager 监视，ready 后 drain），
旧 bash 版"先 spawn 等 tmux ready 再写 inbox"的 race 修复整个不再需要。
macOS launchd 与 Windows 任务计划都调本脚本（每 10 分钟 tick，闸内自节流）。
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

COOLDOWN_MIN = 1800     # 最短间隔 30 分钟
COOLDOWN_MAX = 86400    # 最长间隔 24 小时

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.expanduser("~/.claude/dispatcher/.self-initiate-state")


def _bot_port(bot: str) -> str | None:
    """从 configs/_global.yml 之外最简单的来源拿端口：restart 脚本同款约定，
    或环境变量 DISPATCHER_PORT_<BOT>。找不到就返回 None（只写 inbox 不拉 worker）。"""
    env_key = f"DISPATCHER_PORT_{bot.upper()}"
    if os.environ.get(env_key):
        return os.environ[env_key]
    # 默认单 bot 部署：17801
    return "17801"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: self_initiate.py <bot> <chat_id>", file=sys.stderr)
        return 2
    bot, chat = sys.argv[1], sys.argv[2]
    now = int(time.time())
    hour = datetime.now().hour

    # ─── 随机间隔闸：到点才允许，然后立刻 roll 下一个周期 ───
    os.makedirs(STATE_DIR, exist_ok=True)
    marker = os.path.join(STATE_DIR, f"{bot}-{chat}.last")
    interval_f = os.path.join(STATE_DIR, f"{bot}-{chat}.interval")
    try:
        last = int(open(marker).read().strip())
    except Exception:
        last = 0
    try:
        target = int(open(interval_f).read().strip())
    except Exception:
        target = COOLDOWN_MIN
    if last and now - last < target:
        print(f"skip: 距上次 {now-last}s < 随机目标 {target}s", file=sys.stderr)
        return 0
    open(marker, "w").write(str(now))
    open(interval_f, "w").write(str(random.randint(COOLDOWN_MIN, COOLDOWN_MAX)))

    bot_dir = os.path.expanduser(f"~/.claude/channels/{bot}")
    inbox = os.path.join(bot_dir, "inbox")
    os.makedirs(inbox, exist_ok=True)

    # ─── since_last_user_msg_min（dispatcher 写的 marker）───
    since_min = "unknown"
    lu = os.path.join(STATE_DIR, f"{bot}-{chat}.last-user")
    try:
        since_min = str((now - int(open(lu).read().strip())) // 60)
    except Exception:
        pass

    # ─── life-context 富情境判定 ───
    life_ctx = os.path.join(REPO_ROOT, "life-context.py")
    text = f"[self-initiate] hour={hour} since_last_user_msg_min={since_min}"
    if os.path.isfile(life_ctx):
        try:
            r = subprocess.run([sys.executable, life_ctx, bot, chat],
                               capture_output=True, text=True, timeout=300, cwd=REPO_ROOT)
            if r.returncode != 0:
                print(f"life-context exit={r.returncode} → skip", file=sys.stderr)
                return 0
            out = json.loads(r.stdout.strip().splitlines()[-1])
            action = out.get("action")
            if action == "SKIP":
                print(f"skip per life-context: {out.get('reason','')}", file=sys.stderr)
                return 0
            if action == "TEXT":
                text = str(out.get("text", ""))[:4000]
            elif action != "FALLBACK":
                print(f"unknown action {action} → skip", file=sys.stderr)
                return 0
        except Exception as e:
            print(f"life-context error {type(e).__name__}: {e} → 用 FALLBACK 文本", file=sys.stderr)

    # ─── 防积压：清掉本 bot 之前未消费的 self-*（只有最新一条有意义）───
    for f in os.listdir(inbox):
        if f.startswith("self-") and f.endswith(".json"):
            try: os.remove(os.path.join(inbox, f))
            except OSError: pass

    # ─── 写 unified inbox（schema 与 dispatcher 真实 meta 对齐）───
    ms = int(time.time() * 1000)
    payload = {
        "text": text,
        "chat_id": chat,
        "from_id": chat,
        "from_username": "user",
        "sender_username": "user",
        "chat_type": "private",
        "scene": "private",
        "is_bot_sender": False,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message_id": str(ms),
    }
    fname = os.path.join(inbox, f"self-{ms}.json")
    tmp = fname + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, fname)
    print(f"wrote {fname}", file=sys.stderr)

    # ─── 拉起 worker（dispatcher /ensure_worker；不在也没事，inbox 会被 drain）───
    port = _bot_port(bot)
    if port:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ensure_worker", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"ensure_worker 失败(dispatcher 未起?): {e}", file=sys.stderr)

    # ─── quota 计费 ───
    try:
        sys.path.insert(0, REPO_ROOT)
        import quota
        quota.record_call("worker_trigger", quota.WORKER_TRIGGER_WEIGHT)
    except Exception:
        print("quota record_call failed (non-fatal)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
