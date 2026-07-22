"""FastAPI 应用入口。

启动:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
访问:
    http://127.0.0.1:8000

路由:
    /api/projects          创建/查询任务
    /storage/*             本地生成文件(静态)
    /outputs/*             最终视频(静态)
    /uploads/*             上传图片(静态)
    /                      前端页面(frontend/)

桌面客户端形态说明(V17.4):
    持久化目录由 app.core.config 自适应解析(用户文档/AIVideoStudio),
    不再依赖云端 /data 挂载。桌面启动器 desktop_main.py 会以 AIVS_DESKTOP=1
    环境变量启动本服务,并开放 POST /api/shutdown 供前端"关闭软件"按钮优雅退出。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.api.routes import auth, chat, dashboard, diagnose, history, products, projects
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

# ===== 本地持久化基础目录(桌面客户端:用户文档/AIVideoStudio,由 config 自适应解析) =====
DATA_DIR = Path(settings.DATA_ROOT)
STORAGE_DIR = Path(settings.STORAGE_ROOT)
# 确保根目录与子目录均存在(config 已建根目录,此处补齐业务子目录)
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
for subdir in ("outputs", "uploads", "temp", "audios", "images", "videos", "assets", "assets/bgm"):
    (STORAGE_DIR / subdir).mkdir(parents=True, exist_ok=True)

# V17.6: 云端锁定挂载盘 /data(Render 注入 RENDER_EXTERNAL_URL 即视为云端)。
# 显式创建用户要求的持久化子目录, 杜绝因目录缺失导致 StaticFiles 返回 404。
# 限定云端执行, 避免本地桌面客户端在 C:\ 误建 /data 目录。
if os.getenv("RENDER_EXTERNAL_URL"):
    for _d in ("/data/db", "/data/storage/outputs", "/data/storage/temp", "/data/storage/assets"):
        os.makedirs(_d, exist_ok=True)
    print("[Startup] 云端模式: 已显式锁定并创建持久化目录 /data/{db,storage}", flush=True)

# 诊断:确认实际持久化落点(本地桌面形态应位于 用户文档/AIVideoStudio 之下)
print(f"💾 [Startup] DATA_DIR={DATA_DIR.resolve()}  STORAGE_DIR={STORAGE_DIR.resolve()}", flush=True)

init_db()

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(history.router)
app.include_router(products.router)
app.include_router(chat.router)
app.include_router(dashboard.router)
app.include_router(diagnose.router)


# ===== 自愈:进程启动时拉起"孤儿任务" =====
# 后台 asyncio 流水线任务只存活于拉起它的 worker 进程内。当 Gunicorn worker 因
# 部署 / 重启 / OOM 被回收时,在跑的任务会被静默杀死(无错误、无完成),导致前端
# 进度条永远卡在某阶段。进程刚启动时任何"活跃"状态的任务都必定是孤儿,直接续跑
# 绝对安全(单 worker 下不会重复拉起)。看门狗则兜底周期性卡死的任务。
@app.on_event("startup")
async def _startup_self_heal():
    try:
        from app.api.routes import projects as _projects
        recovered = _projects.recover_orphaned_tasks()
        if recovered:
            logging.getLogger(__name__).info(
                "[Self-Heal] 启动自愈:拉起 %d 个孤儿任务: %s", len(recovered), recovered
            )
        else:
            logging.getLogger(__name__).info("[Self-Heal] 启动未发现需自愈的孤儿任务")
        _projects.start_watchdog()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("[Self-Heal] 启动自愈异常(已忽略): %s", exc)


@app.post("/api/shutdown")
async def shutdown():
    """优雅关闭后台服务(仅桌面端 AIVS_DESKTOP=1 时可用)。

    由前端"关闭软件"按钮调用。返回响应后延迟终止进程,
    桌面启动器检测到子进程退出即干净收尾,不残留僵尸进程。
    """
    if os.environ.get("AIVS_DESKTOP") != "1":
        raise HTTPException(status_code=403, detail="当前运行模式不支持关闭服务")

    def _delayed_exit():
        import time
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"ok": True, "message": "正在关闭 AI 视频印钞机…"}


frontend_dir = Path(__file__).parent.parent / "frontend"
frontend_dir.mkdir(parents=True, exist_ok=True)

# 本地静态文件服务:视频/图片/音频均经本地磁盘访问(无 OSS 依赖)
# /storage 暴露整个持久化根目录;/outputs、/uploads 保留原有访问前缀
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")
app.mount("/outputs", StaticFiles(directory=str(STORAGE_DIR / "outputs")), name="outputs")
app.mount("/uploads", StaticFiles(directory=str(STORAGE_DIR / "uploads")), name="uploads")
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
