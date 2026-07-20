#!/usr/bin/env bash
# ============================================================
#  AI 视频印钞机 —— macOS / Linux 桌面端一键打包脚本
#  前置:
#     pip install -r requirements.txt
#     pip install pyinstaller
#  产物: dist/AI视频印钞机/AI视频印钞机  (双击 / 命令行运行)
#  注意: 打包完成后,把整个 dist/AI视频印钞机 文件夹发给用户即可。
# ============================================================
set -e

pyinstaller --noconfirm --onedir --windowed --name "AI视频印钞机" \
  --add-data "frontend:frontend" \
  --add-data "app:app" \
  --hidden-import=uvicorn.logging \
  --hidden-import=uvicorn.loops \
  --hidden-import=uvicorn.loops.auto \
  --hidden-import=uvicorn.protocols \
  --hidden-import=uvicorn.protocols.http \
  --hidden-import=uvicorn.protocols.http.auto \
  --hidden-import=uvicorn.protocols.websockets \
  --hidden-import=uvicorn.protocols.websockets.auto \
  --hidden-import=uvicorn.lifespan \
  --hidden-import=uvicorn.lifespan.on \
  desktop_main.py

echo ""
echo "✅ 打包完成! 可执行文件位于: dist/AI视频印钞机/AI视频印钞机"
echo "   将整个 dist/AI视频印钞机 文件夹压缩发给用户即可双击运行。"
