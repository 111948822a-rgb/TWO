"""CosyVoice 语音合成 Provider(非实时 HTTP API)。

使用非实时 HTTP 接口,单次合成完整文本,响应中包含音频文件 URL(24 小时有效)。
下载音频到本地后,用 mutagen 读取精确时长(秒,浮点),供合成阶段音画对齐。

重要说明:
    1. 非实时 API 为同步调用(非流式),一次请求即返回完整音频 URL。
    2. 时长精度至关重要:用 mutagen.mp3.MP3 读取 info.length(浮点秒)。
    3. 仅华北2(北京)地域可用,与图片/视频共用同一 DASHSCOPE_API_KEY。
    4. 音色需与模型版本匹配:cosyvoice-v2 用 longxiaochun_v2 等带 _v2 后缀的音色。

参考文档:
    https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api
    (更新时间:2026-07-02)
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.core.config import settings
from app.providers.tts.base import ITTSProvider, TTSResult

logger = logging.getLogger(__name__)

# 非实时语音合成端点(旧域名仍可用,与图片/视频保持一致)
TTS_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/"
    "SpeechSynthesizer"
)

# V4.0 市场聚焦:语言代码 → CosyVoice 音色映射(仅 English/Thai/Indonesian)
# CosyVoice 2.0 所有音色均具备跨语种合成能力,泰语/印尼语无专属音色,
# 复用通用多语言音色 longxiaochun_v2(龙小纯,女声,跨语种表现稳定)
LANGUAGE_VOICES: dict[str, str] = {
    "en": "longwan_v2",        # 英语-龙宛(女声,英文发音清晰)
    "th": "longxiaochun_v2",   # 泰语-龙小纯(通用多语言)
    "id": "longxiaochun_v2",   # 印尼语-龙小纯(通用多语言)
}


def resolve_voice(language: str) -> str:
    """按目标语言解析 CosyVoice 音色,未知语言回退到 settings.TTS_VOICE。"""
    return LANGUAGE_VOICES.get(language, settings.TTS_VOICE)


class CosyVoiceProvider(ITTSProvider):
    """CosyVoice 非实时语音合成 Provider。"""

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.api_key = settings.DASHSCOPE_API_KEY
        self.model = settings.TTS_MODEL  # 默认 cosyvoice-v2
        self.timeout = httpx.Timeout(60.0, connect=10.0)

    async def synthesize(
        self,
        text: str,
        voice: str,
        output_path: str,
    ) -> TTSResult:
        """将文本合成为音频,下载到本地并计算精确时长。"""
        if not text or not text.strip():
            raise RuntimeError("待合成文本为空")

        logger.info(
            "[CosyVoice] 合成语音,model=%s, voice=%s, 文本=%s",
            self.model,
            voice,
            text[:40],
        )
        audio_url = await self._request_tts(text, voice)
        logger.info("[CosyVoice] 音频 URL: %s", audio_url)

        local_path = await self._download_audio(audio_url, output_path)
        duration = self._get_audio_duration(local_path)
        logger.info(
            "[CosyVoice] 下载完成: %s, 时长=%.3fs",
            local_path,
            duration,
        )
        return TTSResult(
            audio_url=audio_url,
            local_path=local_path,
            duration=duration,
        )

    async def _request_tts(self, text: str, voice: str) -> str:
        """调用非实时 TTS API,返回音频文件 URL。"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": {
                "text": text,
                "voice": voice,
                "format": "mp3",
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(TTS_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        audio = data.get("output", {}).get("audio", {})
        url = audio.get("url")
        if not url:
            raise RuntimeError(
                f"CosyVoice 合成失败,未返回音频 URL: {data}"
            )
        return url

    async def _download_audio(
        self, audio_url: str, output_path: str
    ) -> str:
        """下载音频文件到本地。"""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(audio_url)
            resp.raise_for_status()
            Path(output_path).write_bytes(resp.content)
        return output_path

    @staticmethod
    def _get_audio_duration(local_path: str) -> float:
        """读取音频精确时长(秒,浮点)。

        优先用 mutagen(纯 Python,无需 ffmpeg);
        若 mutagen 不可用则回退到 ffprobe(需系统安装 ffmpeg)。
        """
        try:
            from mutagen.mp3 import MP3

            audio = MP3(local_path)
            return float(audio.info.length)
        except ImportError:
            logger.warning(
                "[CosyVoice] mutagen 未安装,回退到 ffprobe 计算时长"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CosyVoice] mutagen 读取时长失败: %s,回退 ffprobe", exc
            )

        # 回退:ffprobe(需 ffmpeg)
        import subprocess

        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    local_path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return float(result.stdout.strip())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"无法读取音频时长(mutagen/ffprobe 均失败): {exc}"
            ) from exc
