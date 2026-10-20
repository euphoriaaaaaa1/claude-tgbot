#!/bin/bash
# voice-bridge HTTP 服务启动脚本
# 用法: ./start.sh [stop|restart|status]

set -e
cd "$(dirname "$0")"
mkdir -p logs

PID_FILE="logs/http_server.pid"
LOG_FILE="logs/http_server.out"
PORT=7788

cmd="${1:-start}"

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "$cmd" in
  start)
    if is_running; then
      echo "已在运行 (PID $(cat $PID_FILE))"
      exit 0
    fi
    # FISH_AUDIO_API_KEY / TELEGRAM_BOT_TOKEN 优先从当前 shell 环境继承；
    # 没设的话再从 ~/.zshrc 里找一遍（如果你 export 在那里）
    if [ -z "$FISH_AUDIO_API_KEY" ]; then
      if [ -f ~/.zshrc ]; then
        eval "$(grep -E '^export (FISH_AUDIO_API_KEY|TELEGRAM_BOT_TOKEN)=' ~/.zshrc 2>/dev/null || true)"
      fi
    fi
    # 代理：如果你的网络需要代理才能访问 Fish Audio / Telegram，先在当前 shell 里
    # export HTTPS_PROXY=http://... 再跑本脚本（这里只透传你已有的设置，不设默认值——
    # 瞎设一个本机不存在的代理端口，会让所有外网请求连接失败）。
    # NO_PROXY 保证访问本机 127.0.0.1 的请求不走代理。
    export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
    if [ ! -x .venv/bin/python ]; then
      echo "✗ 没找到 .venv/bin/python，先建虚拟环境装依赖："
      echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
      exit 1
    fi
    nohup .venv/bin/python server_http.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if is_running; then
      echo "✓ 启动成功 (PID $(cat $PID_FILE)), 端口 $PORT"
      echo "  日志: $LOG_FILE"
      echo "  健康检查: curl http://127.0.0.1:$PORT/health"
    else
      echo "✗ 启动失败，查看 $LOG_FILE"
      tail -20 "$LOG_FILE"
      exit 1
    fi
    ;;
  stop)
    if is_running; then
      kill "$(cat $PID_FILE)"
      rm -f "$PID_FILE"
      echo "✓ 已停止"
    else
      echo "未运行"
    fi
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if is_running; then
      echo "运行中 (PID $(cat $PID_FILE))"
      curl -s "http://127.0.0.1:$PORT/health" && echo
    else
      echo "未运行"
    fi
    ;;
  *)
    echo "用法: $0 [start|stop|restart|status]"
    exit 1
    ;;
esac
