"""通义万相 图生视频 Provider(wan2.2-i2v-flash)。

使用 wan2.2-i2v-flash 模型(极速版),官方文档明确其"指令理解与运镜控制更准,
画面元素保持一致,稳定性与成功率全面提升"——正好契合本系统强制运镜 prompt 的避坑需求。

重要说明:
    1. 采用异步模式:提交任务 -> task_id -> 轮询状态 -> 获取结果 video_url。
    2. 必须将 video_prompt(运镜指令)作为 input.prompt 传入,绝不可留空
       让厂商使用默认推拉摇移(会导致 PPT 轮播效果)。
    3. img_url(首帧图)不支持透明通道 PNG。阶段②生成的场景图无透明通道,可直接使用。
    4. 视频生成耗时 1-5 分钟,轮询间隔 5s,超时 10 分钟。

参考文档:
    https://help.aliyun.com/zh/model-studio/legacy-image-to-video-api-reference
    (更新时间:2026-06-16)
    https://help.aliyun.com/zh/model-studio/image-to-video-guide
    (更新时间:2026-06-12)
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings
from app.providers.video.base import IVideoProvider, VideoResult

logger = logging.getLogger(__name__)

# 图生视频(基于首帧)创建任务端点
CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "video-generation/video-synthesis"
)
# 任务查询端点(与图片共用)
TASK_URL_TEMPLATE = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

# 视频生成轮询参数(视频比图片慢得多)
POLL_INTERVAL = 5.0  # 每次查询间隔(秒)
POLL_MAX_ATTEMPTS = 120  # 最多 120 次 = 600 秒 = 10 分钟


class TongyiVideoProvider(IVideoProvider):
    """通义万相图生视频 Provider。"""

    PROVIDER_NAME = "wan2.2-i2v-flash"

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.api_key = settings.DASHSCOPE_API_KEY
        self.model = settings.VIDEO_MODEL  # 默认 wan2.2-i2v-flash
        self.resolution = settings.VIDEO_RESOLUTION  # 默认 720P
        self.default_duration = settings.VIDEO_DURATION  # 默认 5 秒
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def generate_video(
        self,
        keyframe_image_url: str,
        video_prompt: str,
        duration: int = 5,
    ) -> VideoResult:
        """图生视频:首帧图 + 运镜指令 -> 视频 URL。

        核心避坑:video_prompt 必须非空,强制传入专业运镜术语。
        """
        if not video_prompt or not video_prompt.strip():
            raise RuntimeError(
                "video_prompt(运镜指令)为空,拒绝调用以避免默认推拉摇移"
            )

        logger.info(
            "[通义视频] 提交图生视频任务,首帧=%s, 运镜=%s",
            keyframe_image_url,
            video_prompt[:60],
        )
        task_id = await self._submit_task(
            keyframe_image_url, video_prompt, duration
        )
        logger.info("[通义视频] task_id=%s,开始轮询", task_id)
        video_url = await self._poll_task(task_id)
        logger.info("[通义视频] 生成完成: %s", video_url)
        return VideoResult(video_url=video_url, duration=float(duration))

    async def _submit_task(
        self,
        img_url: str,
        prompt: str,
        duration: int,
    ) -> str:
        """提交异步任务,返回 task_id。

        V12.0 品控增强:
          - negative_prompt: 强制传入负面提示词,压制手部畸形/环境扭曲等崩坏
          - prompt_extend=False: 关闭 API 自动扩展 prompt(避免引入不可控的高风险运镜)
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # 异步模式必选头
            "X-DashScope-Async": "enable",
        }
        # V12.0 负面提示词:压制 AI 视频常见的崩坏/幻觉
        negative_prompt = (
            "bad hands, missing fingers, deformed, ugly, blurry, "
            "distorted environment, extra limbs, mutated, "
            "malformed fingers, extra fingers, fused fingers, "
            "distorted face, warped body, morphed objects"
        )
        payload = {
            "model": self.model,
            "input": {
                "prompt": prompt,
                "img_url": img_url,
                "negative_prompt": negative_prompt,
            },
            "parameters": {
                "resolution": self.resolution,
                "duration": duration,
                # V12.0: 关闭 prompt 自动扩展,避免 API 引入不可控的高风险运镜
                "prompt_extend": False,
            },
        }
        logger.info(
            "[通义视频] V12.0 品控: negative_prompt=%s..., prompt_extend=False",
            negative_prompt[:40],
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                CREATE_URL, headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()

        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(
                f"通义视频任务提交失败,未返回 task_id: {data}"
            )
        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询任务状态,返回结果视频 URL。

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
                logger.info(
                    "[通义视频] 轮询 %d/%d status=%s",
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
                        f"通义视频生成失败[{code}]: {msg}"
                    )
                # PENDING / RUNNING 继续轮询

        raise RuntimeError(
            f"通义视频生成超时({POLL_MAX_ATTEMPTS * POLL_INTERVAL:.0f}s)"
        )

    @staticmethod
    def _extract_video_url(output: dict) -> str:
        """从任务输出中提取结果视频 URL(兼容多种返回格式)。"""
        # 主格式:output.video_url
        video_url = output.get("video_url")
        if video_url:
            return video_url
        # 兼容:output.results[0].url
        results = output.get("results", [])
        if results:
            url = results[0].get("url") or results[0].get("video_url")
            if url:
                return url
        raise RuntimeError(
            f"通义视频任务成功但未找到结果 URL: {output}"
        )
