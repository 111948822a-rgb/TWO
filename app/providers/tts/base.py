"""TTS 语音合成 Provider 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TTSResult:
    """TTS 生成结果。

    duration 必须精确(秒,浮点),供合成阶段音画对齐使用。
    """

    audio_url: str
    local_path: str
    duration: float


class ITTSProvider(ABC):
    """语音合成抽象接口。

    核心契约:将 narration 文本合成为音频,并返回精确时长。
    时长精度对下一阶段 FFmpeg 音画同步至关重要。
    """

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice: str,
        output_path: str,
    ) -> TTSResult:
        """将文本合成为音频文件。

        Args:
            text: 旁白文案
            voice: 音色 ID
            output_path: 本地保存路径(mp3)

        Returns:
            TTSResult(含 audio_url / local_path / duration)
        """
