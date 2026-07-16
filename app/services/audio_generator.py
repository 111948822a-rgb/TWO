"""音频(旁白)生成服务。

调用 CosyVoice TTS,将每个分镜的 narration 合成为旁白音频(mp3),
并获取精确时长(供合成阶段音画对齐)。

更新 Scene.assets.audio.local_path / duration 与 status。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.config import settings
from app.providers.tts.base import ITTSProvider
from app.providers.tts.cosyvoice import CosyVoiceProvider
from app.schemas.project import Scene

logger = logging.getLogger(__name__)


class AudioGenerator:
    """旁白音频生成器。"""

    def __init__(self, provider: ITTSProvider | None = None) -> None:
        self.provider = provider or CosyVoiceProvider()

    async def generate_for_scene(
        self,
        scene: Scene,
        storage_root: str | None = None,
        voice: str | None = None,
    ) -> str:
        """为单个分镜生成旁白音频,更新 scene.assets.audio。

        Args:
            scene: 分镜对象(读取 narration,写入 audio.local_path / duration)
            storage_root: 本地存储根目录(默认 settings.STORAGE_ROOT)
            voice: TTS 音色(由 orchestrator 按语言解析后传入);None 则回退 settings.TTS_VOICE

        Returns:
            音频本地路径
        """
        if not scene.narration or not scene.narration.strip():
            raise RuntimeError(
                f"分镜 {scene.scene_id} 无旁白文案,无法生成音频"
            )

        root = Path(storage_root or settings.STORAGE_ROOT)
        output_path = str(root / "audios" / f"{scene.scene_id}.mp3")
        tts_voice = voice or settings.TTS_VOICE
        logger.info(
            "[%s] 生成旁白,音色=%s,文案=%s",
            scene.scene_id,
            tts_voice,
            scene.narration[:40],
        )
        result = await self.provider.synthesize(
            text=scene.narration,
            voice=tts_voice,
            output_path=output_path,
        )
        scene.assets.audio.local_path = result.local_path
        scene.assets.audio.duration = result.duration
        logger.info(
            "[%s] 旁白生成完成: %s (时长 %.3fs)",
            scene.scene_id,
            result.local_path,
            result.duration,
        )
        return result.local_path
