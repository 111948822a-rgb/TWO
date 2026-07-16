"""HappyHorse 1.1 图生视频 Provider(阿里云百炼最新模型)。

V14.2 重要修复:基于官方文档 https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference
    1. model 修正为 happyhorse-1.1-i2v(图生视频专用,非 happyhorse-1.1)
    2. input 结构改为 media 数组(type=first_frame),非 img_url 字段
    3. 移除 negative_prompt/prompt_extend(I2V 官方不支持)
    4. 新增 watermark=false(去水印,商业用途)
    5. 强制追加 VIDEO_QUALITY_SUFFIX 画质增强后缀到 prompt 末尾

官方 I2V 支持的 parameters 仅有: resolution / duration / watermark / seed
(不支持 motion_scale / cfg_scale / negative_prompt — 这些参数在 HappyHorse I2V API 中不存在)

参考文档:
    https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings
from app.providers.video.base import IVideoProvider, VideoResult
from app.utils.prompt_templates import VIDEO_QUALITY_SUFFIX, truncate_prompt_safe

logger = logging.getLogger(__name__)

# 百炼视频生成端点(与通义万相共用)
CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "video-generation/video-synthesis"
)
TASK_URL_TEMPLATE = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

POLL_INTERVAL = 5.0
POLL_MAX_ATTEMPTS = 120  # 10 分钟超时


class HappyHorseVideoProvider(IVideoProvider):
    """HappyHorse 1.1 图生视频 Provider(百炼最新模型)。

    V14.2: 修正 model 名和 input 结构以匹配官方 I2V API 规范。
    """

    PROVIDER_NAME = "happyhorse-1.1-i2v"

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.api_key = settings.DASHSCOPE_API_KEY
        # V14.2: 官方 I2V model 名为 happyhorse-1.1-i2v
        self.model = "happyhorse-1.1-i2v"
        self.resolution = settings.VIDEO_RESOLUTION
        self.default_duration = settings.VIDEO_DURATION
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def generate_video(
        self,
        keyframe_image_url: str,
        video_prompt: str,
        duration: int = 5,
    ) -> VideoResult:
        """图生视频:首帧图 + 运镜指令 -> 视频 URL。"""
        if not video_prompt or not video_prompt.strip():
            raise RuntimeError(
                "video_prompt(运镜指令)为空,拒绝调用以避免默认推拉摇移"
            )

        # V14.2: 强制追加画质增强后缀(保证即使 LLM 输出简短也含画质词)
        final_prompt = f"{video_prompt.strip()}, {VIDEO_QUALITY_SUFFIX}"

        # V16.1: 字数截断兜底,确保 prompt ≤150 词,避免 API Token 超限
        original_word_count = len(final_prompt.split())
        final_prompt = truncate_prompt_safe(final_prompt)
        final_word_count = len(final_prompt.split())
        logger.info(
            "[%s] video_prompt 词数: 原始=%d, 最终=%d (安全区间≤150)",
            self.PROVIDER_NAME, original_word_count, final_word_count,
        )

        logger.info(
            "[%s] 提交 HappyHorse I2V 任务,首帧=%s, 运镜=%s",
            self.PROVIDER_NAME,
            keyframe_image_url,
            final_prompt[:80],
        )
        task_id = await self._submit_task(
            keyframe_image_url, final_prompt, duration
        )
        logger.info("[HappyHorse] task_id=%s,开始轮询", task_id)
        video_url = await self._poll_task(task_id)
        logger.info("[HappyHorse] 生成完成: %s", video_url)
        return VideoResult(video_url=video_url, duration=float(duration))

    async def _submit_task(
        self,
        img_url: str,
        prompt: str,
        duration: int,
    ) -> str:
        """提交异步任务,返回 task_id。

        V14.2: 使用官方 I2V input.media 结构(type=first_frame)。
        官方 I2V 仅支持 resolution/duration/watermark/seed 参数。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

        payload = {
            "model": self.model,
            "input": {
                "prompt": prompt,
                # V14.2: 官方 I2V 使用 media 数组,非 img_url 字段
                "media": [
                    {
                        "type": "first_frame",
                        "url": img_url,
                    }
                ],
            },
            "parameters": {
                "resolution": self.resolution,
                "duration": duration,
                # V14.2: 去水印(官方默认 true 会加 "Happy Horse" 水印)
                "watermark": False,
            },
        }

        # V14.2 模块3: 全链路 Prompt 日志打印(方便人工微调)
        logger.info("=" * 60)
        logger.info("[HappyHorse] 最终 Image URL: %s", img_url)
        logger.info("[HappyHorse] 最终 Video Prompt: %s", prompt)
        logger.info(
            "[HappyHorse] 最终 Negative Prompt: (N/A — 官方 I2V API 不支持 negative_prompt 字段)"
        )
        logger.info(
            "[HappyHorse] 核心参数: model=%s, resolution=%s, duration=%ss, watermark=False",
            self.model,
            self.resolution,
            duration,
        )
        logger.info(
            "[HappyHorse] 注: 官方 I2V API 不支持 motion_scale/cfg_scale/negative_prompt,"
            "动态表现力通过 Prompt 中的电影级运镜词汇控制"
        )
        logger.info("[HappyHorse] Request Payload: %s", payload)
        logger.info("=" * 60)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                CREATE_URL, headers=headers, json=payload
            )
            resp_text = resp.text
            logger.info(
                "[HappyHorse] Submit Response (HTTP %d): %s",
                resp.status_code,
                resp_text[:500],
            )
            resp.raise_for_status()
            data = resp.json()

        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(
                f"HappyHorse 任务提交失败,未返回 task_id: {data}"
            )
        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询任务状态,返回结果视频 URL。"""
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
                logger.info(
                    "[HappyHorse] 轮询 %d/%d status=%s",
                    attempt,
                    POLL_MAX_ATTEMPTS,
                    status,
                )

                if status == "SUCCEEDED":
                    return self._extract_video_url(output)

                if status == "FAILED":
                    msg = output.get("message", "未知错误")
                    code = output.get("code", "")
                    raise RuntimeError(
                        f"HappyHorse 视频生成失败[{code}]: {msg}"
                    )

        raise RuntimeError(
            f"HappyHorse 视频生成超时({POLL_MAX_ATTEMPTS * POLL_INTERVAL:.0f}s)"
        )

    @staticmethod
    def _extract_video_url(output: dict) -> str:
        """从任务输出中提取结果视频 URL(兼容多种返回格式)。"""
        video_url = output.get("video_url")
        if video_url:
            return video_url
        results = output.get("results", [])
        if results:
            url = results[0].get("url") or results[0].get("video_url")
            if url:
                return url
        raise RuntimeError(
            f"HappyHorse 任务成功但未找到结果 URL: {output}"
        )
