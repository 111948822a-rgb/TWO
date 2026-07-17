"""Dashboard 统计 API (V16.0)。

首页看板聚合数据接口。
系统当前未实现用户鉴权(无 user_id 字段),因此返回全局统计。
若未来接入鉴权,所有 COUNT 需追加 `WHERE user_id = ?` 隔离条件。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.api.routes.auth import get_current_user
from app.core.database import get_dashboard_stats

logger = logging.getLogger(__name__)

# V17.0: 首页看板需登录
router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/stats")
async def dashboard_stats() -> dict:
    """返回首页看板所需的聚合统计数据。

    返回字段:
        total_products  产品总数
        total_videos    成功视频总数(final_video_url 非空)
        today_videos    今日成功视频数(按 UTC 日期)
        running_tasks   当前正在处理中的任务数(活跃流水线状态)
    """
    return get_dashboard_stats()
