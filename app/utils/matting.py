"""抠图工具:白底图 -> 透明 PNG(基于 rembg,本地运行)。

用途:
    通义万相 background-generation 接口要求 base_image_url 为公网可访问的
    透明背景 RGBA PNG。用户提供的"白底图"需先经抠图处理。

说明:
    - 本工具仅负责抠图(本地 rembg,U2Net 模型),不涉及公网上传。
    - 抠图后的透明 PNG 若要供 dashscope 访问,需上传到公网(OSS / 图床 / ngrok)。
    - 开发期测试可直接使用已是透明 PNG 的公网 URL,跳过本步骤。

依赖:
    pip install rembg  (首次使用会自动下载约 170MB 的 U2Net 模型)
    或 pip install "rembg[cpu]"  (CPU 版)
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def remove_background(input_path: str, output_path: str) -> str:
    """将输入图片抠图为透明 PNG。

    Args:
        input_path: 输入图片路径(白底图或其他格式)
        output_path: 输出透明 PNG 路径

    Returns:
        输出透明 PNG 路径
    """
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError(
            "未安装 rembg,请运行: pip install rembg"
        ) from exc

    import io
    from PIL import Image

    input_bytes = Path(input_path).read_bytes()
    output_bytes = remove(input_data=input_bytes)
    # 加固:确保输出为 RGBA 透明 PNG(rembg 默认即如此,此处显式校验)
    img = Image.open(io.BytesIO(output_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    out = Path(output_path)
    if out.suffix.lower() != ".png":
        out = out.with_suffix(".png")
    img.save(out, format="PNG")
    logger.info("[Matting] 抠图完成(RGBA PNG, mode=%s): %s -> %s", img.mode, input_path, out)
    return str(out)
