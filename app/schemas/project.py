"""
VideoProject 数据流转模型定义。

本模块定义贯穿整个 Pipeline 的核心数据契约:
- VideoProject: 项目顶层结构,包含输入、配置、分镜列表、输出
- Scene: 单个分镜,含旁白 / 生图 Prompt / 生视频 Prompt 及生成素材

所有模块(script_generator / image_generator / video_generator /
audio_generator / compositor)均以 VideoProject 为输入输出,
实现数据流转的统一。VideoProject 整体以 JSON 形式持久化到 DB 单列。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 状态枚举
# ---------------------------------------------------------------------------

class ProjectStatus(str, Enum):
    """项目级状态机。

    流转顺序:
        pending -> scripting -> img_gen -> vid_gen -> audio_gen
               -> compositing -> completed
    任何阶段失败 -> failed
    """

    PENDING = "pending"
    SCRIPTING = "scripting"
    IMG_GEN = "img_gen"
    VID_GEN = "vid_gen"
    AUDIO_GEN = "audio_gen"
    COMPOSITING = "compositing"
    COMPLETED = "completed"
    FAILED = "failed"
    # V9.0 Director Mode Pro: 候选池生成完成后暂停，等待用户选择
    AWAITING_SELECTION = "awaiting_selection"


class SceneStatus(str, Enum):
    """分镜级状态机,支持分镜级并发与独立重试。

    流转顺序:
        pending -> img_done -> vid_done -> audio_done -> synced
    任何阶段失败 -> failed
    """

    PENDING = "pending"
    IMG_DONE = "img_done"
    VID_DONE = "vid_done"
    AUDIO_DONE = "audio_done"
    SYNCED = "synced"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# 输入与配置
# ---------------------------------------------------------------------------

class RhythmStage(BaseModel):
    """节奏时间轴单个阶段(Pacing Engine 爆款节奏把控引擎)。

    由前端"黄金节奏模板"生成,作为最高优先级硬约束下发到 LLM 与 FFmpeg:
      - LLM 阶段:强制每个分镜的 stage_name / target_duration 与本表一致
      - FFmpeg 阶段:按 target_duration 精准裁剪 / 变速,确保成片严格卡点
    """

    stage_name: str = Field(
        "", description="阶段名称(黄金钩子/核心信息/实景演示/效果对比/CTA引导)"
    )
    start_time: float = Field(0.0, ge=0.0, description="阶段起始时间(秒)")
    end_time: float = Field(0.0, ge=0.0, description="阶段结束时间(秒)")
    duration: float = Field(0.0, ge=0.0, description="阶段时长(秒)")


class ProductInput(BaseModel):
    """产品输入信息,由用户在创建项目时提供。"""

    product_name: str = Field(..., description="产品名称")
    product_description: str = Field("", description="产品描述 / 核心卖点")
    # V21.0 产品灵魂注入:产品类型与核心外观特征(如"黑色机械键盘、RGB背光、银色铝合金鼠标")
    #   贯穿 LLM 脚本导演(动作指导/景别控制/主体一致性)与生图生视频 Prompt,
    #   是防止 AI 把键盘画成鼠标、近景细节崩坏的核心输入。
    product_category_and_features: str = Field(
        "", description="产品类型与核心特征(防形变/主体一致性锚点)"
    )
    selling_points: List[str] = Field(
        default_factory=list, description="卖点列表"
    )
    target_audience: str = Field("", description="目标受众")
    # 多图支持:用户可上传多张产品图(正面/侧面/细节/场景),LLM 为每个分镜分配 image_index
    # 向后兼容:若仅传单图,此字段保留单图 URL;image_urls 非空时优先使用
    white_image_url: str = Field(..., description="产品白底图 URL(主图,兼容旧逻辑)")
    image_urls: List[str] = Field(
        default_factory=list,
        description="多张产品图公网 URL 列表(供 LLM 分配到各分镜)",
    )
    duration_target_sec: int = Field(
        30, ge=5, le=120, description="目标视频时长(秒)"
    )
    style: str = Field(
        "生活化", description="视频风格:生活化 / 专业测评 / 戏剧化"
    )
    voice: str = Field("女声温柔", description="TTS 音色选择")
    # V4.0 市场聚焦:目标语言代码(en英语/th泰语/id印尼语)
    # narration 和 hook_text 用目标语言,image_prompt/video_prompt 强制英文
    language: str = Field("en", description="目标语言(en/th/id)")
    # V4.0 配音开关:关闭时跳过阶段④TTS,阶段⑤仅保留 BGM 循环,不烧录字幕
    enable_voiceover: bool = Field(True, description="是否生成 AI 配音(关闭则纯 BGM 无旁白无字幕)")
    # V6.0 BGM 情绪引擎扩充为 7 种:
    #   upbeat(动感快节奏) / premium(高级轻奢) / chill(轻松生活)
    #   cinematic(电影史诗) / viral(搞笑网感) / asmr(沉浸解压) / urgent(急促大促)
    vibe: str = Field("upbeat", description="视频氛围(upbeat/premium/chill/cinematic/viral/asmr/urgent)")
    # V6.0 视觉风格:影响生图/生视频 Prompt 的画面风格描述词
    #   photorealistic(真实摄影) / 3d_render(3D渲染) / anime(二次元) / cyberpunk(赛博朋克)
    visual_style: str = Field("photorealistic", description="视觉风格(photorealistic/3d_render/anime/cyberpunk)")
    # V6.1 去白边强度:rembg 抠图后对 alpha 通道进行腐蚀+羽化,消灭边缘 Halo effect
    #   off(关闭) / light(轻度1px) / medium(中度2px,默认) / heavy(重度3px+强羽化)
    defringe_strength: str = Field("medium", description="去白边强度(off/light/medium/heavy)")
    # V11.0 视频比例:支持多场景发布
    #   9:16(竖屏/TikTok 1080x1920) / 16:9(横屏/YouTube 1920x1080) / 1:1(方形/Feed 1080x1080)
    aspect_ratio: str = Field("9:16", description="视频比例(9:16/16:9/1:1)")
    # V14.0 产品材质:引导 LLM 生成匹配的物理光影交互(消灭贴图感)
    #   glass(玻璃/透明) / metal(金属/反光) / plastic(塑料/哑光)
    #   fabric(布料/服饰) / electronics(3C数码/发光) / other(其他)
    product_material: str = Field("other", description="产品材质(glass/metal/plastic/fabric/electronics/other)")
    # V15.0 拍同款:参考视频 URL(上传到 OSS 的公网 MP4 URL)
    reference_video_url: Optional[str] = Field(None, description="参考视频公网 URL(拍同款模式)")
    # V15.0 拍同款:是否为克隆模式(用 Qwen-VL 分析参考视频提取分镜,替代 LLM 生成脚本)
    clone_mode: bool = Field(False, description="是否为拍同款模式")
    # V18.0 Pacing Engine 爆款节奏把控:总时长 + 黄金节奏模板时间轴
    #   total_duration 与 duration_target_sec 语义一致,新增以对齐前端字段名;
    #   rhythm_rules 非空时启用节奏硬约束(LLM 分镜数/时长 + FFmpeg 精准卡点)。
    total_duration: int = Field(
        15, ge=5, le=120, description="Pacing Engine 目标总时长(秒:15/30/60)"
    )
    rhythm_rules: List[RhythmStage] = Field(
        default_factory=list,
        description="节奏时间轴规则(Pacing Engine 黄金节奏模板,为空则不启用硬约束)",
    )

    def get_image_url(self, index: int | None = None) -> str:
        """按索引取产品图 URL。image_urls 非空则按索引取,否则回退主图。"""
        if self.image_urls:
            if index is None or index < 0 or index >= len(self.image_urls):
                return self.image_urls[0]
            return self.image_urls[index]
        return self.white_image_url


class ProjectConfig(BaseModel):
    """Provider 与运行时配置。

    默认: LLM=DeepSeek、图片=通义万相(tongyi_wanxiang)、
    视频=HappyHorse 1.1(happyhorse)、TTS=CosyVoice。
    视频引擎自 V16.2 起唯一锁定 HappyHorse 1.1,不再使用通义万相视频。
    """

    llm_provider: str = "deepseek"
    image_provider: str = "tongyi_wanxiang"
    video_provider: str = "happyhorse"
    tts_provider: str = "cosyvoice"
    bgm_url: Optional[str] = Field(None, description="BGM 音频 URL,可选")
    concurrency: int = Field(
        3, ge=1, le=10, description="分镜级并发数"
    )
    # V9.0 Director Mode Pro: 每分镜生成候选数(1=普通模式, 2-3=候选池模式)
    candidates_per_scene: int = Field(
        1, ge=1, le=3, description="每分镜候选素材数(导演模式Pro)"
    )


# ---------------------------------------------------------------------------
# 分镜素材
# ---------------------------------------------------------------------------

class KeyframeImage(BaseModel):
    """关键帧图片资产。"""

    url: Optional[str] = None
    local_path: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class VideoClip(BaseModel):
    """分镜视频片段资产。"""

    url: Optional[str] = None
    local_path: Optional[str] = None
    duration: Optional[float] = None
    fps: Optional[int] = None
    # V14.1: 记录视频由哪个引擎生成(happyhorse-1.1 / wan2.2-i2v-flash)
    engine: Optional[str] = None


class AudioAsset(BaseModel):
    """分镜配音音频资产。"""

    url: Optional[str] = None
    local_path: Optional[str] = None
    duration: Optional[float] = None


class SceneAssets(BaseModel):
    """单个分镜的全部生成素材。"""

    keyframe_image: KeyframeImage = Field(default_factory=KeyframeImage)
    video_clip: VideoClip = Field(default_factory=VideoClip)
    audio: AudioAsset = Field(default_factory=AudioAsset)


# ---------------------------------------------------------------------------
# 分镜与项目
# ---------------------------------------------------------------------------

class Scene(BaseModel):
    """单个分镜定义。

    核心字段(对应避坑要点):
        narration:    旁白文案(用于 TTS 与字幕)
        image_prompt: 生图场景 Prompt(必须含场景环境 / 光线 / 构图,
                      避免只换背景)
        video_prompt: 生视频运镜 Prompt(必须含具体运镜词与动态元素,
                      避免 PPT 轮播)
        hook_text:    V4.0 黄金3秒钩子花字(仅第一个分镜,compositor 用
                      drawtext 在画面正中央叠加超大粗体白字黑边,2-3秒淡出)
    """

    scene_id: str = Field(..., description="分镜唯一 ID,如 scene_001")
    index: int = Field(..., ge=0, description="分镜序号,从 0 开始")
    narration: str = Field("", description="旁白文案")
    image_prompt: str = Field("", description="生图场景 Prompt")
    video_prompt: str = Field("", description="生视频运镜 Prompt")
    # V4.0 黄金3秒钩子花字:仅第一个分镜有值(痛点反问/夸张陈述),compositor 用 drawtext 叠加
    hook_text: Optional[str] = Field(None, description="钩子花字(仅第一个分镜,2-6词)")
    # 多图支持:LLM 为该分镜分配的产品图索引(对应 input.image_urls)
    # 为 None/越界 时回退到第 0 张
    image_index: int = Field(0, ge=0, description="该分镜使用的产品图索引")
    # V7.0 导演模式:分镜级视觉风格覆盖(为 None 时使用全局 project.input.visual_style)
    visual_style: Optional[str] = Field(None, description="分镜级视觉风格覆盖(photorealistic/3d_render/anime/cyberpunk)")
    # V18.0 Pacing Engine 爆款节奏把控:节奏阶段名 + 目标时长(硬约束)+ 实际素材时长
    #   stage_name/target_duration 由 rhythm_rules 强制注入 LLM 并回填;
    #   actual_video_duration 在 stage_video_gen 后记录,供 FFmpeg 精准卡点对齐。
    stage_name: str = Field("", description="节奏阶段名称(Pacing Engine,如 黄金钩子/CTA引导)")
    target_duration: float = Field(0.0, ge=0.0, description="节奏目标时长(秒,Pacing Engine 硬约束)")
    actual_video_duration: Optional[float] = Field(
        None, description="实际生成视频素材时长(秒,供 FFmpeg 对齐)"
    )
    status: SceneStatus = SceneStatus.PENDING
    assets: SceneAssets = Field(default_factory=SceneAssets)
    # V9.0 Director Mode Pro: 候选池(每分镜多张图/多个视频供用户选择)
    candidate_images: List[KeyframeImage] = Field(
        default_factory=list, description="候选图片列表(导演模式Pro)"
    )
    candidate_videos: List[VideoClip] = Field(
        default_factory=list, description="候选视频列表(导演模式Pro)"
    )
    error: Optional[str] = None


class FinalOutput(BaseModel):
    """最终合成产物。"""

    final_video_url: Optional[str] = None
    local_path: Optional[str] = None
    duration_sec: Optional[float] = None
    subtitle_url: Optional[str] = None
    # V14.1: 记录视频生成引擎(happyhorse-1.1 / wan2.2-i2v-flash)
    video_engine: Optional[str] = None


class StageLog(BaseModel):
    """V17.3: 单条执行日志(用于前端"查看执行日志"折叠面板)。"""

    ts: str = Field(..., description="ISO 时间戳")
    stage: str = Field(..., description="阶段标识(pending/scripting/img_gen/.../completed/failed)")
    message: str = Field("", description="可读的执行说明")


class VideoProject(BaseModel):
    """贯穿 Pipeline 的核心数据契约。

    每个模块只读写自己负责的字段,通过 status 推进状态机。
    VideoProject 整体以 JSON 形式持久化到 DB 单列(SQLite JSON1)。
    """

    project_id: str
    status: ProjectStatus = ProjectStatus.PENDING
    progress: float = Field(0.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # V17.3: 执行时间打点(供前端耗时 / ETA 估算)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    input: ProductInput
    config: ProjectConfig = Field(default_factory=ProjectConfig)
    scenes: List[Scene] = Field(default_factory=list)
    output: FinalOutput = Field(default_factory=FinalOutput)

    error: Optional[str] = None
    # 完整 Python 堆栈(防抓瞎:前端折叠展示,便于排查深层错误)
    technical_traceback: Optional[str] = None
    # V17.3: 执行日志流(各阶段开始/完成/失败记录,前端折叠展示)
    logs: List[StageLog] = Field(default_factory=list)
    # V19.0: 开机自动续跑计数 —— 实例重启/部署会杀死在跑的后台任务,
    #   开机时自动续跑被打断的合成/视频生成任务;此字段记录已自动续跑次数,
    #   达上限(auto_resume_interrupted 的 max_retries)则判定为真失败、不再续跑,
    #   防止"每次部署都无限循环续跑同一失败任务"。随 scenes_data 持久化。
    auto_retry_count: int = Field(
        0, ge=0, description="被实例重启打断后的自动续跑次数(达上限则标记失败)"
    )
    # V20.0: Logo 品牌动画配置 —— 在成片前后或全程叠加 Logo 动画,
    #   默认启用片头+片尾淡入淡出动画,增强品牌露出。
    logo_enabled: bool = Field(
        True, description="是否在视频中加入 Logo 动画/水印"
    )
    logo_position: str = Field(
        "head_tail", description="Logo 位置: none/head/tail/head_tail/watermark"
    )
    logo_duration: float = Field(
        2.0, ge=0.5, le=5.0, description="片头/片尾 Logo 动画时长(秒)"
    )
    logo_animation: str = Field(
        "fade", description="Logo 动画效果: fade/zoom/slide/none"
    )
    logo_local_path: Optional[str] = Field(
        None, description="用户自定义 Logo 图片本地路径(为空则使用默认 Logo)"
    )

    def touch(self) -> None:
        """更新 updated_at 时间戳,在任何状态变更后调用。"""
        self.updated_at = datetime.utcnow()
