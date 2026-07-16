"""视频生成 Provider 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VideoResult:
    """视频生成结果。"""

    video_url: str
    duration: float


class IVideoProvider(ABC):
    """视频生成抽象接口。

    核心契约:图生视频,必须将 video_prompt(运镜指令)作为 prompt 强制传入,
    避免厂商默认 pan/zoom 导致 PPT 轮播效果。
    """

    @abstractmethod
    async def generate_video(
        self,
        keyframe_image_url: str,
        video_prompt: str,
        duration: int = 5,
    ) -> VideoResult:
        """基于首帧图 + 运镜指令生成视频片段。

        Args:
            keyframe_image_url: 首帧图片 URL(阶段②生成的场景图)
            video_prompt: 运镜指令(如 dolly in / orbit / tracking),
                          必须传入,不可使用厂商默认推拉摇移
            duration: 视频时长(秒,整数)

        Returns:
            VideoResult(含 video_url 与 duration)
        """
