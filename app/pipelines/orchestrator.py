"""
Pipeline 总控状态机。

职责:
    1. 推进 VideoProject 状态机:
       pending -> scripting -> img_gen -> vid_gen
              -> audio_gen -> compositing -> completed
    2. 图片/视频阶段串行执行(规避阿里云并发限流)
    3. 任何阶段失败 -> project.status = failed,记录 error
    4. 支持 until 参数:只跑到指定阶段(用于分阶段测试)

当前接入情况:
    - 阶段① 文案:已接入 DeepSeek(services/script_generator.py)
    - 阶段② 图片:已接入通义万相 background-generation(services/image_generator.py)
    - 阶段③ 视频:已接入通义万相 wan2.2-i2v-flash(services/video_generator.py)
    - 阶段④ 音频:已接入 CosyVoice TTS(services/audio_generator.py)
    - 阶段⑤ 合成:已接入 FFmpeg(services/compositor.py)
      音画对齐 + xfade 转场 + BGM ducking + 字幕烧录 + 1080p H.264 输出

避坑要点(已体现在接口设计中):
    - 图片阶段:调用 background-generation,主体保持 + 场景融合,避免换背景
    - 视频阶段:VideoGenerator 强制依赖 scene.video_prompt(运镜指令),
      拒绝空 prompt 调用以避免默认推拉摇移导致 PPT 轮播
    - 音频阶段:AudioGenerator 返回精确 duration,供合成阶段音画对齐
    - 合成阶段:Compositor 用 xfade 转场 + tpad 补帧保证音画同步,
      sidechaincompress 实现 BGM ducking,subtitles 烧录字幕
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import traceback
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

from app.schemas.project import (
    ProjectStatus,
    Scene,
    SceneStatus,
    StageLog,
    VideoProject,
)
from app.services.audio_generator import AudioGenerator
from app.services.compositor import Compositor
from app.services.image_generator import ImageGenerator
from app.services.script_generator import ScriptGenerator
from app.services.video_generator import VideoGenerator
from app.core.constants import STAGE_LABELS

logger = logging.getLogger(__name__)

# V8.0: SQLite 持久化同步
try:
    from app.core.database import sync_project_from_model
    _DB_AVAILABLE = True
except Exception as _db_exc:  # noqa: BLE001
    logger.warning("[Orchestrator] SQLite 不可用,跳过持久化: %s", _db_exc)
    _DB_AVAILABLE = False


def _sync_db(project: VideoProject) -> None:
    """同步 VideoProject 到 SQLite(V8.0)。"""
    if _DB_AVAILABLE:
        sync_project_from_model(project)


# V19.2: 全局合成并发信号量 —— 任一时刻仅允许 1 个 ffmpeg 合成在跑(跨所有任务,
# 含用户触发的正常任务 + 开机自动续跑任务)。512MB 实例上 2 路合成并发必 OOM,
# 此信号量把内存峰值压到单路,是根治"合成 OOM"的关键。
# vid_gen/img_gen 等阶段多为等外部 API(低内存),不在此约束内,可并发。
_composite_semaphore = asyncio.Semaphore(1)


# ---------------------------------------------------------------------------
# V21: 内存观测 + 主动归还(512MB 小实例保命三件套之二)
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    """读取当前进程常驻内存 RSS(MB)。Linux 读 /proc,其他平台返回 -1。零依赖。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB -> MB
    except Exception:  # noqa: BLE001
        pass
    return -1.0


def _release_memory(tag: str) -> None:
    """强制垃圾回收 + glibc malloc_trim,把峰值后的空闲堆真正归还 OS。

    Python 的 gc.collect 只回收对象,glibc 仍可能扣着空闲页不还(RSS 不降);
    malloc_trim(0) 强制归还。在合成等内存峰值阶段结束后调用,
    使实例回到低水位,给下一个任务留足余量。
    """
    import ctypes
    import gc
    before = _rss_mb()
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass  # 非 Linux/glibc(本地 Windows 开发)静默跳过
    after = _rss_mb()
    if before > 0:
        logger.info(
            "[Memory] %s 后主动归还内存: RSS %.0fMB -> %.0fMB", tag, before, after
        )


# ---------------------------------------------------------------------------
# V-XRAY: 全链路产物"生死追踪"
#   用户核心质疑: HappyHorse / TTS 返回云端 URL,代码是否真的把文件下载到
#   本地 /data/storage? 这里在**每个阶段完成后**暴力打印每个分镜所有中间
#   产物的绝对路径 + 存在性 + 精确字节数,让"中间产物断层"在日志里无所遁形。
# ---------------------------------------------------------------------------

# 生死阈值:视频本地落盘后必须 >10KB(排除空壳/截断);音频 mp3 短片段也远超 1KB
_XRAY_MIN_VIDEO_BYTES = 10240
_XRAY_MIN_AUDIO_BYTES = 1024


def _xray_size(path: "str | None") -> int:
    """返回文件字节数;路径为空或不存在时返回 0(绝不抛异常)。"""
    if not path:
        return 0
    try:
        return os.path.getsize(path) if os.path.exists(path) else 0
    except OSError:
        return 0


def _trace_artifacts(project: "VideoProject", tag: str) -> None:
    """全链路 X 光级产物生死追踪(纯观测,不中断流程)。

    在每个阶段完成后打印每个分镜的 关键帧图 / 视频 / 音频 的绝对路径 +
    存在性 + 精确字节数。格式与用户要求一致,便于线上 grep 定位断层。

    注意:视频的**本地落盘**发生在合成阶段 compositor._prepare_assets,
    故 stage_video_gen 阶段视频 local_path 通常尚为 None(仅云端 URL 已就绪),
    真正的本地落盘校验在 compositor 内完成。
    """
    logger.warning("=" * 72)
    logger.warning(
        "[全链路自检] >>> %s | 任务=%s | 分镜数=%d | 配音=%s",
        tag, project.project_id, len(project.scenes),
        "开" if project.input.enable_voiceover else "关",
    )
    for i, scene in enumerate(project.scenes, 1):
        a = scene.assets
        # 关键帧图(通常为云端 URL,用于图生视频,不直接进 FFmpeg)
        kf = a.keyframe_image
        kf_path = kf.local_path if kf else None
        # 视频
        vc = a.video_clip
        v_url = vc.url if vc else None
        v_local = vc.local_path if vc else None
        # 音频
        au = a.audio
        a_local = au.local_path if au else None
        logger.warning(
            "[全链路自检] 分镜%d | 图:路径=%s,存在=%s,大小=%dB | "
            "视频:URL=%s,本地=%s,存在=%s,大小=%dB | "
            "音频:路径=%s,存在=%s,大小=%dB",
            i,
            kf_path or "(无)", bool(kf_path and os.path.exists(kf_path)), _xray_size(kf_path),
            v_url or "(无)", v_local or "(暂未落盘/合成阶段下载)",
            bool(v_local and os.path.exists(v_local)), _xray_size(v_local),
            a_local or "(无)", bool(a_local and os.path.exists(a_local)), _xray_size(a_local),
        )
    logger.warning("=" * 72)


# ---------------------------------------------------------------------------
# 阶段 ① 文案分镜(已接入 DeepSeek)
# ---------------------------------------------------------------------------

async def stage_scripting(project: VideoProject) -> None:
    """阶段 ①:生成文案与分镜。

    V15.0: 支持两种模式:
      - 普通模式:调用 DeepSeek LLM 生成文案分镜
      - 拍同款模式(clone_mode=True):调用 Qwen-VL-Max 分析参考视频提取分镜
    """
    project.status = ProjectStatus.SCRIPTING
    logger.info("[%s] 阶段① 文案分镜生成开始", project.project_id)

    if project.input.clone_mode and project.input.reference_video_url:
        # V15.0 拍同款:用 Qwen-VL 分析参考视频
        logger.info(
            "[%s] 阶段① 拍同款模式:调用 Qwen-VL-Max 分析参考视频",
            project.project_id,
        )
        from app.services.video_analyzer import VideoAnalyzer
        analyzer = VideoAnalyzer()
        await analyzer.analyze(project)
    else:
        # 普通模式:调用 DeepSeek 生成文案
        generator = ScriptGenerator()
        await generator.generate(project)

    logger.info(
        "[%s] 阶段① 完成,生成 %d 个分镜",
        project.project_id,
        len(project.scenes),
    )


# ---------------------------------------------------------------------------
# 阶段 ② 场景图片(已接入通义万相,串行执行规避限流)
# ---------------------------------------------------------------------------

async def stage_image_gen(project: VideoProject) -> None:
    """阶段 ②:生成关键帧图片(主体保持 + 场景融合)。

    通义万相 background-generation 限流"同时处理中任务数=1",
    故此处串行执行(不使用并发框架),避免任务被拒。
    后续若切换不限流的 Provider,可改回 _run_scenes_concurrent。

    V7.0 导演模式:若分镜有独立的 visual_style 覆盖,将对应风格关键词
    追加到 image_prompt 末尾,使生图模型使用该分镜的指定风格。

    V9.0 Director Mode Pro:当 candidates_per_scene > 1 时,为每个分镜
    生成 N 张候选图片存入 scene.candidate_images,供用户选择。
    """
    project.status = ProjectStatus.IMG_GEN
    n_candidates = project.config.candidates_per_scene
    logger.info(
        "[%s] 阶段② 关键帧图片生成开始(串行,候选数=%d)",
        project.project_id, n_candidates,
    )

    from app.utils.prompt_templates import VISUAL_STYLE_PROMPTS
    from app.schemas.project import KeyframeImage

    image_gen = ImageGenerator()

    for scene in project.scenes:
        # V7.0: 分镜级视觉风格覆盖
        if scene.visual_style and scene.visual_style != project.input.visual_style:
            style_desc = VISUAL_STYLE_PROMPTS.get(
                scene.visual_style, VISUAL_STYLE_PROMPTS["photorealistic"]
            )
            if style_desc not in scene.image_prompt:
                scene.image_prompt = f"{scene.image_prompt}, {style_desc}"
                logger.info(
                    "  [%s] 分镜级风格覆盖: %s -> 追加风格关键词",
                    scene.scene_id, scene.visual_style,
                )

        subject_url = project.input.get_image_url(scene.image_index)

        if n_candidates > 1:
            # V9.0: 候选池模式 - 生成 N 张候选
            scene.candidate_images = []
            for ci in range(n_candidates):
                try:
                    await image_gen.generate_for_scene(scene, subject_url)
                    # generate_for_scene sets scene.assets.keyframe_image; copy to candidates
                    if scene.assets.keyframe_image and scene.assets.keyframe_image.url:
                        scene.candidate_images.append(KeyframeImage(
                            url=scene.assets.keyframe_image.url,
                            local_path=scene.assets.keyframe_image.local_path,
                        ))
                        logger.info(
                            "  [%s] 候选图片 %d/%d 生成完成",
                            scene.scene_id, ci + 1, n_candidates,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "  [%s] 候选图片 %d 生成失败: %s",
                        scene.scene_id, ci + 1, exc,
                    )
                    # Continue to try remaining candidates
            if not scene.candidate_images:
                scene.status = SceneStatus.FAILED
                scene.error = f"所有 {n_candidates} 张候选图片均生成失败"
            else:
                # Set primary asset to first candidate
                scene.assets.keyframe_image = scene.candidate_images[0]
                scene.status = SceneStatus.IMG_DONE
        else:
            # 普通模式: 生成 1 张
            try:
                await image_gen.generate_for_scene(scene, subject_url)
                scene.status = SceneStatus.IMG_DONE
            except Exception as exc:  # noqa: BLE001
                scene.status = SceneStatus.FAILED
                scene.error = str(exc)
                logger.error(
                    "  [%s] 图片生成失败(img_idx=%d): %s",
                    scene.scene_id, scene.image_index, exc,
                )

    failed = [s for s in project.scenes if s.status == SceneStatus.FAILED]
    succeeded = [s for s in project.scenes if s.status == SceneStatus.IMG_DONE]
    if failed:
        details = " | ".join(
            f"[{s.scene_id}] {s.error or '未知错误'}" for s in failed
        )
        logger.warning(
            "[%s] 阶段② %d 个分镜失败(跳过),%d 个成功: %s",
            project.project_id, len(failed), len(succeeded), details,
        )
    logger.info(
        "[%s] 阶段② 完成(成功 %d/%d)",
        project.project_id, len(succeeded), len(project.scenes),
    )
    _trace_artifacts(project, "阶段② 生图完成")


# ---------------------------------------------------------------------------
# 阶段 ③ 视频生成(唯一引擎: HappyHorse 1.1,串行执行规避限流)
# ---------------------------------------------------------------------------

async def stage_video_gen(project: VideoProject) -> None:
    """阶段 ③:生成视频片段(图生视频 + 强制运镜 prompt)。

    唯一视频引擎 HappyHorse 1.1。VideoGenerator 已移除所有 fallback,
    HappyHorse 失败即抛出,本阶段捕获后标记该分镜 failed 并记录详细错误。
    VideoGenerator 仍会强制校验 video_prompt 非空,拒绝默认推拉摇移。

    V9.0 Director Mode Pro:当 candidates_per_scene > 1 时,为每个分镜
    生成 N 个候选视频存入 scene.candidate_videos,供用户选择。
    每个候选视频使用对应的候选图片作为输入。
    """
    project.status = ProjectStatus.VID_GEN
    n_candidates = project.config.candidates_per_scene
    logger.info(
        "[%s] 阶段③ 视频片段生成开始(串行,候选数=%d,每片约 1-5 分钟)",
        project.project_id, n_candidates,
    )

    from app.schemas.project import VideoClip, KeyframeImage

    video_gen = VideoGenerator()

    for scene in project.scenes:
        # V19.1: 逐分镜进度落库,前端可见"正在生成分镜视频",消除 vid_gen "卡住"错觉
        project.logs.append(StageLog(
            ts=datetime.utcnow().isoformat(),
            stage=ProjectStatus.VID_GEN.value,
            message=f"正在生成分镜 {scene.scene_id} 视频(约1-5分钟)…",
        ))
        _sync_db(project)
        if n_candidates > 1 and scene.candidate_images:
            # V9.0: 候选池模式 - 为每张候选图片生成视频
            scene.candidate_videos = []
            original_kf = scene.assets.keyframe_image
            for ci, cand_img in enumerate(scene.candidate_images):
                # 临时设置候选图片为关键帧
                scene.assets.keyframe_image = cand_img
                try:
                    await video_gen.generate_for_scene(scene)
                    if scene.assets.video_clip and scene.assets.video_clip.url:
                        scene.candidate_videos.append(VideoClip(
                            url=scene.assets.video_clip.url,
                            local_path=scene.assets.video_clip.local_path,
                            duration=scene.assets.video_clip.duration,
                            engine=scene.assets.video_clip.engine,
                        ))
                        logger.info(
                            "  [%s] 候选视频 %d/%d 生成完成",
                            scene.scene_id, ci + 1, n_candidates,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "  [%s] 候选视频 %d 生成失败: %s",
                        scene.scene_id, ci + 1, exc,
                    )
            # 恢复第一候选为主素材
            scene.assets.keyframe_image = scene.candidate_images[0] if scene.candidate_images else original_kf
            if scene.candidate_videos:
                scene.assets.video_clip = scene.candidate_videos[0]
                scene.status = SceneStatus.VID_DONE
            else:
                scene.status = SceneStatus.FAILED
                scene.error = f"所有 {n_candidates} 个候选视频均生成失败"
        else:
            # 普通模式: 生成 1 个视频
            try:
                await video_gen.generate_for_scene(scene)
                scene.status = SceneStatus.VID_DONE
            except Exception as exc:  # noqa: BLE001
                scene.status = SceneStatus.FAILED
                scene.error = str(exc)
                logger.error(
                    "  [%s] 视频生成失败: %s", scene.scene_id, exc
                )

    failed = [s for s in project.scenes if s.status == SceneStatus.FAILED]
    succeeded = [s for s in project.scenes if s.status == SceneStatus.VID_DONE]

    # V18.0 Pacing Engine:记录每个分镜"实际素材时长",供 FFmpeg 精准卡点对齐。
    #   HappyHorse 1.1 通常固定输出约 5s 片段;若 provider 未回填 duration,
    #   则以固定 5.0s 兜底(与引擎默认时长一致)。
    HAPPYHORSE_DEFAULT_DURATION = 5.0
    for scene in project.scenes:
        vc = scene.assets.video_clip
        actual = vc.duration if (vc and vc.duration and vc.duration > 0) else HAPPYHORSE_DEFAULT_DURATION
        scene.actual_video_duration = round(float(actual), 2)
        if scene.target_duration and scene.target_duration > 0:
            logger.info(
                "[%s] [Pacing Engine] 分镜 %s(%s) 素材时长记录:实际=%.1fs, 节奏目标=%.1fs%s",
                project.project_id, scene.scene_id,
                scene.stage_name or "-", scene.actual_video_duration,
                scene.target_duration,
                "" if scene.status == SceneStatus.VID_DONE else " (视频生成失败,以引擎默认时长兜底,合成阶段将降级对齐)",
            )
        else:
            logger.info(
                "[%s] 分镜 %s 素材时长记录:实际=%.1fs",
                project.project_id, scene.scene_id, scene.actual_video_duration,
            )

    if failed:
        details = " | ".join(
            f"[{s.scene_id}] {s.error or '未知错误'}" for s in failed
        )
        logger.warning(
            "[%s] 阶段③ %d 个分镜失败(跳过),%d 个成功: %s",
            project.project_id, len(failed), len(succeeded), details,
        )
    # V17.1: 全部失败 -> 提前抛出详细死因, 让 project.error 携带真实原因
    # (图片URL无法访问 / 下载失败 等), 而非后续阶段报笼统的'无有效片段'。
    if not succeeded:
        raise RuntimeError(
            f"全部 {len(project.scenes)} 个分镜视频均生成失败: {details}"
        )
    # V-XRAY: 视频生成后硬校验 —— 标记为成功(VID_DONE)的分镜,其云端视频
    # URL 必须真实存在(本地落盘在合成阶段完成,这里先确保『云端产物』没断)。
    for s in succeeded:
        vc = s.assets.video_clip
        if not vc or not vc.url:
            logger.error(
                "[%s][全链路自检] ❌ 分镜 %s 视频标记为成功,但 video_clip.url 为空(云端产物断层)!",
                project.project_id, s.scene_id,
            )
    _trace_artifacts(project, "阶段③ 视频完成")
    logger.info(
        "[%s] 阶段③ 完成(成功 %d/%d)",
        project.project_id, len(succeeded), len(project.scenes),
    )

    # V14.1: 记录视频引擎到 output(取第一个成功分镜的 engine)
    for s in succeeded:
        if s.assets.video_clip and s.assets.video_clip.engine:
            project.output.video_engine = s.assets.video_clip.engine
            logger.info(
                "[%s] 视频引擎: %s",
                project.project_id, s.assets.video_clip.engine,
            )
            break


# ---------------------------------------------------------------------------
# 阶段 ④ 音频生成(已接入 CosyVoice,并发执行)
# ---------------------------------------------------------------------------

async def _run_scenes_concurrent(
    project: VideoProject,
    handler: Callable[[Scene], Awaitable[None]],
    new_status: ProjectStatus,
    stage_name: str,
) -> None:
    """分镜级并发执行通用框架。

    使用 asyncio.Semaphore 限制并发度,避免对第三方 API 造成压力。
    任一分镜失败则标记该分镜 failed,并最终抛出整体异常。
    """
    project.status = new_status
    # TTS-HARDEN-2: 防御性并发度 —— 即便配置异常 concurrency<=0 也绝不造成
    # Semaphore(0) 永久死锁(每个 async with sem 永远拿不到锁 → 阶段卡死)
    concurrency = max(1, int(project.config.concurrency or 1))
    logger.info(
        "[%s] %s 并发度: %d (原始配置=%s)",
        project.project_id, stage_name, concurrency, project.config.concurrency,
    )
    sem = asyncio.Semaphore(concurrency)

    async def _wrapped(scene: Scene) -> None:
        async with sem:
            try:
                await handler(scene)
            except Exception as exc:  # noqa: BLE001
                scene.status = SceneStatus.FAILED
                scene.error = str(exc)
                logger.error(
                    "  [%s] %s 失败: %s", scene.scene_id, stage_name, exc
                )

    await asyncio.gather(*[_wrapped(s) for s in project.scenes])

    failed = [s for s in project.scenes if s.status == SceneStatus.FAILED]
    if failed:
        details = " | ".join(
            f"[{s.scene_id}] {s.error or '未知错误'}" for s in failed
        )
        logger.warning(
            "[%s] %s 阶段 %d 个分镜失败(跳过): %s",
            project.project_id, stage_name, len(failed), details,
        )


async def stage_audio_gen(project: VideoProject) -> None:
    """阶段 ④:分镜级并发生成配音音频(返回精确时长)。

    V3.0:按 project.input.language 解析 CosyVoice 音色,
    传入 AudioGenerator 实现多语言出海 TTS。
    V4.0:当 project.input.enable_voiceover == False 时跳过整个阶段
    (run_pipeline 已动态排除 AUDIO_GEN,此处为防御性早退)。
    """
    if not project.input.enable_voiceover:
        logger.info(
            "[%s] 阶段④ 配音已关闭(用户关闭配音开关),跳过 TTS",
            project.project_id,
        )
        return

    from app.providers.tts.cosyvoice import resolve_voice

    voice = resolve_voice(project.input.language)
    logger.info(
        "[TTS] 阶段④ 配音生成开始(任务=%s, 语言=%s, 音色=%s)",
        project.project_id, project.input.language, voice,
    )

    # V-XRAY-2: 收集每个分镜真实的 TTS 死因(欠费/配额/音色/地域/超时等),
    # 供聚合阶段把根因打印出来 —— 而非只报笼统的"本地文件缺失"。
    audio_errors: dict[str, str] = {}

    async def _process_audio(scene: Scene) -> None:
        """单分镜配音生成:调 CosyVoice,返回 mp3 + 精确 duration。

        V-TTS-HARDEN: 配音阶段不再因单个分镜 TTS 失败而中断整条流水线。
        捕获 TTS 异常后将该分镜 audio.local_path 置 None 并继续,后续合成阶段
        (stage_compositing) 会自动过滤无音频分镜或降级为纯 BGM 无旁白模式,
        确保视频依然能产出,不再"卡在 AI 配音阶段无法继续"。

        TTS-HARDEN-2: 外层 asyncio.wait_for(80s) 兜底 —— 即便 provider 内部
        的 75s 硬超时墙因任何原因失效(极端事件循环异常),单分镜也绝不会无限
        死等,最多 80s 必降级为无配音并放行后续分镜/阶段。
        """
        t0 = time.monotonic()
        try:
            audio_gen = AudioGenerator()
            logger.info("[TTS] 单分镜 %s 配音开始(音色=%s)", scene.scene_id, voice)
            await asyncio.wait_for(
                audio_gen.generate_for_scene(scene, voice=voice),
                timeout=80.0,
            )
            elapsed = time.monotonic() - t0
            scene.status = SceneStatus.AUDIO_DONE
            logger.info(
                "[TTS] 单分镜 %s 配音完成(耗时 %.1fs)", scene.scene_id, elapsed
            )
            # V18.0 Pacing Engine:预检配音时长与节奏目标的偏差,提示后续 FFmpeg 变速对齐
            #   真正的精准 atempo(加速/补静音)在 compositor 阶段按 target_duration 执行
            ad = scene.assets.audio.duration if (scene.assets and scene.assets.audio) else None
            if scene.target_duration and scene.target_duration > 0 and ad and ad > 0:
                if ad > scene.target_duration + 0.05:
                    ratio = ad / scene.target_duration
                    logger.info(
                        "[TTS] [Pacing Engine] 分镜 %s(%s) 配音偏长:配音=%.1fs > 目标=%.1fs,"
                        "FFmpeg 将以 atempo≈%.2f 加速对齐",
                        scene.scene_id, scene.stage_name or "-", ad,
                        scene.target_duration, min(ratio, 2.0),
                    )
                elif ad < scene.target_duration - 0.05:
                    logger.info(
                        "[TTS] [Pacing Engine] 分镜 %s(%s) 配音偏短:配音=%.1fs < 目标=%.1fs,"
                        "FFmpeg 将补静音对齐",
                        scene.scene_id, scene.stage_name or "-", ad, scene.target_duration,
                    )
        except (asyncio.TimeoutError, TimeoutError):
            # Python 3.10 中 asyncio.TimeoutError 与内置 TimeoutError 是不同类,
            # 必须同时捕获两者,确保超时一定能走降级而非冒泡中断流水线
            elapsed = time.monotonic() - t0
            logger.warning(
                "[TTS] ❌ 单分镜 %s 配音超时(80s, 已耗时 %.1fs),降级为无配音(视频仍合成)",
                scene.scene_id, elapsed,
            )
            audio_errors[scene.scene_id] = f"配音超时(>{80}s)"
            if scene.assets and scene.assets.audio:
                scene.assets.audio.local_path = None
                scene.assets.audio.duration = None
            scene.status = SceneStatus.AUDIO_DONE
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            logger.warning(
                "[TTS] 单分镜 %s 配音失败(耗时 %.1fs),降级为无配音(视频仍合成): %s",
                scene.scene_id, elapsed, exc,
            )
            audio_errors[scene.scene_id] = f"{type(exc).__name__}: {exc}"
            # 清除可能残留的音频信息,避免合成阶段误用半成品
            if scene.assets and scene.assets.audio:
                scene.assets.audio.local_path = None
                scene.assets.audio.duration = None
            # 标记为配音阶段已处理(仅缺配音),不把整个分镜标 FAILED,
            # 否则合成阶段会按"无效分镜"将其排除,丢失该分镜的视频
            scene.status = SceneStatus.AUDIO_DONE

    await _run_scenes_concurrent(
        project, _process_audio, ProjectStatus.AUDIO_GEN, "音频生成"
    )
    # V-XRAY-2: 音频生成后自检 —— 配音开启时,统计本地 mp3 真实落盘情况,
    # 并把每个失败分镜的**真实 TTS 死因**(欠费/配额/音色/地域/超时)打印出来。
    #
    # ⚠️ 重要设计原则(修正上一版倒退):配音是**可降级的可选项**,不是硬性产物。
    #   系统既有设计为「配音失败 → 降级为无配音 → 视频照常合成出片」。上一版
    #   在『全部配音失败』时直接 raise 致命错误,反而破坏了这个保命降级,导致
    #   连一个无配音的视频都拿不到。此处**绝不 raise**,只做响亮告警 + 暴露根因,
    #   把降级为纯 BGM/无旁白的决定交给合成阶段(stage_compositing 已实现)。
    if project.input.enable_voiceover:
        done = [s for s in project.scenes if s.status == SceneStatus.AUDIO_DONE]
        bad = [
            s for s in done
            if not s.assets.audio or not s.assets.audio.local_path
            or _xray_size(s.assets.audio.local_path) < _XRAY_MIN_AUDIO_BYTES
        ]
        if bad:
            reason_str = " | ".join(
                f"[{s.scene_id}] {audio_errors.get(s.scene_id, '本地文件缺失/过小(<%dB)' % _XRAY_MIN_AUDIO_BYTES)}"
                for s in bad
            )
            if len(bad) == len(done):
                logger.error(
                    "[%s][全链路自检] ⚠️ 全部 %d 个分镜配音失败,自动降级为『无配音/纯 BGM』"
                    "继续合成(视频仍可产出)。各分镜 TTS 死因如下,请据此排查 "
                    "DASHSCOPE_API_KEY 余额/配额/地域/音色: %s",
                    project.project_id, len(bad), reason_str,
                )
            else:
                logger.warning(
                    "[%s][全链路自检] ⚠️ %d/%d 个分镜配音失败(将降级为无配音),死因: %s",
                    project.project_id, len(bad), len(done), reason_str,
                )
        else:
            logger.info(
                "[%s][全链路自检] ✅ 全部 %d 个分镜配音本地 mp3 落盘校验通过",
                project.project_id, len(done),
            )
    _trace_artifacts(project, "阶段④ 音频完成")
    logger.info("[%s] 阶段④ 完成", project.project_id)


async def stage_compositing(project: VideoProject) -> None:
    """阶段 ⑤:FFmpeg 合成(音画对齐 + xfade 转场 + BGM + 字幕 + Hook花字)。

    V10.1 P0 核心修复 —— 绝不静默跳过合成:
      - 唯一硬性门槛:至少 1 个分镜有 video_clip.url(有视频就能合成)
      - 音频全部失败时:自动降级为纯 BGM 无旁白模式,仍然合成
      - 部分分镜无音频时:过滤到有音频的分镜(配音模式下)
      - 0 个有效视频片段 → raise(真正的不可恢复错误)
    """
    project.status = ProjectStatus.COMPOSITING
    logger.info("[%s] 阶段⑤ 后期合成开始", project.project_id)

    # P0 硬性门槛:至少 1 个有效视频片段
    valid_scenes = [
        s for s in project.scenes
        if s.assets.video_clip and s.assets.video_clip.url
    ]
    if not valid_scenes:
        total = len(project.scenes)
        raise RuntimeError(
            f"无有效视频片段(全部 {total} 个分镜视频均不可用),"
            f"合成终止 — 请检查图片/视频生成日志"
        )

    # 临时保存原始配音开关(合成后恢复,避免污染持久化状态)
    original_voiceover = project.input.enable_voiceover

    if original_voiceover:
        scenes_with_audio = [
            s for s in valid_scenes
            if s.assets.audio and s.assets.audio.local_path
        ]
        if not scenes_with_audio:
            # 音频全部失败 → 降级为纯 BGM 模式,仍然合成
            logger.warning(
                "[%s] 阶段⑤ 全部 %d 个分镜音频缺失,降级为纯 BGM 无旁白模式合成",
                project.project_id, len(valid_scenes),
            )
            project.input.enable_voiceover = False
        elif len(scenes_with_audio) < len(valid_scenes):
            # 部分音频失败 → 仅保留有音频的分镜
            skipped_ids = [
                s.scene_id for s in valid_scenes if s not in scenes_with_audio
            ]
            logger.warning(
                "[%s] 阶段⑤ 过滤 %d 个无音频分镜(跳过: %s),仅合成 %d 个",
                project.project_id, len(skipped_ids),
                ", ".join(skipped_ids), len(scenes_with_audio),
            )
            valid_scenes = scenes_with_audio

    if len(valid_scenes) < len(project.scenes):
        skipped_ids = [
            s.scene_id for s in project.scenes if s not in valid_scenes
        ]
        logger.warning(
            "[%s] 阶段⑤ 仅合成 %d/%d 个有效分镜(跳过: %s)",
            project.project_id, len(valid_scenes), len(project.scenes),
            ", ".join(skipped_ids),
        )
        project.scenes = valid_scenes
    else:
        logger.info(
            "[%s] 阶段⑤ 全部 %d 个分镜有效,开始合成",
            project.project_id, len(valid_scenes),
        )

    try:
        compositor = Compositor()
        # 全局串行:512MB 实例上同时只跑 1 个合成,杜绝并发 ffmpeg 导致 OOM
        async with _composite_semaphore:
            logger.info("[Memory] 合成开始前 RSS=%.0fMB", _rss_mb())
            output_path = await compositor.composite(project)
    except Exception as _comp_exc:  # noqa: BLE001
        # [用户要求] 状态机兜底保活:合成抛出异常时,立刻把具体错误(含 FFmpeg
        # stderr)写入 error 字段,并由外层 run_pipeline 统一置为 FAILED +
        # completed_at,绝不让任务永远停在 COMPOSITING 状态。
        project.error = f"合成失败：{type(_comp_exc).__name__}: {_comp_exc}"
        project.completed_at = datetime.utcnow()
        logger.error(
            "[%s] 阶段⑤ 合成抛出异常(将置为 FAILED): %s",
            project.project_id, project.error,
        )
        raise
    finally:
        # 恢复原始配音开关(避免降级污染持久化状态)
        project.input.enable_voiceover = original_voiceover
        # V21: 合成是全流程内存峰值,结束后(无论成败)立即归还空闲堆给 OS,
        # 让实例回到低水位,给下一个任务/自动续跑留足余量。
        _release_memory("合成")

    logger.info(
        "[%s] 阶段⑤ 完成,最终视频: %s (时长 %.1fs)",
        project.project_id,
        output_path,
        project.output.duration_sec or 0.0,
    )


# ---------------------------------------------------------------------------
# 状态机总控
# ---------------------------------------------------------------------------

STAGE_HANDLERS: dict[ProjectStatus, Callable[[VideoProject], Awaitable[None]]] = {
    ProjectStatus.SCRIPTING: stage_scripting,
    ProjectStatus.IMG_GEN: stage_image_gen,
    ProjectStatus.VID_GEN: stage_video_gen,
    ProjectStatus.AUDIO_GEN: stage_audio_gen,
    ProjectStatus.COMPOSITING: stage_compositing,
}

FLOW: List[ProjectStatus] = [
    ProjectStatus.SCRIPTING,
    ProjectStatus.IMG_GEN,
    ProjectStatus.VID_GEN,
    ProjectStatus.AUDIO_GEN,
    ProjectStatus.COMPOSITING,
]


async def run_pipeline(
    project: VideoProject,
    until: Optional[ProjectStatus] = None,
    from_stage: Optional[ProjectStatus] = None,
) -> VideoProject:
    """Pipeline 总入口:按顺序执行各阶段,推进状态机。

    V10.1 P0 重构 —— 普通模式与导演模式彻底隔离:

    普通模式 (candidates_per_scene == 1, until == None):
        执行流 [SCRIPTING, IMG_GEN, VID_GEN, AUDIO_GEN, COMPOSITING]
        中间绝对不允许有任何 await 等待用户的逻辑,全自动一气呵成跑完!

    导演模式 (candidates_per_scene > 1, until == VID_GEN):
        执行流 [SCRIPTING, IMG_GEN, VID_GEN] → 设置 AWAITING_SELECTION → return
        等待前端调用 select-candidates 后,再从 AUDIO_GEN/COMPOSITING 继续。

    任何阶段抛异常 -> project.status = FAILED,记录 error + traceback。
    跑完全部 -> project.status = COMPLETED,progress = 1.0。
    """
    task_id = project.project_id
    director_mode = project.config.candidates_per_scene > 1

    # V4.0:关闭配音时跳过 AUDIO_GEN
    flow = [
        s for s in FLOW
        if not (s == ProjectStatus.AUDIO_GEN and not project.input.enable_voiceover)
    ]

    # V7.0:从指定阶段开始(导演模式 continue-generation)
    if from_stage and from_stage in flow:
        from_idx = flow.index(from_stage)
        flow = flow[from_idx:]
        logger.info(
            "[Task %s] 从阶段 %s 开始执行(跳过前 %d 个阶段)",
            task_id, from_stage.value, from_idx,
        )

    total = len(flow)
    skipped = [s.value for s in FLOW if s not in flow]
    if skipped:
        logger.info(
            "[Task %s] 已跳过阶段: %s", task_id, ", ".join(skipped)
        )

    logger.info(
        "[Task %s] 🎬 Pipeline 启动 (模式=%s, 阶段数=%d, until=%s)",
        task_id, "导演" if director_mode else "普通", total,
        until.value if until else "无(跑完)",
    )

    # V17.3: 执行时间打点 —— 仅在首次启动记录开始时间(导演模式续跑不覆盖)
    if not project.started_at:
        project.started_at = datetime.utcnow()
    project.logs.append(StageLog(
        ts=datetime.utcnow().isoformat(),
        stage="pending",
        message="🚀 任务开始执行",
    ))

    # V19.1: 流水线级心跳 —— 整条 pipeline 运行期间每 30s 刷新 updated_at,
    # 确保 vid_gen / img_gen 等长耗时阶段不会被看门狗误判卡死(阈值见
    # _STALE_BY_STATUS),也让前端看到任务持续活跃,消除"卡在某阶段不动"的错觉。
    # 合成阶段另有更频繁的 compositor 心跳,二者并存无害(均为幂等写库)。
    async def _pipeline_heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                _sync_db(project)
            except Exception:  # noqa: BLE001
                pass
            # V21: 心跳同时打印 RSS 内存水位,Render Logs 里直接可见离 512MB 多远,
            # OOM 前必有征兆(RSS 持续 >450MB),便于事后定位是哪个阶段吃的内存。
            rss = _rss_mb()
            if rss > 0:
                logger.info(
                    "[Memory] 心跳 RSS=%.0fMB (stage=%s)", rss, project.status
                )

    hb_task = asyncio.create_task(_pipeline_heartbeat())
    try:
        for i, stage in enumerate(flow):
            # ================= 阶段流转关键修复 (V-STATE-FLOW) =================
            # 此前 _sync_db 仅在阶段"完成后"调用(见循环末尾),导致 COMPOSITING
            # 阶段在 FFmpeg 跑完(可能数分钟)之前,数据库状态一直停留在 AUDIO_GEN,
            # 前端因此"卡在配音阶段不动"。现在进入每个阶段的第一时间就把
            # status + "开始阶段"日志落库,前端立即能看到"正在合成视频"。
            project.status = stage
            if stage == ProjectStatus.COMPOSITING:
                logger.info(
                    "[Orchestrator] TTS 已完成，状态切换为 COMPOSITING，"
                    "准备调用 FFmpeg..."
                )
            logger.info(
                "[Task %s] 🚀 开始阶段: %s (%d/%d)",
                task_id, stage.value, i + 1, total,
            )
            project.logs.append(StageLog(
                ts=datetime.utcnow().isoformat(),
                stage=stage.value,
                message=f"开始阶段：{STAGE_LABELS.get(stage, stage.value)}",
            ))
            # ← 阶段一开始即同步 DB,前端实时看到当前阶段(彻底修复"卡在配音"显示)
            _sync_db(project)
            handler = STAGE_HANDLERS[stage]
            await handler(project)
            project.progress = round((i + 1) / total, 2)
            project.touch()
            project.logs.append(StageLog(
                ts=datetime.utcnow().isoformat(),
                stage=stage.value,
                message=f"完成阶段：{STAGE_LABELS.get(stage, stage.value)}",
            ))
            _sync_db(project)  # V8.0: 每阶段完成同步 SQLite
            logger.info(
                "[Task %s] ✅ 完成阶段: %s (progress=%.0f%%)",
                task_id, stage.value, project.progress * 100,
            )

            # 导演模式暂停点:仅在 until 显式指定时触发,普通模式 until=None 永不暂停
            if until and stage == until:
                logger.info(
                    "[Task %s] 🎬 到达指定阶段 %s,停止推进",
                    task_id, until.value,
                )
                # V9.0 Director Mode Pro: 候选池模式下设 AWAITING_SELECTION
                if (
                    until == ProjectStatus.VID_GEN
                    and director_mode
                ):
                    project.status = ProjectStatus.AWAITING_SELECTION
                    logger.info(
                        "[Task %s] 🎬 导演模式:设置 AWAITING_SELECTION,等待用户选择候选素材",
                        task_id,
                    )
                _sync_db(project)
                return project

        # 普通模式:全部阶段跑完 → COMPLETED
        project.status = ProjectStatus.COMPLETED
        project.progress = 1.0
        project.completed_at = datetime.utcnow()
        project.logs.append(StageLog(
            ts=project.completed_at.isoformat(),
            stage="completed",
            message="🎉 任务全部完成",
        ))
        _sync_db(project)
        logger.info("[Task %s] 🎉 Pipeline 全部完成!", task_id)
    except Exception as exc:  # noqa: BLE001
        project.status = ProjectStatus.FAILED
        project.completed_at = datetime.utcnow()
        project.error = f"{type(exc).__name__}: {exc}"
        project.technical_traceback = traceback.format_exc()
        project.logs.append(StageLog(
            ts=project.completed_at.isoformat(),
            stage="failed",
            message=f"❌ 任务失败：{type(exc).__name__}: {exc}",
        ))
        _sync_db(project)
        logger.exception(
            "[Task %s] ❌ Pipeline 失败: %s", task_id, exc
        )
    finally:
        hb_task.cancel()
        project.touch()

    return project
