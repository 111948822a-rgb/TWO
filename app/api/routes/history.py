"""历史记录 API(V8.0)。

提供项目历史的分页查询和详情查看。
数据从 SQLite 读取,支持重启后恢复。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.core.database import get_project_detail, list_projects

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
@router.get("/")
async def get_history(
    page: int = Query(1, ge=1, description="页码(从1开始)"),
    size: int = Query(20, ge=1, le=100, description="每页数量"),
):
    """分页返回历史项目列表(不含 scenes_data)。"""
    result = list_projects(page=page, size=size)
    return result


@router.get("/{task_id}")
async def get_history_detail(task_id: str):
    """返回单个项目的完整详情(含 scenes_data)。"""
    detail = get_project_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail="项目不存在")
    return detail
