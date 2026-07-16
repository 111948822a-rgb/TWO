"""LLM Provider 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ILLMProvider(ABC):
    """LLM 抽象接口。"""

    @abstractmethod
    async def chat_json(self, system_prompt: str, user_prompt: str) -> str:
        """调用 LLM 对话,返回 JSON 格式字符串(由调用方解析)。

        实现方应通过 response_format / JSON mode 等机制强制输出合法 JSON。
        """
