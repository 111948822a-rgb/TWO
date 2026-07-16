"""场景图片生成服务。

调用通义万相 background-generation,将主体透明 PNG + 场景 prompt
融合生成场景图(产品自然融入场景,带光影/阴影)。

更新 Scene.assets.keyframe_image.url 与 status。
"""

from __future__ import annotations

import logging

from app.providers.image.base import IImageProvider
from app.providers.image.tongyi_wanxiang import TongyiWanxiangImageProvider
from app.schemas.project import Scene

logger = logging.getLogger(__name__)


class ImageGenerator:
    """场景图片生成器。"""

    def __init__(self, provider: IImageProvider | None = None) -> None:
        self.provider = provider or TongyiWanxiangImageProvider()

    async def generate_for_scene(
        self, scene: Scene, subject_image_url: str
    ) -> str:
        """为单个分镜生成场景图,更新 scene.assets.keyframe_image.url。

        Args:
            scene: 分镜对象(读取 image_prompt,写入 keyframe_image.url)
            subject_image_url: 主体商品图 URL(须为公网可访问的透明 PNG)

        Returns:
            生成的场景图 URL
        """
        logger.info(
            "[%s] 生成场景图,prompt=%s",
            scene.scene_id,
            scene.image_prompt[:60],
        )
        url = await self.provider.generate_scene_image(
            subject_image_url=subject_image_url,
            scene_prompt=scene.image_prompt,
        )
        scene.assets.keyframe_image.url = url
        logger.info("[%s] 场景图生成完成: %s", scene.scene_id, url)
        return url
