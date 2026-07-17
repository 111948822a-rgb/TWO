"""FastAPI 应用入口。

启动:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
访问:
    http://127.0.0.1:8000

路由:
    /api/projects          创建/查询任务
    /outputs/*             最终视频(静态)
    /uploads/*             上传图片(静态)
    /                      前端页面(frontend/)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, chat, dashboard, history, products, projects
from app.core.config import settings
from app.core.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# 启动诊断日志:确认 Python 解释器确实开始执行(用于排查 Docker 启动 Exited 128 类问题)
print(
    f"🚀 [Startup] 应用正在启动... 当前 PORT 环境变量为: {os.environ.get('PORT', '未设置')}",
    flush=True,
)

app = FastAPI(title="AI 带货视频生成系统", version="10.0.0")

# ===== 锁死持久化基础目录(必须位于 Render Disk,部署后不丢失) =====
DATA_DIR = Path(settings.DATA_ROOT)        # /app/data
STORAGE_DIR = Path(settings.STORAGE_ROOT)  # /app/data/storage (与数据库同处单块磁盘)
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
for subdir in ("outputs", "uploads", "temp", "audios", "images", "videos", "assets", "assets/bgm"):
    (STORAGE_DIR / subdir).mkdir(parents=True, exist_ok=True)

# 诊断:确认实际持久化落点(部署后必须位于 /app 下,否则历史仍会丢失)
print(f"💾 [Startup] DATA_DIR={DATA_DIR.resolve()}  STORAGE_DIR={STORAGE_DIR.resolve()}", flush=True)

init_db()

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(history.router)
app.include_router(products.router)
app.include_router(chat.router)
app.include_router(dashboard.router)

frontend_dir = Path(__file__).parent.parent / "frontend"
frontend_dir.mkdir(parents=True, exist_ok=True)

# 本地静态文件服务:视频/图片/音频均经本地磁盘访问(无 OSS 依赖)
# /storage 暴露整个持久化根目录;/outputs、/uploads 保留原有访问前缀
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")
app.mount("/outputs", StaticFiles(directory=str(STORAGE_DIR / "outputs")), name="outputs")
app.mount("/uploads", StaticFiles(directory=str(STORAGE_DIR / "uploads")), name="uploads")
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
