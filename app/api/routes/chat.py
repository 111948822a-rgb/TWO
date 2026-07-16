"""V10.0 AI 导演 Chat 接口(SSE 流式输出)。

POST /api/chat
    Body: {"message": "...", "history": [{"role":"user","content":"..."}, ...]}
    Response: text/event-stream
        data: {"type": "text", "content": "..."}      文本片段
        data: {"type": "action", "payload": {...}}     业务动作
        data: {"type": "error", "content": "..."}      错误
        data: {"type": "done"}                         结束
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.agent import AgentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    history: Optional[List[ChatMessage]] = Field(default_factory=list)


@router.post("/chat")
async def chat(body: ChatRequest):
    """AI 导演对话,SSE 流式返回。"""
    try:
        agent = AgentService()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    history = [
        {"role": m.role, "content": m.content}
        for m in (body.history or [])
    ]

    async def event_stream():
        try:
            async for event in agent.chat_stream(body.message, history):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Chat] SSE 流异常: %s", exc)
            err = {"type": "error", "content": f"流中断: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            done = {"type": "done"}
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )
