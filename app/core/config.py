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
# 🗂️ 存储路径解析(双形态自适应)
# ----------------------------------------------------------------------------
#   • 云端(Render): 检测到 RENDER_EXTERNAL_URL 即视为云端, 强制锁死挂载盘 /data
#     (DATA_ROOT=/data/db, STORAGE_ROOT=/data/storage, SQLite DSN 锁死
#      sqlite:////data/db/data.db), 与 render.yaml 的 disks.mountPath 一致。
#     即使 AIVS_DATA_ROOT 等环境变量漏配, 也以 /data 为准, 杜绝部署后路径漂移。
#   • 本地桌面客户端: 落到 用户文档/AIVideoStudio/{data,storage}(Win/Mac 通用),
#     初始化时自动创建目录。
# 所有文件 I/O 一律走 settings.DATA_ROOT / settings.STORAGE_ROOT, 不出现硬编码。
# ============================================================================

if os.getenv("RENDER_EXTERNAL_URL"):
    # ---- 云端: 绝对路径锁死 /data(持久化磁盘挂载点) ----
    settings.DATA_ROOT = "/data/db"
    settings.STORAGE_ROOT = "/data/storage"
    settings.DATABASE_URL = "sqlite:////data/db/data.db"
    # 云端免费实例 CPU/内存极小:放宽最终成片 CRF(20→23)以加速编码、降低 OOM 风险,
    # 短视频平台对 23 画质完全可接受;本地桌面仍用 20 保高画质。
    settings.OUTPUT_CRF = 23
    # ★ 内存优化(V21): 云端帧率 30→24, 编码像素吞吐/内存 -20%, 短视频观感无差异;
    #   本地桌面保持 30fps。
    settings.OUTPUT_FPS = 24
    # ★★ 内存优化(V21)最关键一条: 云端强制跳过 rembg AI 抠图!
    #   rembg 首次调用会把 U2Net 模型(~176MB)+onnxruntime 加载进主进程,
    #   RSS 直接飙升 ~300MB 且【永不释放】——之后基线内存 ~450MB,
    #   任何 ffmpeg 一启动即打爆 512MB → 表现为"有概率 OOM"(取决于本次
    #   实例生命周期内是否有人上传过非透明图触发过抠图)。
    #   云端改走"白底转透明"纯 PIL 兜底(白底商品图效果可接受, 内存 ~10MB);
    #   如实例内存充足(付费实例)可设环境变量 AIVS_FORCE_MATTING=1 恢复 AI 抠图。
    if os.getenv("AIVS_FORCE_MATTING") != "1":
        settings.SKIP_MATTING = True
        print("☁️ [Config] 云端模式: 已禁用 rembg AI 抠图(省 ~300MB 常驻内存), "
              "改用白底转透明兜底; 付费实例可设 AIVS_FORCE_MATTING=1 恢复", flush=True)
    print(
        f"☁️ [Config] 云端模式(Render): 存储锁定 /data "
        f"(DATA={settings.DATA_ROOT}, STORAGE={settings.STORAGE_ROOT})",
        flush=True,
    )
else:
    # ---- 本地桌面客户端: 用户文档/AIVideoStudio ----
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
    print(
        f"🖥️ [Config] 本地模式: 存储于 {settings.DATA_ROOT}",
        flush=True,
    )
