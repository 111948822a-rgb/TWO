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
    sem = asyncio.Semaphore(project.config.concurrency)

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
        "[%s] 阶段④ 配音生成开始(语言=%s, 音色=%s)",
        project.project_id, project.input.language, voice,
    )

    async def _process_audio(scene: Scene) -> None:
        """单分镜配音生成:调 CosyVoice,返回 mp3 + 精确 duration。"""
        audio_gen = AudioGenerator()
        await audio_gen.generate_for_scene(scene, voice=voice)
        scene.status = SceneStatus.AUDIO_DONE
        logger.info("  [%s] 配音生成完成", scene.scene_id)

    await _run_scenes_concurrent(
        project, _process_audio, ProjectStatus.AUDIO_GEN, "音频生成"
    )
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
        output_path = await compositor.composite(project)
    finally:
        # 恢复原始配音开关(避免降级污染持久化状态)
        project.input.enable_voiceover = original_voiceover

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

    try:
        for i, stage in enumerate(flow):
            logger.info(
                "[Task %s] 🚀 开始阶段: %s (%d/%d)",
                task_id, stage.value, i + 1, total,
            )
            project.logs.append(StageLog(
                ts=datetime.utcnow().isoformat(),
                stage=stage.value,
                message=f"开始阶段：{STAGE_LABELS.get(stage, stage.value)}",
            ))
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
        project.touch()

    return project
