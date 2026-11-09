#!/bin/bash
# 阿里云等自有服务器上的运行入口（cron 调用）
# 用法：先在 .env 里填好三个变量（参考 .env.example，chmod 600 .env）
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p logs
.venv/bin/python pipeline.py "$@" >> logs/scan.log 2>&1
