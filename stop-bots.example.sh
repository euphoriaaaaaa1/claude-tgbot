#!/usr/bin/env bash
# 复制为 stop-bots.sh。停掉所有 bot 的 dispatcher（连带它的 worker 子进程一起走）。
for s in $(tmux ls 2>/dev/null | grep -oE '^tg-[a-z0-9_]+' | sort -u); do
  tmux kill-session -t "$s" 2>/dev/null && echo "已停 $s"
done
echo "全部 bot 已停。"
