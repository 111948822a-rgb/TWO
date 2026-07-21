"""TTS-HARDEN-2 行为验证(用 venv_verify 运行)。

验证:
  1. httpx 网络超时 = 30s(connect=10s)
  2. synthesize 外层硬超时墙: 任意一行死等最多 TTS_HARD_TIMEOUT 必抛 TimeoutError
  3. 1/4~4/4 全景日志按序出现
"""
import asyncio
import logging
import os
import sys
import tempfile

# 避免 __init__ 因无 API KEY 报错
os.environ.setdefault("DASHSCOPE_API_KEY", "test-key-for-import")

import app.providers.tts.cosyvoice as cosyvoice
from app.providers.tts.cosyvoice import CosyVoiceProvider

logging.basicConfig(level=logging.INFO, format="%(message)s")
log_capture = []

orig_info = logging.getLogger("app.providers.tts.cosyvoice").info


def _patched_info(msg, *a, **k):
    s = msg % a if a else str(msg)
    if "[TTS]" in s or "1/4" in s or "2/4" in s or "3/4" in s or "4/4" in s:
        log_capture.append(s)
    orig_info(msg, *a, **k)


logging.getLogger("app.providers.tts.cosyvoice").info = _patched_info


def test_timeout_constant():
    p = CosyVoiceProvider()
    assert p.timeout.read == 30.0, f"read timeout 应为30,实际 {p.timeout.read}"
    assert p.timeout.connect == 10.0, f"connect timeout 应为10,实际 {p.timeout.connect}"
    print("✅ [1] httpx 网络超时 = 30s (connect=10s) 正确")


async def test_hard_wall():
    # 把硬超时墙调小便于测试
    cosyvoice.TTS_HARD_TIMEOUT = 1.0
    p = CosyVoiceProvider()

    async def _hang(self, text, voice, output_path):
        await asyncio.sleep(10)  # 模拟死等

    p._synthesize_impl = _hang.__get__(p, CosyVoiceProvider)
    t0 = asyncio.get_event_loop().time()
    try:
        await p.synthesize("hello", "longwan_v2", "x.mp3")
        print("❌ 硬超时墙失效: 未抛出 TimeoutError")
        sys.exit(1)
    except TimeoutError as e:
        dt = asyncio.get_event_loop().time() - t0
        assert dt < 3.0, f"硬墙应在 ~1s 触发,实际 {dt:.2f}s"
        print(f"✅ [2] 硬超时墙生效: {dt:.2f}s 后抛出 TimeoutError -> {e}")


async def test_log_sequence():
    cosyvoice.TTS_HARD_TIMEOUT = 75.0
    p = CosyVoiceProvider()

    async def _fake_request(self, text, voice):
        return "http://fake/audio.mp3"

    async def _fake_download(self, url, output_path):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"ID3 dummy mp3 bytes")
        return output_path

    async def _fake_duration(self, path):
        return 1.23

    p._request_tts = _fake_request.__get__(p, CosyVoiceProvider)
    p._download_audio = _fake_download.__get__(p, CosyVoiceProvider)
    p._get_audio_duration = _fake_duration.__get__(p, CosyVoiceProvider)

    d = os.path.join(tempfile.gettempdir(), "tts_harden_test")
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "a.mp3")
    try:
        res = await p.synthesize("这是一段测试旁白文本", "longxiaochun_v2", out)
        assert abs(res.duration - 1.23) < 1e-6
        assert os.path.getsize(out) > 0

        markers = [m for m in log_capture if any(f"{i}/4" in m for i in range(1, 5))]
        print("捕获到的流程日志:")
        for m in markers:
            print("   ", m)
        assert len(markers) == 4, f"应捕获 4 条 1/4~4/4 日志,实际 {len(markers)}"
        for i in range(1, 4):
            assert markers[i - 1] in log_capture and markers[i] in log_capture
            assert log_capture.index(markers[i - 1]) < log_capture.index(markers[i]), \
                "日志顺序必须是 1/4 < 2/4 < 3/4 < 4/4"
        print("✅ [3] 1/4~4/4 全景日志按序出现,且返回 duration=1.23")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


async def main():
    test_timeout_constant()
    await test_hard_wall()
    await test_log_sequence()
    print("\n🎉 全部 TTS-HARDEN-2 行为测试通过")


asyncio.run(main())
