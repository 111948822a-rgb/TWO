"""DeepSeek LLM Provider(使用 openai 兼容库)。

DeepSeek 完全兼容 OpenAI API 协议,可直接用 openai SDK:
    - base_url 指向 https://api.deepseek.com
    - model 使用 deepseek-chat
    - 支持 response_format={"type": "json_object"} 强制 JSON 输出

参考:https://api-docs.deepseek.com/(OpenAI 兼容章节)
"""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.core.config import settings
from app.providers.llm.base import ILLMProvider

logger = logging.getLogger(__name__)


class DeepSeekProvider(ILLMProvider):
    """DeepSeek 异步 Provider。"""

    def __init__(self) -> None:
        if not settings.DEEPSEEK_API_KEY:
            raise RuntimeError(
                "未配置 DEEPSEEK_API_KEY,请在 .env 中设置"
            )
        self.client = AsyncOpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
        )
        self.model = "deepseek-chat"

    async def chat_json(self, system_prompt: str, user_prompt: str) -> str:
        """调用 DeepSeek,强制返回 JSON 字符串。"""
        logger.info("[DeepSeek] 调用 chat_json,model=%s", self.model)
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.8,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content or ""
        logger.info("[DeepSeek] 返回 %d 字符", len(content))
        return content
