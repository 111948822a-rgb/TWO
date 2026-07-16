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
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import chat, history, products, projects
from app.core.config import settings
from app.core.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="AI 带货视频生成系统", version="10.0.0")

Path(settings.DATA_ROOT).mkdir(parents=True, exist_ok=True)
storage = Path(settings.STORAGE_ROOT)
storage.mkdir(parents=True, exist_ok=True)
for subdir in ("outputs", "uploads", "temp", "audios", "assets", "assets/bgm"):
    (storage / subdir).mkdir(parents=True, exist_ok=True)

init_db()

app.include_router(projects.router)
app.include_router(history.router)
app.include_router(products.router)
app.include_router(chat.router)

frontend_dir = Path(__file__).parent.parent / "frontend"
frontend_dir.mkdir(parents=True, exist_ok=True)

app.mount("/outputs", StaticFiles(directory=str(storage / "outputs")), name="outputs")
app.mount("/uploads", StaticFiles(directory=str(storage / "uploads")), name="uploads")
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
