"""图片尺寸与格式自适应工具。

通义万相 wanx-background-generation-v2 对 base_image_url 有严格要求:
    - 必须为公网可访问的透明背景 RGBA PNG
    - 分辨率范围:200x200 ~ 4096x4096
    - 长宽比不能过于极端(否则会被拒)

本模块在图片上传到 OSS 之前进行规范化处理,确保格式合法,
从根源避免 "InvalidParameter / 图片尺寸不合法" 类错误。

应用于 POST /api/projects 的两条分支:
    - SKIP_MATTING=True:对原图直接 normalize
    - SKIP_MATTING=False:对抠图后的透明 PNG 再 normalize(兜底)
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# 通义万相图片尺寸限制(官方文档)
MIN_SIZE = 200
MAX_SIZE = 4096
# 长宽比上限(超过此值则居中裁剪,避免极端比例被拒)
MAX_ASPECT_RATIO = 2.5


def normalize_to_rgba_png(input_bytes: bytes) -> bytes:
    """将任意图片字节规范化为符合通义万相要求的 RGBA PNG。

    处理流程:
        1. 转为 RGBA 模式(透明背景)
        2. 长宽比过极端时居中裁剪到 MAX_ASPECT_RATIO
        3. 尺寸过小则等比放大,过大则等比缩小(限定在 200~4096)
        4. 强制 PNG 格式输出

    Args:
        input_bytes: 原始图片字节(JPG/PNG/WebP 等均可)

    Returns:
        规范化后的 PNG 字节流(RGBA 模式)
    """
    from PIL import Image

    # Pillow 10+ 移除了 Image.LANCZOS 常量,改用 Image.Resampling.LANCZOS
    try:
        RESAMPLE = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover - 旧版 Pillow
        RESAMPLE = Image.LANCZOS

    img = Image.open(io.BytesIO(input_bytes))
    original_mode = img.mode
    orig_w, orig_h = img.size

    # 1. 转 RGBA(透明背景)
    if img.mode != "RGBA":
        # 调色板模式先转 RGB 再转 RGBA,避免透明信息丢失
        if img.mode == "P":
            img = img.convert("RGBA")
        else:
            img = img.convert("RGBA")

    # 2. 长宽比裁剪(过宽或过高都会被通义万相拒绝)
    w, h = img.size
    short = min(w, h)
    aspect = max(w, h) / short if short > 0 else 1.0
    if aspect > MAX_ASPECT_RATIO:
        if w > h:
            new_w = int(h * MAX_ASPECT_RATIO)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w * MAX_ASPECT_RATIO)
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))
        logger.info(
            "[image_utils] 长宽比 %.2f 超限,居中裁剪 %dx%d -> %dx%d (ratio=%.2f)",
            aspect, w, h, img.size[0], img.size[1], MAX_ASPECT_RATIO,
        )

    # 3. 尺寸调整(过小放大,过大缩小)
    w, h = img.size
    if w < MIN_SIZE or h < MIN_SIZE:
        scale = max(MIN_SIZE / w, MIN_SIZE / h)
        new_w = max(MIN_SIZE, int(w * scale))
        new_h = max(MIN_SIZE, int(h * scale))
        img = img.resize((new_w, new_h), RESAMPLE)
        logger.info(
            "[image_utils] 尺寸过小,放大 %dx%d -> %dx%d",
            w, h, new_w, new_h,
        )
    elif w > MAX_SIZE or h > MAX_SIZE:
        scale = min(MAX_SIZE / w, MAX_SIZE / h)
        new_w = min(MAX_SIZE, int(w * scale))
        new_h = min(MAX_SIZE, int(h * scale))
        img = img.resize((new_w, new_h), RESAMPLE)
        logger.info(
            "[image_utils] 尺寸过大,缩小 %dx%d -> %dx%d",
            w, h, new_w, new_h,
        )

    # 4. 强制 PNG 输出
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    output = buf.getvalue()
    logger.info(
        "[image_utils] 规范化完成 mode=%s->RGBA, %dx%d->%dx%d, PNG %d bytes",
        original_mode, orig_w, orig_h, img.size[0], img.size[1], len(output),
    )
    return output


# 白底转透明的阈值(R/G/B 均大于此值视为背景白)
_WHITE_THRESHOLD = 220

# V6.1 去白边强度配置: (腐蚀像素, 羽化半径)
# erosion_px: alpha 通道向内收缩的像素数(切掉白边)
# feather_radius: 高斯模糊半径(让边缘过渡自然,避免收缩后过于锐利)
_DEFRINGE_PARAMS: dict[str, tuple[int, float]] = {
    "off":    (0, 0.0),   # 不处理
    "light":  (1, 0.5),   # 轻度: 腐蚀1px, 轻微羽化
    "medium": (2, 1.0),   # 中度: 腐蚀2px, 标准羽化(默认, 适合大多数白底图)
    "heavy":  (3, 2.0),   # 重度: 腐蚀3px, 强羽化(适合白边极严重/反光的图)
}


def defringe_alpha(image_bytes: bytes, strength: str = "medium") -> bytes:
    """对 RGBA 透明 PNG 进行边缘腐蚀与羽化(消灭 rembg 抠图后的白边/Halo effect)。

    工业级边缘后处理流程:
        1. 提取 Alpha 通道
        2. 形态学腐蚀(cv2.erode): 将 alpha 向内收缩 N 像素,直接切掉最外层白边
        3. 高斯模糊(cv2.GaussianBlur): 对腐蚀后的 alpha 边缘轻微模糊,让过渡自然
        4. 重组 RGB + 处理后 Alpha → RGBA PNG

    Args:
        image_bytes: RGBA PNG 字节流(通常是 rembg.remove() 的输出)
        strength: 去白边强度 off/light/medium/heavy

    Returns:
        处理后的 RGBA PNG 字节流(strength=off 时原样返回)
    """
    if strength == "off":
        return image_bytes

    erosion_px, feather_radius = _DEFRINGE_PARAMS.get(
        strength, _DEFRINGE_PARAMS["medium"]
    )
    if erosion_px == 0:
        return image_bytes

    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    try:
        import cv2
        import numpy as np

        # 分离 RGBA 通道
        rgba = np.array(img)
        rgb = rgba[:, :, :3]
        alpha = rgba[:, :, 3]

        # 1. 形态学腐蚀: 用椭圆 kernel 向内收缩 alpha
        kernel_size = erosion_px * 2 + 1
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)
        )
        alpha_eroded = cv2.erode(alpha, kernel, iterations=1)

        # 2. 高斯羽化: 让腐蚀后的边缘过渡自然
        if feather_radius > 0:
            blur_size = int(feather_radius * 2) | 1  # 必须为奇数
            alpha_eroded = cv2.GaussianBlur(
                alpha_eroded, (blur_size, blur_size), feather_radius
            )

        # 3. 重组 RGBA
        rgba_out = np.dstack([rgb, alpha_eroded])
        out_img = Image.fromarray(rgba_out, "RGBA")

        buf = io.BytesIO()
        out_img.save(buf, format="PNG")
        out = buf.getvalue()
        logger.info(
            "[image_utils] defringe_alpha: strength=%s, erode=%dpx, feather=%.1f, "
            "PNG %d->%d bytes",
            strength, erosion_px, feather_radius, len(image_bytes), len(out),
        )
        return out

    except ImportError:
        # cv2 不可用时用 Pillow 降级: MinFilter 腐蚀 + GaussianBlur 羽化
        logger.warning(
            "[image_utils] cv2 未安装,降级 Pillow 腐蚀(strength=%s)", strength
        )
        alpha = img.split()[3]
        # MinFilter(N) 等效于对 alpha 做最小值滤波(腐蚀)
        alpha_eroded = alpha.filter(
            Image.MinFilter(erosion_px * 2 + 1)
        )
        if feather_radius > 0:
            alpha_eroded = alpha_eroded.filter(
                Image.GaussianBlur(radius=feather_radius)
            )
        rgb = img.split()[:3]
        out_img = Image.merge("RGBA", (*rgb, alpha_eroded))
        buf = io.BytesIO()
        out_img.save(buf, format="PNG")
        out = buf.getvalue()
        logger.info(
            "[image_utils] defringe_alpha(Pillow降级): strength=%s, erode=%dpx, "
            "PNG %d->%d bytes",
            strength, erosion_px, len(image_bytes), len(out),
        )
        return out


def _white_to_transparent(img) -> bytes:
    """将 RGB/P 等非透明图片按"近白像素置透明"规则转为 RGBA 透明 PNG。

    兜底算法:R/G/B 三通道均 > 阈值 → alpha=0(透明),否则 alpha=255(不透明)。
    适用于白底产品图的快速去背(无需 rembg),效果不如 AI 抠图但可保证格式合法。
    """
    from PIL import Image

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    datas = img.getdata()
    new_data = []
    transparent = (0, 0, 0, 0)
    for item in datas:
        r, g, b = item[0], item[1], item[2]
        if r > _WHITE_THRESHOLD and g > _WHITE_THRESHOLD and b > _WHITE_THRESHOLD:
            new_data.append(transparent)
        else:
            new_data.append((r, g, b, 255))
    img.putdata(new_data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def ensure_rgba_transparent(
    image_bytes: bytes,
    skip_matting: bool,
    defringe_strength: str = "medium",
) -> bytes:
    """确保图片为通义万相要求的 RGBA 透明 PNG(核心格式兜底)。

    通义万相 wanx-background-generation 严格要求 base_image 为 RGBA 格式,
    否则报 `BadRequest.UnsupportedFileFormat: Base image require RGBA format`。
    本函数在上传 OSS 前统一兜底,无论用户传什么格式、无论是否跳过抠图,
    都保证输出是 RGBA 透明 PNG。

    V6.1 新增: rembg 抠图后自动进行边缘腐蚀与羽化(去白边/Defringing),
    仅对 rembg 输出生效;已是 RGBA 的高质量透明 PNG 和白底转透明兜底均跳过。

    Args:
        image_bytes: 原始图片字节(JPG/PNG/WebP/RGB/RGBA 均可)
        skip_matting: True=不调 rembg(用白底转透明兜底);
                      False=调 rembg AI 抠图(rembg 不可用时回退白底转透明)
        defringe_strength: 去白边强度 off/light/medium/heavy,
                           仅 rembg 抠图后生效(已是 RGBA 或白底转透明时跳过)

    Returns:
        RGBA 透明 PNG 字节流
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    original_mode = img.mode

    # 已经是 RGBA:直接返回(用户自备的透明 PNG,不做腐蚀以免破坏完美边缘)
    if img.mode == "RGBA":
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = buf.getvalue()
        logger.info(
            "[image_utils] ensure_rgba: 已是 RGBA,直接返回 PNG %d bytes (跳过去白边)",
            len(out),
        )
        return out

    # 非 RGBA:需要产生透明通道
    if not skip_matting:
        # 正常路径:调用 rembg AI 抠图(效果最佳)
        try:
            from rembg import remove

            output_bytes = remove(input_data=image_bytes)
            out_img = Image.open(io.BytesIO(output_bytes))
            if out_img.mode != "RGBA":
                out_img = out_img.convert("RGBA")
            buf = io.BytesIO()
            out_img.save(buf, format="PNG")
            rembg_out = buf.getvalue()

            # V6.1 去白边:仅对 rembg 输出进行边缘腐蚀与羽化
            out = defringe_alpha(rembg_out, defringe_strength)
            logger.info(
                "[image_utils] ensure_rgba: rembg 抠图+去白边(%s)完成 mode=%s->RGBA, PNG %d bytes",
                defringe_strength, original_mode, len(out),
            )
            return out
        except ImportError:
            logger.warning(
                "[image_utils] rembg 未安装,回退白底转透明算法"
                "(如需更好效果请运行: pip install rembg onnxruntime)"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[image_utils] rembg 抠图失败,回退白底转透明: %s", exc
            )

    # skip_matting=True 或 rembg 不可用:白底转透明兜底(不做去白边)
    out = _white_to_transparent(img)
    logger.info(
        "[image_utils] ensure_rgba: 白底转透明 mode=%s->RGBA, PNG %d bytes (跳过去白边)",
        original_mode, len(out),
    )
    return out
