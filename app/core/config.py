"""
应用配置:基于 pydantic-settings 从环境变量加载。

所有敏感信息(API Key)与可调参数均通过 .env 注入,
代码中不硬编码任何密钥。
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- 应用 ---
    APP_NAME: str = "ai-video-commerce"
    DEBUG: bool = True

    # --- 数据库(SQLite,本地持久化) ---
    # 默认落 用户文档/AIVideoStudio/data(桌面客户端形态);可用 DATABASE_URL 环境变量覆盖
    DATABASE_URL: str = ""

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    # 开发期同步执行 Celery 任务,无需启动 worker,便于断点调试
    # 生产环境置为 False 并启动 celery worker
    CELERY_EAGER: bool = True

    # --- 存储根目录(本地持久化,桌面客户端形态) ---
    # 默认落 用户文档/AIVideoStudio/storage;可用 AIVS_DATA_ROOT / AIVS_STORAGE_ROOT 环境变量覆盖
    DATA_ROOT: str = ""
    STORAGE_ROOT: str = ""

    # --- DeepSeek(LLM) ---
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"

    # --- 阿里云百炼(通义万相图片 / 视频 + CosyVoice) ---
    # 注意:与 dashscope SDK / 旧项目保持一致,使用 DASHSCOPE_API_KEY
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1"

    # --- 通义万相 图生视频 ---
    # wan2.2-i2v-flash:极速版,指令理解与运镜控制更准(契合避坑需求)
    VIDEO_MODEL: str = "wan2.2-i2v-flash"
    # 1080P 提升带货视频清晰度(9:16 竖屏由合成阶段统一保证)
    VIDEO_RESOLUTION: str = "1080P"
    VIDEO_DURATION: int = 5

    # --- CosyVoice TTS ---
    TTS_MODEL: str = "cosyvoice-v2"
    TTS_VOICE: str = "longxiaochun_v2"

    # --- 阶段⑤ FFmpeg 合成 ---
    FFMPEG_PATH: str = ""
    BGM_PATH: str = ""
    TRANSITION_DURATION: float = 0.4
    SUBTITLE_FONT: str = "SimHei"
    OUTPUT_WIDTH: int = 1080
    OUTPUT_HEIGHT: int = 1920
    OUTPUT_FPS: int = 30
    OUTPUT_CRF: int = 20

    # --- 阿里云 OSS ---
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_BUCKET_NAME: str = ""
    OSS_ENDPOINT: str = "oss-cn-hangzhou.aliyuncs.com"
    OSS_BASE_URL: str = ""
    SKIP_MATTING: bool = False


settings = Settings()

# ============================================================================
# 🖥️ 本地存储路径自适应(桌面客户端形态)
# ----------------------------------------------------------------------------
# 不再硬编码 /data(Render 专属)。解析顺序:
#   1. 环境变量 AIVS_DATA_ROOT / AIVS_STORAGE_ROOT 覆盖(服务器/挂载盘场景)
#   2. 否则落到 用户文档目录 / AIVideoStudio / data|storage(Win/Mac 通用)
# 初始化时自动创建目录,保证任意环境下首次启动即可读写。
# 云端部署仍可通过环境变量指向挂载盘(见 render.yaml 的 AIVS_* 配置)。
# ============================================================================
def _resolve_local_dir(sub: str) -> Path:
    """解析本地专属目录:环境变量优先,否则落到 用户文档/AIVideoStudio/<sub>。"""
    env_key = "AIVS_DATA_ROOT" if sub == "data" else "AIVS_STORAGE_ROOT"
    override = os.getenv(env_key)
    if override:
        base = Path(override)
    else:
        base = Path.home() / "Documents" / "AIVideoStudio" / sub
    base.mkdir(parents=True, exist_ok=True)
    return base


_DATA_DIR = _resolve_local_dir("data")
_STORAGE_DIR = _resolve_local_dir("storage")

settings.DATA_ROOT = str(_DATA_DIR)
settings.STORAGE_ROOT = str(_STORAGE_DIR)
if not settings.DATABASE_URL:
    settings.DATABASE_URL = f"sqlite:///{_DATA_DIR / 'data.db'}"
