"""图片 Provider 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class IImageProvider(ABC):
    """图片生成抽象接口。

    核心契约:主体保持 + 场景融合(避免简单换背景)。
    实现方必须将 subject_image_url 作为主体参考,结合 scene_prompt
    生成产品自然融入场景的图片。
    """

    @abstractmethod
    async def generate_scene_image(
        self,
        subject_image_url: str,
        scene_prompt: str,
    ) -> str:
        """生成场景图,返回结果图片 URL。

        Args:
            subject_image_url: 主体商品图 URL(通常需为公网可访问的透明 PNG)
            scene_prompt: 场景描述 prompt(含环境/光线/构图)

        Returns:
            生成的场景图 URL
        """
