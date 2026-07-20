#!/bin/bash
# 启动电话模块。加载本目录 .env（CALL_TOKEN / VAPID 私钥路径等，已 gitignore，本文件不含任何密钥）。
# Python 需装本目录 requirements.txt 的依赖；用你自己的解释器请设 PYTHON_BIN=/path/to/python。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
cd "$HERE"   # push_state.json 落本目录（已 gitignore）；server.py 自行把仓库根加入 sys.path
exec "${PYTHON_BIN:-python3}" "$HERE/server.py"
