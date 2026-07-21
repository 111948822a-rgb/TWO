"""CosyVoice 语音合成 Provider(非实时 HTTP API)。

使用非实时 HTTP 接口,单次合成完整文本,响应中包含音频文件 URL(24 小时有效)。
下载音频到本地后,用 mutagen 读取精确时长(秒,浮点),供合成阶段音画对齐。

重要说明:
    1. 非实时 API 为同步调用(非流式),一次请求即返回完整音频 URL。
       本 Provider 没有"异步轮询任务状态"的 while 循环 —— DashScope 非实时
       TTS 接口是「请求即返回音频 URL」的同步模式,因此不存在轮询死循环问题。
       若日后切换到「提交任务 + 轮询状态」的异步 TTS,需在此处新增带
       max_retries / FAILED/UNKNOWN 状态判断的轮询循环(见模块底部注释模板)。
    2. 时长精度至关重要:用 mutagen.mp3.MP3 读取 info.length(浮点秒)。
    3. 仅华北2(北京)地域可用,与图片/视频共用同一 DASHSCOPE_API_KEY。
    4. 音色需与模型版本匹配:cosyvoice-v2 用 longxiaochun_v2 等带 _v2 后缀的音色。

防卡死改造(TTS-HARDEN-2):
    - 所有网络请求(httpx)强制 timeout=30s,超时立即抛 TimeoutError。
    - 整段 synthesize 用 asyncio.wait_for(timeout=75s) 包死:任意一行卡死
      (网络假活 / DNS 卡住 / 下载滴流)最多 75s 必强制退出,绝不死等。
    - ffprobe 阻塞调用改为 asyncio.to_thread + 超时,不阻塞事件循环。
    - 每步打印 1/4 ~ 4/4 全景日志,精准定位卡点。

参考文档:
    https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api
    (更新时间:2026-07-02)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
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

# ---- 防卡死超时常量(集中管理,便于调参) ----
TTS_HTTP_TIMEOUT = 30.0          # 单次 API 调用 / 文件下载的网络超时(秒)
TTS_CONNECT_TIMEOUT = 10.0       # 建连超时(秒)
TTS_HARD_TIMEOUT = 75.0          # synthesize 整段硬超时墙(秒):API + 下载 + 时长读取
TTS_FFPROBE_TIMEOUT = 20.0       # ffprobe 回退读取时长超时(秒)


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
        # 强制 30s 网络超时,杜绝无超时裸奔
        self.timeout = httpx.Timeout(
            TTS_HTTP_TIMEOUT, connect=TTS_CONNECT_TIMEOUT
        )

    async def synthesize(
        self,
        text: str,
        voice: str,
        output_path: str,
    ) -> TTSResult:
        """将文本合成为音频,下载到本地并计算精确时长。

        TTS-HARDEN-2:外层 asyncio.wait_for 硬超时墙。
        无论 _synthesize_impl 内部哪一步卡死(网络假活 / DNS / 滴流下载 /
        ffprobe 阻塞),最多 TTS_HARD_TIMEOUT 秒必被取消并抛出 TimeoutError,
        绝不允许无限死等。
        """
        if not text or not text.strip():
            raise RuntimeError("待合成文本为空")

        try:
            return await asyncio.wait_for(
                self._synthesize_impl(text, voice, output_path),
                timeout=TTS_HARD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[TTS] ❌ 合成整体超时(%.0fs 硬上限),疑似网络卡死或阻塞调用",
                TTS_HARD_TIMEOUT,
            )
            raise TimeoutError(
                f"[TTS] ❌ 配音合成超时({TTS_HARD_TIMEOUT:.0f}s):"
                f"API 或下载卡死"
            ) from None

    async def _synthesize_impl(
        self,
        text: str,
        voice: str,
        output_path: str,
    ) -> TTSResult:
        """合成主流程(被外层 wait_for 包裹),含 1/4~4/4 全景日志。"""
        logger.info(
            "[TTS] 1/4 准备调用 API, 文本长度: %d, model=%s, voice=%s",
            len(text), self.model, voice,
        )
        audio_url = await self._request_tts(text, voice)
        logger.info(
            "[TTS] 2/4 API 响应接收完毕, 状态: 200, url=%s", audio_url
        )

        logger.info(
            "[TTS] 3/4 开始下载音频文件到: %s", output_path
        )
        local_path = await self._download_audio(audio_url, output_path)
        size = os.path.getsize(local_path)
        logger.info(
            "[TTS] 4/4 音频文件下载并落盘成功, 大小: %d bytes", size
        )

        duration = await self._get_audio_duration(local_path)
        logger.info(
            "[TTS] ✅ 配音合成完成: %s, 时长=%.3fs", local_path, duration
        )
        return TTSResult(
            audio_url=audio_url,
            local_path=local_path,
            duration=duration,
        )

    async def _request_tts(self, text: str, voice: str) -> str:
        """调用非实时 TTS API,返回音频文件 URL。

        关键诊断加固: 任何非 200 响应都打印完整响应体(含 DashScope 的
        code/message,如 Arrearage 欠费 / InvalidParameter / QuotaExhausted),
        不再被 resp.raise_for_status() 默认的简短信息吞掉真实死因。
        网络超时(30s)立即转抛 TimeoutError,杜绝裸奔死等。
        """
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
            try:
                resp = await client.post(TTS_URL, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                logger.error("[TTS] ❌ API 调用超时(%.0fs): %s", TTS_HTTP_TIMEOUT, exc)
                raise TimeoutError(
                    f"[TTS] ❌ API 调用超时({TTS_HTTP_TIMEOUT:.0f}s): {exc}"
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("[TTS] ❌ 调用 API 网络异常: %s", exc)
                raise

            status_code = resp.status_code
            body_text = resp.text or ""
            if status_code != 200:
                # 尝试解析业务错误码,便于用户直接看到欠费/配额等根因
                biz_code = ""
                try:
                    err_json = resp.json()
                    biz_code = str(
                        err_json.get("code")
                        or err_json.get("message")
                        or err_json.get("error", {}).get("message")
                        or ""
                    )
                except Exception:  # noqa: BLE001
                    pass
                logger.error(
                    "[TTS] 调用 API 返回状态: %s, 业务错误码: %s, "
                    "响应内容: %s",
                    status_code, biz_code, body_text[:2000],
                )
                raise RuntimeError(
                    f"[TTS] 调用 API 返回状态 {status_code}"
                    f"{(' 业务错误: ' + biz_code) if biz_code else ''}"
                    f", 响应内容: {body_text[:1000]}"
                ) from None

            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[TTS] ❌ API 返回非 JSON 响应: %s", body_text[:1000]
                )
                raise RuntimeError(
                    f"[TTS] API 返回无法解析的响应: {body_text[:1000]}"
                ) from exc

        audio = data.get("output", {}).get("audio", {})
        url = audio.get("url")
        if not url:
            logger.error(
                "[TTS] ❌ 未返回音频 URL, 完整响应: %s", body_text[:2000]
            )
            raise RuntimeError(
                f"[TTS] CosyVoice 合成失败,未返回音频 URL: {body_text[:1000]}"
            )
        return url

    async def _download_audio(
        self, audio_url: str, output_path: str
    ) -> str:
        """下载音频文件到本地,并强制做落盘自检。

        落盘自检(核心): 写入后必须用 os.path.exists + os.path.getsize
        确认文件真实存在且大小 > 0。空文件/保存失败一律抛明确异常,
        避免把 0 字节或损坏的 mp3 带进合成阶段。
        网络超时(30s)立即转抛 TimeoutError。
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(audio_url)
            except httpx.TimeoutException as exc:
                logger.error(
                    "[TTS] ❌ 下载音频超时(%.0fs): %s",
                    TTS_HTTP_TIMEOUT, audio_url,
                )
                raise TimeoutError(
                    f"[TTS] ❌ 下载音频超时({TTS_HTTP_TIMEOUT:.0f}s):"
                    f" {audio_url}"
                ) from exc
            except httpx.HTTPError as exc:
                logger.error("[TTS] ❌ 下载音频网络异常: %s", exc)
                raise

            if resp.status_code != 200:
                logger.error(
                    "[TTS] ❌ 下载音频返回状态: %s, 响应内容: %s",
                    resp.status_code, (resp.text or "")[:500],
                )
                raise RuntimeError(
                    f"[TTS] 下载音频失败, HTTP {resp.status_code}"
                )
            content = resp.content or b""
            if len(content) == 0:
                logger.error(
                    "[TTS] ❌ 下载音频内容为空(HTTP %s): %s",
                    resp.status_code, audio_url,
                )
                raise RuntimeError(f"[TTS] 下载音频内容为空: {audio_url}")
            Path(output_path).write_bytes(content)

        # ---- 落盘自检 ----
        if not os.path.exists(output_path):
            raise RuntimeError(
                f"[TTS] ❌ 音频文件保存失败或为空: {output_path}"
            )
        size = os.path.getsize(output_path)
        if size == 0:
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise RuntimeError(
                f"[TTS] ❌ 音频文件保存失败或为空(0 字节): {output_path}"
            )
        return output_path

    async def _get_audio_duration(self, local_path: str) -> float:
        """读取音频精确时长(秒,浮点)。

        优先用 mutagen(纯 Python,无需 ffmpeg,非阻塞);
        若 mutagen 不可用则回退到 ffprobe —— 但 ffprobe 是同步 subprocess,
        必须用 asyncio.to_thread 丢到线程执行并加超时,否则会阻塞整个事件循环,
        在多分镜并发时造成"假死卡顿"。
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

        # 回退:ffprobe(需 ffmpeg)—— 非阻塞 + 超时保护
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
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
                ),
                timeout=TTS_FFPROBE_TIMEOUT,
            )
            return float(result.stdout.strip())
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"[TTS] ffprobe 读取时长超时({TTS_FFPROBE_TIMEOUT:.0f}s):"
                f" {local_path}"
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"无法读取音频时长(mutagen/ffprobe 均失败): {exc}"
            ) from exc


# ============================================================================
# 异步轮询 TTS 改造模板(当前 CosyVoice 非实时接口无需,仅供切换到异步 TTS 时参考)
# ----------------------------------------------------------------------------
# 若某 TTS Provider 采用「提交任务 -> 轮询状态」模式,必须这样写以防死循环:
#
#   MAX_RETRIES = 60          # 假设每次 sleep 2s,最多等 2 分钟
#   POLL_INTERVAL = 2.0
#   for attempt in range(1, MAX_RETRIES + 1):
#       status = await _query_task_status(task_id)   # 单次也需带 httpx timeout
#       logger.info("[TTS] 轮询状态: %s, 已重试 %d 次", status, attempt)
#       if status == "SUCCEEDED":
#           break
#       if status in ("FAILED", "UNKNOWN"):           # 必须同时判失败态
#           raise RuntimeError(f"[TTS] ❌ 轮询超时/任务失败: {status}")
#       await asyncio.sleep(POLL_INTERVAL)
#   else:
#       raise RuntimeError("[TTS] ❌ 轮询超时: 超过 %d 次仍未 SUCCEEDED" % MAX_RETRIES)
# ============================================================================
