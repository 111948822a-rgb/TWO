"""V10.0 AI 导演 Agent 引擎(基于阿里云百炼 Qwen-Max Function Calling)。

通过 OpenAI 兼容接口调用 Qwen-Max,支持:
    1. 自然语言对话(流式 SSE 输出)
    2. Function Calling 工具调用:
       - generate_video_task: 创建视频生成任务(复用现有 Pipeline)
       - search_product_library: 搜索产品库

设计要点:
    - base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    - model: qwen-max
    - 流式输出: 文本逐字返回 + 业务动作事件
    - 容错: tool_calls JSON 解析失败时返回友好错误,不崩溃
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, List

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.database import list_products, sync_project_from_model

logger = logging.getLogger(__name__)

# Qwen-Max OpenAI 兼容端点
_DASHSCOPE_COMPATIBLE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

SYSTEM_PROMPT = """你是 AI 导演助手,帮助用户快速创建带货短视频。你可以:

1. 搜索产品库中的产品(search_product_library)
2. 创建视频生成任务(generate_video_task)

工作流程:
- 当用户提到产品或想制作视频时,先搜索产品库匹配产品
- 找到产品后,使用产品的图片URL和卖点信息创建视频任务
- 如果用户直接提供了产品名称、卖点和图片URL,可以直接创建任务
- 任务创建后会自动执行完整流水线(文案→图片→视频→音频→合成)

参数说明:
- language: en(英语)/th(泰语)/id(印尼语)
- vibe: upbeat(动感)/premium(高级)/chill(轻松)/cinematic(电影)/viral(网感)/asmr(解压)/urgent(急促)
- visual_style: photorealistic(真实)/3d_render(3D)/anime(二次元)/cyberpunk(赛博朋克)

始终用中文回复,简洁专业。"""


# ---------------------------------------------------------------------------
# 工具定义(JSON Schema for Function Calling)
# ---------------------------------------------------------------------------

AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_video_task",
            "description": "创建视频生成任务并自动执行完整流水线(文案分镜→场景图片→动态视频→音频→后期合成)。需要至少一张产品图片的公网URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "产品名称,如:保温杯、蓝牙耳机",
                    },
                    "selling_points": {
                        "type": "string",
                        "description": "产品卖点,逗号分隔,如:24小时保温,防漏水,316不锈钢",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["en", "th", "id"],
                        "description": "目标语言:en英语,th泰语,id印尼语",
                    },
                    "vibe": {
                        "type": "string",
                        "enum": ["upbeat", "premium", "chill", "cinematic", "viral", "asmr", "urgent"],
                        "description": "视频氛围",
                    },
                    "visual_style": {
                        "type": "string",
                        "enum": ["photorealistic", "3d_render", "anime", "cyberpunk"],
                        "description": "视觉风格",
                    },
                    "image_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "产品图片的公网URL列表(至少1张)",
                    },
                },
                "required": ["product_name", "selling_points", "image_urls"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_product_library",
            "description": "搜索产品库中的产品。返回匹配的产品列表(含名称、卖点、图片URL)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词,如:杯子、耳机、化妆品",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
]


class AgentService:
    """AI 导演 Agent,封装 Qwen-Max 对话与工具调用。"""

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY,请在 .env 中设置")
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=_DASHSCOPE_COMPATIBLE_BASE,
        )
        self.model = "qwen-max"

    async def chat_stream(
        self,
        message: str,
        history: List[Dict[str, str]],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式对话,yield SSE 事件字典。

        事件格式:
            {"type": "text", "content": "..."}       — 文本片段
            {"type": "action", "payload": {...}}      — 业务动作
            {"type": "error", "content": "..."}       — 错误信息
            {"type": "done"}                          — 流结束
        """
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        # 追加历史对话(仅保留 role/content 的文本消息)
        for h in history[-10:]:  # 最多保留最近 10 轮
            role = h.get("role", "user")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        try:
            async for event in self._stream_with_tools(messages):
                yield event
        except Exception as exc:
            logger.exception("[Agent] 对话异常: %s", exc)
            yield {"type": "error", "content": f"对话出错: {exc}"}
        finally:
            yield {"type": "done"}

    async def _stream_with_tools(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """第一轮流式输出 + 工具执行 + 第二轮总结。"""
        # ---- 第一轮:流式调用,可能返回文本和/或 tool_calls ----
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=AGENT_TOOLS,
            stream=True,
        )

        tool_calls_accum: Dict[int, Dict[str, str]] = {}
        has_tool_calls = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 流式输出文本
            if delta.content:
                yield {"type": "text", "content": delta.content}

            # 累积 tool_calls(参数分片到达)
            if delta.tool_calls:
                has_tool_calls = True
                for tc in delta.tool_calls:
                    idx = tc.index if tc.index is not None else 0
                    if idx not in tool_calls_accum:
                        tool_calls_accum[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc.id:
                        tool_calls_accum[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_accum[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_accum[idx]["arguments"] += tc.function.arguments

        # ---- 无工具调用:第一轮文本即为最终回复 ----
        if not has_tool_calls:
            return

        # ---- 有工具调用:执行工具,然后第二轮总结 ----
        assistant_tool_calls = []
        for idx in sorted(tool_calls_accum):
            tc = tool_calls_accum[idx]
            assistant_tool_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                },
            })

        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": assistant_tool_calls,
        })

        # 逐个执行工具
        for idx in sorted(tool_calls_accum):
            tc = tool_calls_accum[idx]
            tool_name = tc["name"]
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError as je:
                logger.error("[Agent] 工具参数 JSON 解析失败: %s (raw: %s)", je, tc["arguments"][:200])
                args = {}

            try:
                result = await self._execute_tool(tool_name, args)
            except Exception as exc:
                logger.exception("[Agent] 工具 %s 执行失败", tool_name)
                result = {"error": f"工具执行失败: {exc}"}

            # 发送业务动作事件
            if tool_name == "generate_video_task" and "task_id" in result:
                yield {
                    "type": "action",
                    "payload": {
                        "action_type": "task_created",
                        "task_id": result["task_id"],
                        "product_name": args.get("product_name", ""),
                    },
                }
            elif tool_name == "search_product_library":
                yield {
                    "type": "action",
                    "payload": {
                        "action_type": "product_found",
                        "products": result.get("products", []),
                        "count": result.get("count", 0),
                    },
                }

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False)[:2000],
            })

        # ---- 第二轮:带工具结果再次调用,流式输出总结 ----
        try:
            stream2 = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream2:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield {"type": "text", "content": delta.content}
        except Exception as exc:
            logger.exception("[Agent] 第二轮流式调用失败: %s", exc)
            yield {"type": "text", "content": f"(工具已执行,但总结生成失败: {exc})"}

    # -----------------------------------------------------------------
    # 工具执行
    # -----------------------------------------------------------------

    async def _execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """分发执行工具函数。"""
        if name == "generate_video_task":
            return await self._tool_generate_video_task(args)
        elif name == "search_product_library":
            return await self._tool_search_product_library(args)
        else:
            return {"error": f"未知工具: {name}"}

    async def _tool_generate_video_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """创建视频生成任务,复用现有 Pipeline。"""
        from app.api.routes.projects import _spawn_task, _run_safe
        from app.schemas.project import VideoProject

        product_name = args.get("product_name", "").strip()
        selling_points = args.get("selling_points", "").strip()
        language = args.get("language", "en")
        vibe = args.get("vibe", "upbeat")
        visual_style = args.get("visual_style", "photorealistic")
        image_urls = args.get("image_urls", [])

        if not product_name:
            return {"error": "product_name 不能为空"}
        if not image_urls or not isinstance(image_urls, list):
            return {"error": "image_urls 不能为空(至少需要1张产品图的公网URL)"}

        # 过滤无效 URL
        valid_urls = [u.strip() for u in image_urls if u and u.strip().startswith("http")]
        if not valid_urls:
            return {"error": "image_urls 中无有效 URL(必须以 http 开头)"}

        task_id = uuid.uuid4().hex[:12]
        sp = [s.strip() for s in selling_points.replace("，", ",").split(",") if s.strip()]

        project = VideoProject(
            project_id=task_id,
            input={
                "product_name": product_name,
                "product_description": selling_points,
                "selling_points": sp,
                "target_audience": "",
                "white_image_url": valid_urls[0],
                "image_urls": valid_urls,
                "duration_target_sec": 15,
                "style": "生活化",
                "language": language,
                "enable_voiceover": True,
                "vibe": vibe,
                "visual_style": visual_style,
                "defringe_strength": "medium",
            },
        )
        sync_project_from_model(project)
        _spawn_task(task_id, _run_safe(project))

        logger.info(
            "[Agent] 任务已创建: %s (产品=%s, 语言=%s, 氛围=%s, 图片=%d张)",
            task_id, product_name, language, vibe, len(valid_urls),
        )

        return {
            "task_id": task_id,
            "status": "pending",
            "product_name": product_name,
            "language": language,
            "vibe": vibe,
            "message": f"已创建视频任务: {product_name}({language}),任务ID: {task_id}",
        }

    async def _tool_search_product_library(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """搜索产品库。"""
        keyword = args.get("keyword", "").strip().lower()
        all_products = list_products()

        if keyword:
            matched = [
                p for p in all_products
                if keyword in p.get("name", "").lower()
                or keyword in p.get("selling_points", "").lower()
            ]
        else:
            matched = all_products[:10]

        # 精简返回(避免 token 过多)
        slim = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "selling_points": p.get("selling_points", "")[:200],
                "image_urls": p.get("image_urls", [])[:3],
            }
            for p in matched[:10]
        ]

        logger.info(
            "[Agent] 产品库搜索: keyword='%s', 匹配 %d 个",
            keyword, len(slim),
        )

        return {
            "products": slim,
            "count": len(slim),
            "message": f"找到 {len(slim)} 个匹配产品" if slim else "未找到匹配产品",
        }
