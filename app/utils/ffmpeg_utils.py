"""FFmpeg 可执行文件定位与自动下载兜底。

优先级(依次尝试,成功即返回):
    1. settings.FFMPEG_PATH(用户自定义)
    2. imageio-ffmpeg 自带二进制(免系统安装,默认路径)
    3. 项目根目录 bin/ffmpeg.exe(之前自动下载缓存)
    4. 从镜像自动下载并解压到 bin/ffmpeg.exe(终极兜底)

将最终路径写入 IMAGEIO_FFMPEG_EXE / FFMPEG_BINARY 环境变量,
让 ffmpeg-python 及任何依赖环境变量的库都能命中,彻底消灭
"No ffmpeg exe could be found" 报错。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根目录(ai-video-commerce/)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _PROJECT_ROOT / "bin"
_LOCAL_FFMPEG = _BIN_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")

# 镜像下载地址(BtbN GPL 构建,Win64)
# 注:此 URL 为兜底,仅当 imageio-ffmpeg 二进制损坏/缺失时触发
_FFMPEG_DOWNLOAD_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)


def ensure_ffmpeg_exe() -> str:
    """确保返回可用的 ffmpeg 可执行文件路径(终极兜底)。

    按优先级依次尝试,成功即返回;全部失败则抛 RuntimeError。
    返回前将路径写入环境变量 IMAGEIO_FFMPEG_EXE / FFMPEG_BINARY。
    """
    # 1. 用户自定义路径
    try:
        from app.core.config import settings
        if getattr(settings, "FFMPEG_PATH", None) and Path(settings.FFMPEG_PATH).exists():
            _apply_env(settings.FFMPEG_PATH)
            logger.info("[ffmpeg_utils] 使用自定义 FFMPEG_PATH: %s", settings.FFMPEG_PATH)
            return settings.FFMPEG_PATH
    except Exception:  # noqa: BLE001
        pass

    # 2. imageio-ffmpeg 自带二进制(默认路径,绝大多数情况命中此处)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            _apply_env(exe)
            logger.info("[ffmpeg_utils] 使用 imageio-ffmpeg 自带二进制: %s", exe)
            return exe
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ffmpeg_utils] imageio-ffmpeg 解析失败: %s", exc)

    # 3. 本地 bin/ 缓存(之前自动下载过)
    if _LOCAL_FFMPEG.exists():
        _apply_env(str(_LOCAL_FFMPEG))
        logger.info("[ffmpeg_utils] 使用本地缓存: %s", _LOCAL_FFMPEG)
        return str(_LOCAL_FFMPEG)

    # 4. 终极兜底:从镜像下载
    logger.warning(
        "[ffmpeg_utils] 所有快速路径均失败,开始从镜像下载 FFmpeg 到 %s",
        _LOCAL_FFMPEG,
    )
    exe = _download_and_extract()
    _apply_env(exe)
    logger.info("[ffmpeg_utils] 下载完成,使用: %s", exe)
    return exe


def _apply_env(exe_path: str) -> None:
    """将 ffmpeg 路径写入环境变量,供 ffmpeg-python 等库读取。"""
    os.environ["IMAGEIO_FFMPEG_EXE"] = exe_path
    os.environ["FFMPEG_BINARY"] = exe_path


def _download_and_extract() -> str:
    """从镜像下载 FFmpeg 压缩包并解压出可执行文件到 bin/。"""
    _BIN_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _BIN_DIR / "ffmpeg_download.zip"

    logger.info("[ffmpeg_utils] 下载: %s", _FFMPEG_DOWNLOAD_URL)
    urllib.request.urlretrieve(_FFMPEG_DOWNLOAD_URL, zip_path)

    # 解压:在 zip 中查找 ffmpeg 可执行文件
    target_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    with zipfile.ZipFile(zip_path, "r") as zf:
        ffmpeg_entry = None
        for name in zf.namelist():
            if Path(name).name == target_name:
                ffmpeg_entry = name
                break
        if not ffmpeg_entry:
            raise RuntimeError(
                f"下载的压缩包中未找到 {target_name}: {zf.namelist()[:10]}"
            )
        zf.extract(ffmpeg_entry, _BIN_DIR)
        extracted = _BIN_DIR / ffmpeg_entry

    # 移动到 bin/ffmpeg(.exe) 固定路径
    shutil.move(str(extracted), str(_LOCAL_FFMPEG))

    # 清理 zip
    zip_path.unlink(missing_ok=True)
    # 赋予执行权限(非 Windows)
    if sys.platform != "win32":
        _LOCAL_FFMPEG.chmod(0o755)

    if not _LOCAL_FFMPEG.exists():
        raise RuntimeError(f"解压后未找到 {_LOCAL_FFMPEG}")
    return str(_LOCAL_FFMPEG)
