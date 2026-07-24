"""HappyHorse 1.1 图生视频 Provider(阿里云百炼最新模型)。

V14.2 重要修复:基于官方文档 https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference
    1. model 修正为 happyhorse-1.1-i2v(图生视频专用,非 happyhorse-1.1)
    2. input 结构改为 media 数组(type=first_frame),非 img_url 字段
    3. 移除 negative_prompt/prompt_extend(I2V 官方不支持)
    4. 新增 watermark=false(去水印,商业用途)
    5. 强制追加 VIDEO_QUALITY_SUFFIX 画质增强后缀到 prompt 末尾

官方 I2V 支持的 parameters 仅有: resolution / duration / watermark / seed
(不支持 motion_scale / cfg_scale / negative_prompt — 这些参数在 HappyHorse I2V API 中不存在)

参考文档:
    https://help.aliyun.com/zh/model-studio/happyhorse-image-to-video-api-reference
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.core.config import settings
from app.providers.video.base import IVideoProvider, VideoResult
from app.utils.oss_client import is_oss_configured, upload_image_to_oss
from app.utils.prompt_templates import compose_final_video_prompt

logger = logging.getLogger(__name__)

# loopback / 内网地址:阿里云服务器无法下载,必须中转
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}

# 百炼视频生成端点(与通义万相共用)
CREATE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "video-generation/video-synthesis"
)
TASK_URL_TEMPLATE = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

POLL_INTERVAL = 5.0
POLL_MAX_ATTEMPTS = 120  # 10 分钟超时


# ---------------------------------------------------------------------------
# 关键帧图片中转:确保传给 HappyHorse 的 URL 阿里云服务器绝对可访问
# ---------------------------------------------------------------------------

def _is_public_http_url(value: str) -> bool:
    """判断是否为公网可访问的 http(s) URL(排除 loopback/内网)。"""
    low = value.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return False
    host = (urlparse(value).hostname or "").lower()
    return host not in _LOOPBACK_HOSTS


def _read_local_file_as_bytes(raw: str) -> tuple[bytes, str]:
    """读取本地图片文件为字节流(支持 file:// 与绝对路径)。"""
    path = raw
    if path.startswith("file://"):
        path = path[len("file://"):]
    # Windows 盘符 file:///C:/... 处理
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"关键帧图片本地文件不存在: {raw}")
    return p.read_bytes(), p.name or "keyframe.png"


def _decode_data_uri(raw: str) -> tuple[bytes, str]:
    """解析 data:image/png;base64,xxxx 为字节流 + 文件名。"""
    try:
        header, b64 = raw.split(",", 1)
        ext = ".png"
        if "jpeg" in header or "jpg" in header:
            ext = ".jpg"
        elif "webp" in header:
            ext = ".webp"
        elif "gif" in header:
            ext = ".gif"
        data = base64.b64decode(b64)
        return data, f"keyframe{ext}"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"关键帧图片 data URI 解析失败: {exc}") from exc


async def _download_bytes(url: str) -> bytes:
    """从公网 URL 下载图片字节(供云端场景落盘后经自有公网暴露)。"""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
    ) as client:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(
                f"关键帧图片源 URL 下载失败 (HTTP {resp.status_code}): {url}"
            )
        return resp.content


async def _normalize_to_render_public_url(raw_url: str) -> str:
    """云端(Render)策略: 将任意来源的关键帧图统一落盘到 STORAGE_ROOT/temp,
    再经 RENDER_EXTERNAL_URL 暴露为公网 URL, 彻底规避阿里云下载
    本地路径 / 临时签名 URL / data URI 的各类难题, 且无需依赖 OSS。

    返回形如: https://<app>.onrender.com/storage/temp/keyframe_<uuid>.png
    """
    external = os.getenv("RENDER_EXTERNAL_URL")
    if not external:
        raise RuntimeError("RENDER_EXTERNAL_URL 未注入,无法生成云端公网图片 URL")

    # 1) 解析为字节(三种来源: data URI / 远程公网 / 本地路径)
    if raw_url.startswith("data:"):
        data, fname = _decode_data_uri(raw_url)
    elif _is_public_http_url(raw_url):
        # 远程公网图(如 DashScope 结果 URL)先下载落盘, 规避签名过期 / CORS
        data = await _download_bytes(raw_url)
        fname = "keyframe.png"
    else:
        # 本地路径 / file:// -> 读取字节
        data, fname = _read_local_file_as_bytes(raw_url)

    # 2) 落盘到 STORAGE_ROOT/temp(StaticFiles 已挂载 /storage -> STORAGE_ROOT)
    storage_root = Path(settings.STORAGE_ROOT)
    temp_dir = storage_root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(fname).suffix or ".png"
    safe_name = f"keyframe_{uuid.uuid4().hex}{ext}"
    dest = temp_dir / safe_name
    await asyncio.to_thread(dest.write_bytes, data)

    # 3) 拼接公网 URL
    base = external.rstrip("/")
    return f"{base}/storage/temp/{safe_name}"


async def _verify_public_image_url(url: str) -> None:
    """公网 URL 连通性自检: 调用 HappyHorse 前先自己 GET 一次。

    - data URI 等内联形式跳过(无网络端点可验)
    - 非 200 直接抛错(如 404 说明 StaticFiles 挂载或路径有误),
      避免盲目浪费 API 调用
    - 仅自连网络异常时降级为告警(不阻塞), 因为文件已落盘且 /storage
      挂载正确, 真实可达性由 HappyHorse 服务器最终判定
    """
    if not url.startswith("http"):
        return
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True
        ) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[HappyHorse] 公网图片 URL 自检网络异常(已跳过, 不阻塞): %s | %s",
            url, exc,
        )
        return
    if resp.status_code != 200:
        raise RuntimeError(
            f"[HappyHorse] 图片公网 URL 无法访问(HTTP {resp.status_code}),"
            f"请检查 StaticFiles 挂载(/storage)是否覆盖该文件路径: {url}"
        )
    logger.info("[HappyHorse] 图片公网 URL 连通性自检通过 (HTTP 200): %s", url)


async def _upload_to_oss_or_base64(data: bytes, fname: str) -> str:
    """方案A(优先):上传 OSS 拿公网 URL;方案B(OSS 未配置):降级 Base64 data URI。"""
    if is_oss_configured():
        try:
            public_url = await asyncio.to_thread(
                upload_image_to_oss, data, fname
            )
            logger.info(
                "[HappyHorse] 关键帧图片已中转上传 OSS -> %s", public_url
            )
            return public_url
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"关键帧图片上传 OSS 失败(中转环节): {exc}"
            ) from exc
    # 方案B 兜底: 无 OSS -> Base64 data URI(仅当百炼 HappyHorse 支持时有效)
    logger.warning(
        "[HappyHorse] OSS 未配置, 退化为 base64 data URI 传图"
        "(若百炼不支持将失败, 强烈建议配置 OSS)"
    )
    mime = mimetypes.guess_type(fname)[0] or "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def ensure_public_image_url(raw_url: str) -> str:
    """将任意来源的关键帧图片 URL 转换为 HappyHorse 绝对可访问的公网 URL。

    云端(Render)优先策略(V17.6): 一旦检测到 RENDER_EXTERNAL_URL,
    直接将图片落盘到 /storage/temp 并经 RENDER_EXTERNAL_URL 暴露,
    彻底规避阿里云下载本地路径 / 临时签名 URL / data URI 的各类难题,
    且无需依赖 OSS(云端未配置 OSS 时旧逻辑会退化成被拒的 Base64)。

    非云端(本地桌面客户端)沿用原有逻辑:
      - 公网 http(s) URL -> 直接复用
      - data: URI -> 解码后中转(OSS 方案A / Base64 方案B)
      - 本地路径 / loopback -> 读取后中转(OSS 方案A / Base64 方案B)
    """
    if not raw_url or not raw_url.strip():
        raise RuntimeError("关键帧图片 URL 为空,无法生成视频")
    raw_url = raw_url.strip()

    # 云端: 统一经自有公网 /storage 暴露(最稳, 不依赖 OSS)
    if os.getenv("RENDER_EXTERNAL_URL"):
        try:
            return await _normalize_to_render_public_url(raw_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HappyHorse] Render 公网暴露失败, 回落 OSS/Base64 兜底: %s", exc
            )

    # 原有逻辑(本地 / 兜底)
    # data URI: 解码后中转
    if raw_url.startswith("data:"):
        data, fname = _decode_data_uri(raw_url)
        return await _upload_to_oss_or_base64(data, fname)

    # 已是公网可访问 URL -> 透传
    if _is_public_http_url(raw_url):
        logger.info(
            "[HappyHorse] 关键帧图片已是公网 URL, 直接复用: %s", raw_url
        )
        return raw_url

    # 本地文件 / loopback -> 读取字节中转
    data, fname = _read_local_file_as_bytes(raw_url)
    return await _upload_to_oss_or_base64(data, fname)


class HappyHorseVideoProvider(IVideoProvider):
    """HappyHorse 1.1 图生视频 Provider(百炼最新模型)。

    V14.2: 修正 model 名和 input 结构以匹配官方 I2V API 规范。
    """

    PROVIDER_NAME = "happyhorse-1.1-i2v"

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.api_key = settings.DASHSCOPE_API_KEY
        # V14.2: 官方 I2V model 名为 happyhorse-1.1-i2v
        self.model = "happyhorse-1.1-i2v"
        self.resolution = settings.VIDEO_RESOLUTION
        self.default_duration = settings.VIDEO_DURATION
        self.timeout = httpx.Timeout(30.0, connect=10.0)

    async def generate_video(
        self,
        keyframe_image_url: str,
        video_prompt: str,
        duration: int = 5,
    ) -> VideoResult:
        """图生视频:首帧图 + 运镜指令 -> 视频 URL。"""
        if not video_prompt or not video_prompt.strip():
            raise RuntimeError(
                "video_prompt(运镜指令)为空,拒绝调用以避免默认推拉摇移"
            )

        # V21.0 产品灵魂注入与防形变:统一经 compose_final_video_prompt 拼装。
        # 该函数会:① 末尾强制追加 ANTI_DEFORMATION_SUFFIX 防形变咒语;
        # ② 先截断正文再拼后缀(咒语永不被截断丢失);③ 总词数≤150 防 Token 超限。
        original_word_count = len(video_prompt.split())
        final_prompt = compose_final_video_prompt(video_prompt)
        final_word_count = len(final_prompt.split())
        logger.info(
            "[%s] video_prompt 词数: 原始=%d, 最终=%d (安全区间≤150, 已含防形变咒语)",
            self.PROVIDER_NAME, original_word_count, final_word_count,
        )

        logger.info(
            "[%s] 提交 HappyHorse I2V 任务,首帧=%s, 运镜=%s",
            self.PROVIDER_NAME,
            keyframe_image_url,
            final_prompt[:80],
        )

        # V17.1 紧急修复 + V17.6 云端公网穿透: 将本地路径/loopback/data URI/
        # 远程签名 URL 统一中转为 HappyHorse 绝对可访问的公网 URL。
        # 云端优先经 RENDER_EXTERNAL_URL + /storage 自有挂载暴露, 无需 OSS。
        public_image_url = await ensure_public_image_url(keyframe_image_url)
        logger.info("[HappyHorse] 准备使用公网图片 URL: %s", public_image_url)
        # V17.6: 调用 API 前公网 URL 连通性自检(内联 data URI 跳过),
        # 非 200 直接抛错, 避免盲目浪费 API 调用。
        if public_image_url.startswith("http"):
            await _verify_public_image_url(public_image_url)
        if public_image_url != keyframe_image_url:
            logger.info(
                "[%s] 关键帧图片已中转为公网 URL(原=%s)",
                self.PROVIDER_NAME, keyframe_image_url[:80],
            )

        task_id = await self._submit_task(
            public_image_url, final_prompt, duration
        )
        logger.info("[HappyHorse] task_id=%s,开始轮询", task_id)
        video_url = await self._poll_task(task_id)
        logger.info("[HappyHorse] 生成完成: %s", video_url)
        return VideoResult(video_url=video_url, duration=float(duration))

    async def _submit_task(
        self,
        img_url: str,
        prompt: str,
        duration: int,
    ) -> str:
        """提交异步任务,返回 task_id。

        V14.2: 使用官方 I2V input.media 结构(type=first_frame)。
        官方 I2V 仅支持 resolution/duration/watermark/seed 参数。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }

        payload = {
            "model": self.model,
            "input": {
                "prompt": prompt,
                # V14.2: 官方 I2V 使用 media 数组,非 img_url 字段
                "media": [
                    {
                        "type": "first_frame",
                        "url": img_url,
                    }
                ],
            },
            "parameters": {
                "resolution": self.resolution,
                "duration": duration,
                # V14.2: 去水印(官方默认 true 会加 "Happy Horse" 水印)
                "watermark": False,
            },
        }

        # V14.2 模块3: 全链路 Prompt 日志打印(方便人工微调)
        logger.info("=" * 60)
        logger.info("[HappyHorse] 最终 Image URL: %s", img_url)
        logger.info("[HappyHorse] 最终 Video Prompt: %s", prompt)
        logger.info(
            "[HappyHorse] 最终 Negative Prompt: (N/A — 官方 I2V API 不支持 negative_prompt 字段)"
        )
        logger.info(
            "[HappyHorse] 核心参数: model=%s, resolution=%s, duration=%ss, watermark=False",
            self.model,
            self.resolution,
            duration,
        )
        logger.info(
            "[HappyHorse] 注: 官方 I2V API 不支持 motion_scale/cfg_scale/negative_prompt,"
            "动态表现力通过 Prompt 中的电影级运镜词汇控制"
        )
        logger.info("[HappyHorse] Request Payload: %s", payload)
        logger.info(
            "[HappyHorse] >>> 提交请求 model=%s | img_url=%s | prompt=%s",
            self.model, img_url, prompt,
        )
        logger.info("=" * 60)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                CREATE_URL, headers=headers, json=payload
            )
            http_status = resp.status_code
            resp_text = resp.text
            # V16.2: 强制暴露真实死因 —— 非 2xx 直接抛出完整响应体
            logger.error(
                "[HappyHorse] <<< 提交响应 (HTTP %d): %s",
                http_status, resp_text[:1000],
            )
            if http_status < 200 or http_status >= 300:
                # 阿里云 DashScope 欠费/额度停用: 明确提示,避免误判为代码 bug
                if "Arrearage" in resp_text or "overdue" in resp_text.lower():
                    raise RuntimeError(
                        "阿里云 DashScope 视频生成额度已欠费/被停用(HTTP 400 Arrearage)。"
                        "视频生成 API 当前不可用,请到阿里云控制台结清账单或充值后重试;"
                        "图片生成(通义万相)不受影响,可正常出图。原始响应: "
                        + resp_text[:300]
                    )
                raise RuntimeError(
                    f"HappyHorse 提交任务失败 (HTTP {http_status}): {resp_text[:800]}"
                )
            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(
                    f"HappyHorse 返回非 JSON 响应 (HTTP {http_status}): {resp_text[:800]}"
                )
            # DashScope 错误体形如 {"code": "InvalidParameter", "message": "..."}
            err_code = data.get("code")
            err_msg = data.get("message")
            if err_code and err_msg:
                raise RuntimeError(
                    f"HappyHorse 提交任务失败 [code={err_code}]: {err_msg} (HTTP {http_status})"
                )

        task_id = data.get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(
                f"HappyHorse 任务提交失败,未返回 task_id: {data}"
            )
        return task_id

    async def _poll_task(self, task_id: str) -> str:
        """轮询任务状态,返回结果视频 URL。"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = TASK_URL_TEMPLATE.format(task_id=task_id)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
                await asyncio.sleep(POLL_INTERVAL)
                resp = await client.get(url, headers=headers)
                http_status = resp.status_code
                resp_text = resp.text
                # V16.2: 传输层错误(非 2xx)也暴露完整响应体
                if http_status < 200 or http_status >= 300:
                    raise RuntimeError(
                        f"HappyHorse 查询任务失败 (HTTP {http_status}) "
                        f"task_id={task_id}: {resp_text[:800]}"
                    )
                try:
                    data = resp.json()
                except Exception:
                    raise RuntimeError(
                        f"HappyHorse 查询返回非 JSON 响应 (HTTP {http_status}) "
                        f"task_id={task_id}: {resp_text[:800]}"
                    )
                # DashScope 查询接口错误体: {"code": "...", "message": "..."}
                err_code = data.get("code")
                err_msg = data.get("message")
                if err_code and err_msg:
                    raise RuntimeError(
                        f"HappyHorse 任务查询失败 [code={err_code}]: "
                        f"{err_msg} (task_id={task_id})"
                    )

                output = data.get("output", {})
                status = output.get("task_status")
                logger.info(
                    "[HappyHorse] 轮询 %d/%d status=%s",
                    attempt,
                    POLL_MAX_ATTEMPTS,
                    status,
                )

                if status == "SUCCEEDED":
                    return self._extract_video_url(output)

                if status == "FAILED":
                    msg = output.get("message", "未知错误")
                    code = output.get("code", "")
                    raise RuntimeError(
                        f"HappyHorse 视频生成失败[{code}]: {msg}"
                    )

        raise RuntimeError(
            f"HappyHorse 视频生成超时({POLL_MAX_ATTEMPTS * POLL_INTERVAL:.0f}s) "
            f"task_id={task_id}"
        )

    @staticmethod
    def _extract_video_url(output: dict) -> str:
        """从任务输出中提取结果视频 URL(兼容多种返回格式)。"""
        video_url = output.get("video_url")
        if video_url:
            return video_url
        results = output.get("results", [])
        if results:
            url = results[0].get("url") or results[0].get("video_url")
            if url:
                return url
        raise RuntimeError(
            f"HappyHorse 任务成功但未找到结果 URL: {output}"
        )
