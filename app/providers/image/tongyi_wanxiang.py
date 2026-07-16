"""通义万相 图片 Provider(图像背景生成 wanx-background-generation)。

使用 wanx-background-generation-v2 模型,专为电商商品换背景设计,
能让产品自然融入场景(带光影、阴影、反射),从根源避免"只换背景"痛点。

重要说明:
    1. 官方文档明确该接口"目前仅支持 HTTP 调用",dashscope SDK 未封装,
       故此处用 httpx 直接调 HTTP REST 接口。
    2. base_image_url 必须为公网可访问的透明背景 RGBA PNG。
       若用户提供的为白底图,需先经 utils/matting.py 抠图处理。
    3. 采用异步模式:提交任务 -> task_id -> 轮询状态 -> 获取结果 URL。
    4. 模型版本默认使用 v3(parameters.model_version="v3"),效果优于 v2。

参考文档:
    https://help.aliyun.com/zh/model-studio/wanx-background-generation-api-reference
    (更新时间:2026-06-16)
    https://help.aliyun.com/zh/model-studio/image-background-generation
    (更新时间:2026-06-29)
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.core.config import settings
from app.providers.image.base import IImageProvider
from app.utils.prompt_templates import truncate_prompt_safe

logger = logging.getLogger(__name__)

# 创建任务端点(异步)
CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "background-generation/generation/"
)
# 任务查询端点
TASK_URL_TEMPLATE = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

# 轮询参数
POLL_INTERVAL = 2.0  # 每次查询间隔(秒)
POLL_MAX_ATTEMPTS = 120  # 最多 120 次 = 240 秒 = 4 分钟


class TongyiWanxiangImageProvider(IImageProvider):
    """通义万相图像背景生成 Provider。"""

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.api_key = settings.DASHSCOPE_API_KEY
        self.model = "wanx-background-generation-v2"
        self.model_version = "v3"
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def generate_scene_image(
        self,
        subject_image_url: str,
        scene_prompt: str,
    ) -> str:
        """生成场景图:主体透明 PNG + 场景 prompt -> 融合场景图 URL。"""
        logger.info(
            "[通义万相] 提交背景生成任务,主体图=%s", subject_image_url
        )
        task_id = await self._submit_task(subject_image_url, scene_prompt)
        logger.info("[通义万相] task_id=%s,开始轮询", task_id)
        result_url = await self._poll_task(task_id)
        logger.info("[通义万相] 生成完成: %s", result_url)
        return result_url

    async def _submit_task(
        self, base_image_url: str, ref_prompt: str
    ) -> str:
        """提交异步任务,返回 task_id。"""
        # V16.1: 字数截断兜底,确保 prompt ≤150 词,避免 API Token 超限
        original_word_count = len(ref_prompt.split())
        ref_prompt = truncate_prompt_safe(ref_prompt)
        final_word_count = len(ref_prompt.split())
        logger.info(
            "[通义万相] image_prompt 词数: 原始=%d, 最终=%d (安全区间≤150)",
            original_word_count, final_word_count,
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # 异步模式必选头:缺少会报 "current user api does not support synchronous calls"
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": self.model,
            "input": {
                "base_image_url": base_image_url,
                "ref_prompt": ref_prompt,
            },
            "parameters": {
                "model_version": self.model_version,
                "n": 1,
            },
        }
        # 终极调试:打印完整请求 payload,便于在终端核对到底传了什么参数
        logger.info(
            "[通义万相] 提交任务完整 payload: %s",
            json.dumps(payload, ensure_ascii=False),
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(CREATE_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error(
                    "[通义万相] 提交任务失败 status=%s body=%s",
                    resp.status_code, resp.text,
                )
                resp.raise_for_status()
            data = resp.json()

        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(
                f"通义万相任务提交失败,未返回 task_id: {data}"
            )
        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询任务状态,返回结果图片 URL。

        状态机:PENDING -> RUNNING -> SUCCEEDED / FAILED
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = TASK_URL_TEMPLATE.format(task_id=task_id)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
                await asyncio.sleep(POLL_INTERVAL)
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                output = data.get("output", {})
                status = output.get("task_status")
                logger.debug(
                    "[通义万相] 轮询 %d/%d status=%s",
                    attempt,
                    POLL_MAX_ATTEMPTS,
                    status,
                )

                if status == "SUCCEEDED":
                    return self._extract_result_url(output)

                if status == "FAILED":
                    msg = output.get("message") or output.get("errors") or "未知错误"
                    code = output.get("code", "")
                    request_id = data.get("request_id", "")
                    logger.error(
                        "[通义万相] 任务失败 task_id=%s code=%s message=%s "
                        "request_id=%s 完整响应=%s",
                        task_id, code, msg, request_id, data,
                    )
                    raise RuntimeError(
                        f"通义万相图片生成失败 [code={code}]: {msg}"
                        f"{' (request_id=' + request_id + ')' if request_id else ''}"
                    )
                # PENDING / RUNNING 继续轮询

        raise RuntimeError(
            f"通义万相图片生成超时({POLL_MAX_ATTEMPTS * POLL_INTERVAL:.0f}s)"
        )

    @staticmethod
    def _extract_result_url(output: dict) -> str:
        """从任务输出中提取结果图片 URL(兼容多种返回格式)。"""
        # 格式1:output.results[0].url(文生图风格)
        results = output.get("results", [])
        if results:
            url = results[0].get("url") or results[0].get("b64_image")
            if url:
                return url
        # 格式2:output.result_url(部分接口)
        result_url = output.get("result_url")
        if result_url:
            return result_url
        # 格式3:output.url
        url = output.get("url")
        if url:
            return url
        raise RuntimeError(
            f"通义万相任务成功但未找到结果 URL: {output}"
        )
