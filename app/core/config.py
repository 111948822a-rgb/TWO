"""
应用配置:基于 pydantic-settings 从环境变量加载。

所有敏感信息(API Key)与可调参数均通过 .env 注入,
代码中不硬编码任何密钥。
"""

from __future__ import annotations

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

    # --- 数据库(SQLite,开发期) ---
    DATABASE_URL: str = "sqlite:///./data/data.db"

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    # 开发期同步执行 Celery 任务,无需启动 worker,便于断点调试
    # 生产环境置为 False 并启动 celery worker
    CELERY_EAGER: bool = True

    # --- 存储根目录 ---
    DATA_ROOT: str = "data"
    STORAGE_ROOT: str = "storage"

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
