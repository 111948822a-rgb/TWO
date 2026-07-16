"""视频片段生成服务。

调用图生视频 Provider,基于阶段②生成的场景图(首帧)+ 阶段①生成的
video_prompt(运镜指令)生成动态视频片段。

V14.1: 主引擎切换为 HappyHorse 1.1(百炼最新模型),通义万相作为 Fallback。
核心避坑:强制传入 scene.video_prompt,绝不让厂商使用默认推拉摇移。
更新 Scene.assets.video_clip.url / duration 与 status。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.providers.video.base import IVideoProvider
from app.providers.video.happyhorse_video import HappyHorseVideoProvider
from app.providers.video.tongyi_video import TongyiVideoProvider
from app.schemas.project import Scene

logger = logging.getLogger(__name__)


class VideoGenerator:
    """视频片段生成器(主引擎 HappyHorse + Fallback 通义万相)。

    主引擎(HappyHorse)失败时自动无缝降级到 Fallback(通义万相),
    保证流水线不中断。
    """

    def __init__(
        self,
        primary: IVideoProvider | None = None,
        fallback: IVideoProvider | None = None,
    ) -> None:
        self.primary = primary or HappyHorseVideoProvider()
        self.fallback = fallback or TongyiVideoProvider()
        logger.info(
            "[VideoGenerator] 当前使用主引擎: %s (Fallback: %s)",
            self.primary.PROVIDER_NAME,
            self.fallback.PROVIDER_NAME,
        )

    async def generate_for_scene(self, scene: Scene) -> str:
        """为单个分镜生成视频片段,更新 scene.assets.video_clip。

        主引擎 HappyHorse 失败时自动降级到通义万相 Fallback,保证流水线不中断。
        """
        keyframe_url = scene.assets.keyframe_image.url
        if not keyframe_url:
            raise RuntimeError(
                f"分镜 {scene.scene_id} 无关键帧图 URL,无法生成视频"
            )
        if not scene.video_prompt or not scene.video_prompt.strip():
            raise RuntimeError(
                f"分镜 {scene.scene_id} 无运镜指令,拒绝生成视频"
                "(避免厂商默认推拉摇移导致 PPT 轮播)"
            )

        logger.info(
            "[%s] 生成视频,运镜=%s",
            scene.scene_id,
            scene.video_prompt[:60],
        )

        engine_name = self.primary.PROVIDER_NAME
        try:
            logger.info(
                "[%s] 使用主引擎 %s 生成视频,运镜=%s",
                scene.scene_id, engine_name, scene.video_prompt[:60],
            )
            result = await self.primary.generate_video(
                keyframe_image_url=keyframe_url,
                video_prompt=scene.video_prompt,
                duration=settings.VIDEO_DURATION,
            )
        except Exception as exc:  # noqa: BLE001
            # 主引擎失败 → 无缝降级到 Fallback(通义万相),保证流水线不中断
            logger.warning(
                "[%s] ⚠️ 主引擎 %s 失败,降级到 %s: %s",
                scene.scene_id,
                self.primary.PROVIDER_NAME,
                self.fallback.PROVIDER_NAME,
                exc,
            )
            engine_name = self.fallback.PROVIDER_NAME
            result = await self.fallback.generate_video(
                keyframe_image_url=keyframe_url,
                video_prompt=scene.video_prompt,
                duration=settings.VIDEO_DURATION,
            )

        scene.assets.video_clip.url = result.video_url
        scene.assets.video_clip.duration = result.duration
        scene.assets.video_clip.engine = engine_name
        logger.info(
            "[%s] ✅ 视频生成完成(engine=%s): %s (时长 %.1fs)",
            scene.scene_id,
            engine_name,
            result.video_url,
            result.duration,
        )
        logger.info(
            "[VideoGenerator] 最终视频由引擎 %s 生成",
            engine_name,
        )
        return result.video_url
