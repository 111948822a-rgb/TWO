"""
Celery 应用实例配置。

开发期通过 CELERY_EAGER 开关启用同步执行模式(task_always_eager),
无需启动独立 worker 即可在主进程内直接运行任务,便于调试。
生产环境将 CELERY_EAGER=False 并启动:
    celery -A app.core.celery_app worker -l info
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings


celery_app = Celery(
    "ai_video_commerce",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    # 开发期同步执行:任务调用直接在当前进程运行,不入队
    task_always_eager=settings.CELERY_EAGER,
    task_eager_propagates=True,
    # 序列化
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # 时区
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 任务路由(预留:后续 tasks.py 中的任务进入 pipeline 队列)
    task_routes={
        "app.pipelines.*": {"queue": "pipeline"},
    },
)

# 自动发现 tasks 模块(后续 app/pipelines/tasks.py 创建后生效)
celery_app.autodiscover_tasks(["app.pipelines"])
