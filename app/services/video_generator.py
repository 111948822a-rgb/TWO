"""视频片段生成服务。

调用图生视频 Provider,基于阶段②生成的场景图(首帧)+ 阶段①生成的
video_prompt(运镜指令)生成动态视频片段。

V16.2 断舍离:系统唯一视频引擎 = HappyHorse 1.1(阿里云百炼),
彻底移除通义万相视频兜底。HappyHorse 失败时异常**直接向上抛出**,
由编排器标记为分镜/任务 failed 并记录详细错误日志,不再静默降级。
核心避坑:强制传入 scene.video_prompt,绝不让厂商使用默认推拉摇移。
更新 Scene.assets.video_clip.url / duration 与 status。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.providers.video.happyhorse_video import HappyHorseVideoProvider
from app.schemas.project import Scene

logger = logging.getLogger(__name__)


class VideoGenerator:
    """视频片段生成器(唯一引擎: HappyHorse 1.1)。

    不再有任何降级 / 兜底逻辑。HappyHorse 报错即原样向上抛出,
    由调用方(orchestrator.stage_video_gen)捕获并标记分镜 failed,
    错误信息(str(exc))会持久化到 SQLite 并最终展示在前端。
    """

    def __init__(self) -> None:
        # 唯一视频引擎: HappyHorse 1.1(百炼图生视频)
        self.provider = HappyHorseVideoProvider()
        logger.info(
            "[VideoGenerator] 视频引擎已锁定为唯一 Provider: %s",
            self.provider.PROVIDER_NAME,
        )

    async def generate_for_scene(self, scene: Scene) -> str:
        """为单个分镜生成视频片段,更新 scene.assets.video_clip。

        HappyHorse 失败则**直接抛出** RuntimeError(携带完整死因),
        不做任何降级。调用方负责捕获并标记分镜为 failed。
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
            "[%s] 使用视频引擎 %s 生成视频,运镜=%s",
            scene.scene_id,
            self.provider.PROVIDER_NAME,
            scene.video_prompt[:60],
        )

        # 唯一引擎 HappyHorse:不捕获、不降级,异常原样向上抛出
        result = await self.provider.generate_video(
            keyframe_image_url=keyframe_url,
            video_prompt=scene.video_prompt,
            duration=settings.VIDEO_DURATION,
        )

        scene.assets.video_clip.url = result.video_url
        scene.assets.video_clip.duration = result.duration
        scene.assets.video_clip.engine = self.provider.PROVIDER_NAME
        logger.info(
            "[%s] ✅ 视频生成完成(engine=%s): %s (时长 %.1fs)",
            scene.scene_id,
            self.provider.PROVIDER_NAME,
            result.video_url,
            result.duration,
        )
        return result.video_url
