#!/bin/sh
# 启动包装脚本:打印运行环境 + 执行 gunicorn,便于排查 Render "Exited 128" 类问题。
# 用 exec 让 gunicorn 接替 shell 作为主进程,退出码/信号能正确透传。

echo "============================================================"
echo "[boot] python:    $(python --version 2>&1)"
echo "[boot] gunicorn:  $(gunicorn --version 2>&1)"
echo "[boot] PORT:      ${PORT:-8000}"
echo "[boot] cwd:       $(pwd)"
echo "[boot] /app on sys.path: $(python -c 'import sys; print("/app" in sys.path or "." in sys.path)')"
echo "============================================================"

exec gunicorn app.main:app \
    -w 1 \
    -k uvicorn.workers.UvicornWorker \
    -b 0.0.0.0:${PORT:-8000} \
    --timeout 300 \
    --access-logfile - \
    --error-logfile -
