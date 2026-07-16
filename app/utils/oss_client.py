"""阿里云 OSS 图片上传工具。

将本地图片字节流上传到 OSS,返回公网可访问 URL。
用途:用户上传的产品图 -> (抠图) -> OSS -> 公网 URL -> 通义万相 background-generation。

上传路径含日期+UUID 防重名:uploads/yyyyMMdd/uuid.ext
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_oss_configured() -> bool:
    """检查 OSS 是否已配置(必备项非空)。"""
    return bool(
        settings.OSS_ACCESS_KEY_ID
        and settings.OSS_ACCESS_KEY_SECRET
        and settings.OSS_BUCKET_NAME
    )


def upload_image_to_oss(file_content: bytes, filename: str) -> str:
    """上传图片字节流到 OSS,返回公网 URL。

    Args:
        file_content: 图片字节流
        filename: 原始文件名(用于推断扩展名)

    Returns:
        公网可访问的图片 URL

    Raises:
        RuntimeError: OSS 未配置或上传失败
    """
    if not is_oss_configured():
        raise RuntimeError(
            "OSS 未配置,请在 .env 中设置 OSS_ACCESS_KEY_ID / "
            "OSS_ACCESS_KEY_SECRET / OSS_BUCKET_NAME / OSS_ENDPOINT"
        )

    import oss2

    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    bucket = oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME)

    ext = Path(filename).suffix or ".png"
    date_str = datetime.now().strftime("%Y%m%d")
    object_key = f"uploads/{date_str}/{uuid.uuid4().hex}{ext}"

    bucket.put_object(object_key, file_content, headers={"Content-Type": "image/png"})
    url = _build_url(object_key)
    logger.info("[OSS] 上传成功: %s (%d bytes) -> %s", object_key, len(file_content), url)
    return url


def upload_video_to_oss(file_content: bytes, filename: str) -> str:
    """V15.0: 上传视频字节流到 OSS,返回公网 URL。

    用于"拍同款"功能:用户上传参考视频(MP4)→ OSS → 公网 URL → Qwen-VL 分析。

    Args:
        file_content: 视频字节流
        filename: 原始文件名(用于推断扩展名)

    Returns:
        公网可访问的视频 URL
    """
    if not is_oss_configured():
        raise RuntimeError(
            "OSS 未配置,请在 .env 中设置 OSS_ACCESS_KEY_ID / "
            "OSS_ACCESS_KEY_SECRET / OSS_BUCKET_NAME / OSS_ENDPOINT"
        )

    import oss2

    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    bucket = oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME)

    ext = Path(filename).suffix or ".mp4"
    date_str = datetime.now().strftime("%Y%m%d")
    object_key = f"videos/reference/{date_str}/{uuid.uuid4().hex}{ext}"

    content_type = "video/mp4" if ext.lower() in (".mp4", ".m4v") else "application/octet-stream"
    bucket.put_object(object_key, file_content, headers={"Content-Type": content_type})
    url = _build_url(object_key)
    logger.info("[OSS] 视频上传成功: %s (%d bytes) -> %s", object_key, len(file_content), url)
    return url


def _build_url(object_key: str) -> str:
    """拼接公网访问 URL。"""
    if settings.OSS_BASE_URL:
        return f"{settings.OSS_BASE_URL.rstrip('/')}/{object_key}"
    endpoint = settings.OSS_ENDPOINT
    if endpoint.startswith("https://"):
        endpoint = endpoint[len("https://"):]
    elif endpoint.startswith("http://"):
        endpoint = endpoint[len("http://"):]
    return f"https://{settings.OSS_BUCKET_NAME}.{endpoint}/{object_key}"


def delete_oss_object(url: str) -> bool:
    """V14.0: 根据 URL 删除 OSS 对象(best-effort,失败仅记日志不抛异常)。"""
    if not url or not is_oss_configured():
        return False
    try:
        import oss2
        auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
        bucket = oss2.Bucket(auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME)
        # 从 URL 中提取 object_key
        if settings.OSS_BASE_URL and url.startswith(settings.OSS_BASE_URL):
            object_key = url[len(settings.OSS_BASE_URL.rstrip('/')) + 1:]
        else:
            prefix = f"https://{settings.OSS_BUCKET_NAME}."
            if url.startswith(prefix):
                rest = url[len(prefix):]
                object_key = rest.split("/", 1)[1] if "/" in rest else ""
            else:
                return False
        if object_key:
            bucket.delete_object(object_key)
            logger.info("[OSS] 删除成功: %s", object_key)
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OSS] 删除失败(best-effort,忽略): %s", exc)
    return False
