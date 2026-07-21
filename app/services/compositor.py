"""阶段 ⑤ 后期剪辑与合成(基于 ffmpeg-python)。

本模块是整个 Pipeline 的"灵魂",负责把前四阶段生成的素材
(视频片段 + 旁白音频)合成为一条专业级带货短视频。

合成流程(分三个子阶段,避免单次 filter_complex 过于复杂且便于调试):

    A. 素材准备
       - 异步下载各分镜视频 URL 到 storage/temp/
       - 准备 BGM(使用配置的 BGM_PATH,否则生成低音量粉红噪声占位)
       - 旁白音频已在阶段④下载到 storage/audios/,直接复用

    B. 音画对齐(每个分镜并行处理)
       核心:确保每个分镜的视频时长 == 旁白音频时长,否则后续转场与字幕无法对齐。
       策略(根据视频时长 Dv 与音频时长 Da 的比值):
         - Da/Dv >= 0.8 :setpts 慢放/压缩(变化 <= 25%,肉眼无感)
         - Da/Dv <  0.8 :视频远长于音频,trim 截断(音频主导,丢尾可接受)
       统一 scale 到 1920x1080 + fps 30 + 高质量 CRF=18 中间编码。
       输出 storage/temp/{scene_id}_aligned.mp4(含对齐后的音视频)。

    C. 最终合成(单次 ffmpeg 调用,ffmpeg-python 构建 filter graph)
       - 视频轨:xfade 链式转场(fade,0.4s)+ tpad 末尾补帧(补回转场缩短的时长)
                + subtitles 滤镜烧录 SRT 字幕(白字黑边,底部居中)
       - 音频轨:voiceover(各分镜旁白 concat)作为主音频
                BGM 经 sidechaincompress 实现 Ducking(旁白说话时压低 BGM)
                amix 混合 voiceover + ducked_bgm
       - 输出 H.264 / AAC / 1080p MP4 到 storage/outputs/

音画同步原理(关键):
    xfade 转场会使视频轨总时长缩短 (N-1)*T(每次转场重叠 T 秒)。
    而音频 concat 总时长 = sum(Di),不缩短。
    为对齐:视频 xfade 后用 tpad 在末尾克隆最后一帧 (N-1)*T 秒,
    使视频轨总时长 == 音频轨总时长 == sum(Di),实现精确同步。
    转场瞬间(T 秒窗口):画面 crossfade,声音是上一段旁白尾声,符合专业剪辑习惯。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

import ffmpeg
import httpx

from app.core.config import settings
from app.schemas.project import Scene, SceneStatus, VideoProject

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ffmpeg 可执行文件定位(终极兜底:见 app/utils/ffmpeg_utils.py)
# ---------------------------------------------------------------------------

# 模块加载时即解析 ffmpeg 路径并注入环境变量,
# 消灭 "No ffmpeg exe could be found" 报错。
# ensure_ffmpeg_exe() 依次尝试:自定义路径 → imageio-ffmpeg 自带二进制
#   → bin/ 本地缓存 → 镜像自动下载,任一成功即写入环境变量。
try:
    from app.utils.ffmpeg_utils import ensure_ffmpeg_exe

    _ffmpeg_exe = ensure_ffmpeg_exe()
    logger.info("[Compositor] FFmpeg 路径已注入: %s", _ffmpeg_exe)
except Exception as _exc:  # noqa: BLE001
    logger.warning(
        "[Compositor] 启动时注入 FFmpeg 路径失败,将在调用时再解析: %s", _exc
    )


def _get_ffmpeg_exe() -> str:
    """获取 ffmpeg 可执行文件路径(走 ensure_ffmpeg_exe 终极兜底)。"""
    from app.utils.ffmpeg_utils import ensure_ffmpeg_exe
    return ensure_ffmpeg_exe()


# V11.0 视频比例 -> 目标分辨率映射
_ASPECT_RESOLUTIONS = {
    "9:16": (1080, 1920),   # 竖屏/TikTok
    "16:9": (1920, 1080),   # 横屏/YouTube
    "1:1": (1080, 1080),    # 方形/Feed
}


def _get_aspect_resolution(aspect_ratio: str) -> tuple[int, int]:
    """根据视频比例返回目标 (width, height),默认 9:16。"""
    return _ASPECT_RESOLUTIONS.get(aspect_ratio, (1080, 1920))


# ---------------------------------------------------------------------------
# V4.0 drawtext 辅助:粗体字体解析 + 文本转义
# ---------------------------------------------------------------------------

def _find_noto_cjk_font() -> "str | None":
    """在 Linux 容器中查找 Noto CJK 字体(由 apt 包 fonts-noto-cjk 提供)。

    用于支持中文/日文/韩文字幕与花字,避免 FFmpeg drawtext 渲染中文变成方块。
    使用 glob 兼容不同 Debian 版本下的安装路径(opentype/truetype)。
    Windows / 未安装 Noto 的环境返回 None,调用方回退到原有候选字体。
    """
    import glob as _glob
    patterns = [
        "/usr/share/fonts/**/NotoSansCJK*.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.otf",
        "/usr/share/fonts/**/NotoSerifCJK*.ttc",
        "/usr/share/fonts/**/NotoSerifCJK*.otf",
    ]
    for pat in patterns:
        hits = sorted(_glob.glob(pat, recursive=True))
        if hits:
            return hits[0]  # 任意一个 Noto CJK 字体即可覆盖中/日/韩文
    return None


# Docker(apt fonts-noto-cjk)下自动探测到的中文字体,优先级高于 DejaVu(Latin only)
_NOTO_CJK_FONT = _find_noto_cjk_font()
if _NOTO_CJK_FONT:
    logger.info("[Compositor] 检测到 Noto CJK 中文字体: %s", _NOTO_CJK_FONT)


# 粗体字体候选(优先支持泰语/印尼语/英语的 Tahoma Bold,回退 Arial Bold)
# 容器内有 Noto CJK 时插入到 DejaVu 之前,确保中文花字不再变方块
_BOLD_FONT_CANDIDATES = [
    "C:/Windows/Fonts/tahomabd.ttf",   # Tahoma Bold(支持泰语)
    "C:/Windows/Fonts/arialbd.ttf",    # Arial Bold
    "C:/Windows/Fonts/segoeuib.ttf",   # Segoe UI Bold
    "C:/Windows/Fonts/arial.ttf",      # Arial Regular(兜底)
]
if _NOTO_CJK_FONT:
    _BOLD_FONT_CANDIDATES.append(_NOTO_CJK_FONT)  # Linux 中文(优先于 DejaVu)
_BOLD_FONT_CANDIDATES.append(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # Linux 兜底
)
_hook_fontfile: str | None = None


def _resolve_hook_fontfile() -> str | None:
    """解析 drawtext 用的粗体字体文件路径(模块级缓存)。"""
    global _hook_fontfile
    if _hook_fontfile is not None:
        return _hook_fontfile or None
    import os
    for candidate in _BOLD_FONT_CANDIDATES:
        if os.path.exists(candidate):
            _hook_fontfile = candidate
            logger.info("[Compositor] Hook 花字字体: %s", candidate)
            return candidate
    _hook_fontfile = ""  # 标记已查找但未找到
    logger.warning("[Compositor] 未找到粗体字体文件,drawtext 将用默认字体")
    return None


# V12.0 字幕字体(常规,非粗体)——支持泰语/印尼语/英语/中文
# 容器内有 Noto CJK 时插入到 DejaVu 之前,确保中文字幕不再变方块
_SUBTITLE_FONT_CANDIDATES = [
    "C:/Windows/Fonts/tahoma.ttf",     # Tahoma Regular(支持泰语)
    "C:/Windows/Fonts/segoeui.ttf",    # Segoe UI Regular
    "C:/Windows/Fonts/arial.ttf",      # Arial Regular
]
if _NOTO_CJK_FONT:
    _SUBTITLE_FONT_CANDIDATES.append(_NOTO_CJK_FONT)  # Linux 中文(优先于 DejaVu)
_SUBTITLE_FONT_CANDIDATES.append(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"  # Linux 兜底(Latin only)
)
_subtitle_fontfile: str | None = None


def _resolve_subtitle_fontfile() -> str | None:
    """解析 drawtext 字幕用的常规字体文件路径(模块级缓存)。"""
    global _subtitle_fontfile
    if _subtitle_fontfile is not None:
        return _subtitle_fontfile or None
    import os
    for candidate in _SUBTITLE_FONT_CANDIDATES:
        if os.path.exists(candidate):
            _subtitle_fontfile = candidate
            logger.info("[Compositor] 字幕字体: %s", candidate)
            return candidate
    _subtitle_fontfile = ""
    logger.warning("[Compositor] 未找到字幕字体文件,drawtext 将用默认字体")
    return None


def _escape_drawtext_text(text: str) -> str:
    """转义 drawtext text 参数中的特殊字符(冒号/反斜杠/单引号)。"""
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
    )


# ---------------------------------------------------------------------------
# 阶段 A:素材下载与 BGM 准备
# ---------------------------------------------------------------------------

async def _download_file(url: str, output_path: str, timeout: float = 120.0) -> str:
    """异步下载文件到本地。"""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        Path(output_path).write_bytes(resp.content)
    return output_path


def _verify_file(path: str, min_bytes: int = 1) -> bool:
    """校验文件真实存在于本地磁盘且非空(防"无米之炊")。"""
    try:
        p = Path(path)
        return p.exists() and p.is_file() and p.stat().st_size >= min_bytes
    except Exception:
        return False


def _assert_inputs_nonempty(
    project_id: str, labeled_paths: List[tuple[str, str]]
) -> None:
    """FFmpeg 调用前的强制输入自检:任一文件不存在或 0 字节立刻抛异常。

    V-FFMPEG-GUARD: 绝不把空文件/缺失文件交给 FFmpeg 处理导致卡死或产出损坏。
    labeled_paths: [(label, path), ...],label 如 "视频[scene_1]" / "音频[scene_1]"。
    """
    logger.info(
        "[Compositor] 🔍 FFmpeg 前输入自检开始,共 %d 个文件...",
        len(labeled_paths),
    )
    for label, path in labeled_paths:
        if not path or not os.path.exists(path):
            raise RuntimeError(
                f"[Compositor] ❌ 输入文件无效: {label} 文件不存在: {path}"
            )
        size = os.path.getsize(path)
        if size == 0:
            raise RuntimeError(
                f"[Compositor] ❌ 输入文件无效: {label} 大小为 0: {path}"
            )
        logger.info(
            "[Compositor] ✓ 输入自检通过 %s: %s (%d bytes)", label, path, size
        )
    logger.info("[Compositor] ✅ 全部 %d 个输入文件自检通过", len(labeled_paths))


# ---------------------------------------------------------------------------
# 统一 FFmpeg 执行器(终极防卡死封装)
# ---------------------------------------------------------------------------

def _run_ffmpeg_cmd(
    cmd_args: List[str], level_name: str, timeout: float = 300
) -> "subprocess.CompletedProcess":
    """统一执行任意 FFmpeg 命令,彻底根除"静默卡死"。

    强制做到四件事:
      1. -y 覆盖符:若命令中未显式含 -y,自动插入,防止 FFmpeg 等待
         用户确认覆盖而永远卡死。
      2. stdin=DEVNULL:FFmpeg 默认会打开 stdin 监听 'q' 退出键,若 stdin
         连着未关闭的管道(常见于 subprocess 继承父进程 fd),它会永久阻塞
         等待输入 —— 这是云端"静默卡死"的头号元凶。置 DEVNULL 彻底禁用。
      3. timeout:超时强制 kill 子进程并抛异常,杜绝任何无界等待。
      4. 全景日志 + 完整 stderr:执行前打印完整命令,失败/超时打印完整 stderr。

    Returns:
        执行成功的 CompletedProcess(returncode==0)。
    Raises:
        RuntimeError:超时或退出码非 0(携带完整命令与 stderr 末段)。
    """
    args = list(cmd_args)
    # 强制 -y(ffmpeg-python 的 overwrite_output=True 已加则重复无害)
    if args and args[0].endswith("ffmpeg"):
        if "-y" not in args:
            args.insert(1, "-y")
    cmd_str = " ".join(args)
    logger.info("[Compositor][%s] FFmpeg 完整命令:\n%s", level_name, cmd_str)

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
        logger.error(
            "[Compositor][%s] ❌ FFmpeg 执行超时(%ds)已被强制终止!\n"
            "完整命令:\n%s\n==== stderr 末段 ====\n%s",
            level_name, int(timeout), cmd_str, stderr[-2000:],
        )
        raise RuntimeError(
            f"[{level_name}] FFmpeg 执行超时({int(timeout)}s)被强制 kill"
        ) from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        logger.error(
            "[Compositor][%s] ❌ FFmpeg 执行失败(退出码=%d)!\n"
            "完整命令:\n%s\n==== 完整 stderr ====\n%s",
            level_name, proc.returncode, cmd_str, stderr[-2500:],
        )
        raise RuntimeError(
            f"[{level_name}] FFmpeg 合成失败(退出码={proc.returncode}): {stderr[-1500:]}"
        )
    return proc


def _verify_product(output_path: str, level_name: str) -> None:
    """校验 FFmpeg 产物真实存在且非空(防损坏/空文件被当作成功)。"""
    p = Path(output_path)
    if not p.exists():
        raise RuntimeError(
            f"[{level_name}] FFmpeg 退出但未生成文件: {output_path}"
        )
    if p.stat().st_size < 1024:
        raise RuntimeError(
            f"[{level_name}] FFmpeg 生成文件过小(疑似空): "
            f"{output_path} size={p.stat().st_size}"
        )
    logger.info(
        "[Compositor][%s] 产物校验通过: %s (%d bytes)",
        level_name, output_path, p.stat().st_size,
    )


async def _prepare_assets(
    project: VideoProject, storage_root: str
) -> tuple[List[str], List[str], str]:
    """下载视频片段,返回 (视频本地路径列表, 音频本地路径列表, BGM 路径)。

    旁白音频已在阶段④下载到 storage/audios/,此处直接复用 local_path。
    V4.0:当 enable_voiceover=False 时,audio_paths 返回空列表(无旁白音频)。
    """
    temp_dir = Path(storage_root) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 异步并发下载所有视频片段
    video_paths: List[str] = []
    download_tasks = []
    for scene in project.scenes:
        clip = scene.assets.video_clip
        if not clip.url:
            raise RuntimeError(f"分镜 {scene.scene_id} 无视频 URL,无法合成")
        dst = str(temp_dir / f"{scene.scene_id}_src.mp4")
        video_paths.append(dst)
        download_tasks.append(_download_file(clip.url, dst))

    logger.info("[%s] 下载 %d 个视频片段...", project.project_id, len(download_tasks))
    downloaded = await asyncio.gather(*download_tasks)
    # 强制校验:每个视频本地文件必须真实存在且非空(防"无米之炊")
    for scene, path in zip(project.scenes, downloaded):
        if not _verify_file(path, min_bytes=1024):
            logger.warning(
                "[%s] 分镜 %s 首次下载疑似空文件,重试一次: %s",
                project.project_id, scene.scene_id, path,
            )
            await _download_file(scene.assets.video_clip.url, path)
        if not _verify_file(path, min_bytes=1024):
            raise RuntimeError(
                f"分镜 {scene.scene_id} 视频下载失败或文件为空: {path}"
            )
    logger.info("[%s] 视频片段下载完成并校验通过", project.project_id)

    # 旁白音频路径(已在阶段④落盘);V4.0 配音关闭时跳过
    audio_paths: List[str] = []
    if project.input.enable_voiceover:
        for scene in project.scenes:
            audio = scene.assets.audio
            if not audio.local_path or not _verify_file(audio.local_path, min_bytes=128):
                raise RuntimeError(
                    f"分镜 {scene.scene_id} 旁白音频文件不存在或为空: {audio.local_path}"
                )
            audio_paths.append(audio.local_path)
    else:
        logger.info("[%s] 配音已关闭,跳过旁白音频收集", project.project_id)

    # BGM 准备(V4.0:按 vibe 选择)
    # 关键防御: BGM 任何环节失败都不得中断最终合成,降级为无 BGM。
    try:
        bgm_path = await _prepare_bgm(project.input.vibe, str(temp_dir / "bgm.mp3"))
    except Exception as bgm_exc:  # noqa: BLE001
        logger.warning(
            "[%s] BGM 准备失败,降级为无 BGM 合成(不中断): %s",
            project.project_id, bgm_exc,
        )
        bgm_path = None

    return video_paths, audio_paths, bgm_path


# V6.0 BGM 情绪引擎扩充为 7 种:vibe → 占位生成参数(真实 MP3 文件存在时优先复用)
# 真实文件路径:storage/assets/bgm/{vibe}.mp3
# type=noise: 用 anoisesrc 生成噪声; type=sine: 用 sine 生成正弦波
_VIBE_AUDIO_PARAMS: dict[str, dict] = {
    "upbeat":    {"type": "noise", "color": "pink",   "amplitude": 0.10},  # 动感快节奏:明亮粉噪
    "premium":   {"type": "noise", "color": "brown",  "amplitude": 0.06},  # 高级轻奢:低沉棕噪
    "chill":     {"type": "noise", "color": "pink",   "amplitude": 0.08},  # 轻松生活:柔和粉噪
    "cinematic": {"type": "sine",  "freq": 55,        "amplitude": 0.15},  # 电影史诗:低频正弦波(低沉鼓点感)
    "viral":     {"type": "noise", "color": "white",  "amplitude": 0.12},  # 搞笑网感:滑稽白噪
    "asmr":      {"type": "noise", "color": "white",  "amplitude": 0.04},  # 沉浸解压:柔和白噪音
    "urgent":    {"type": "sine",  "freq": 220,       "amplitude": 0.10},  # 急促大促:中频正弦波(倒计时感)
}


async def _prepare_bgm(vibe: str, target_path: str) -> str:
    """准备 BGM 文件(V4.0 情绪引擎)。

    选择优先级:
      1. storage/assets/bgm/{vibe}.mp3(用户放置的真实 BGM 文件)
      2. settings.BGM_PATH 配置的全局 BGM(向后兼容)
      3. ffmpeg lavfi 生成对应 vibe 的占位噪声音频

    Args:
        vibe: 视频氛围(upbeat/premium/chill)
        target_path: 占位生成时的输出路径
    """
    vibe = (vibe or "upbeat").lower()

    # 1. vibe 专属 BGM 文件(用户可在 storage/assets/bgm/ 放置真实 MP3)
    vibe_path = Path(settings.STORAGE_ROOT) / "assets" / "bgm" / f"{vibe}.mp3"
    if vibe_path.exists():
        logger.info("[Compositor] 使用 vibe=%s 专属 BGM: %s", vibe, vibe_path)
        return str(vibe_path)

    # 2. 全局配置 BGM(向后兼容)
    if settings.BGM_PATH and Path(settings.BGM_PATH).exists():
        logger.info(
            "[Compositor] 未找到 vibe=%s 专属 BGM,回退全局配置: %s",
            vibe, settings.BGM_PATH,
        )
        return settings.BGM_PATH

    # 3. 复用已生成的占位 BGM
    if Path(target_path).exists():
        logger.info("[Compositor] 复用已生成的占位 BGM: %s", target_path)
        return target_path

    # 4. 生成 vibe 对应的占位音频(noise 或 sine)
    params = _VIBE_AUDIO_PARAMS.get(vibe, _VIBE_AUDIO_PARAMS["upbeat"])
    ffmpeg_exe = _get_ffmpeg_exe()
    if params["type"] == "sine":
        lavfi_input = f"sine=frequency={params['freq']}:duration=30:sample_rate=44100"
        logger.info(
            "[Compositor] 生成占位 BGM(vibe=%s, 正弦波 %dHz 30s)",
            vibe, params["freq"],
        )
    else:
        lavfi_input = f"anoisesrc=color={params['color']}:duration=30:amplitude={params['amplitude']}"
        logger.info(
            "[Compositor] 生成占位 BGM(vibe=%s, %s噪声 30s)",
            vibe, params["color"],
        )
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "lavfi",
        "-i", lavfi_input,
        "-ac", "2",
        "-b:a", "128k",
        target_path,
    ]
    _run_ffmpeg_cmd(cmd, "BGM占位生成", timeout=60)
    return target_path


# ---------------------------------------------------------------------------
# 阶段 B:音画对齐(并行)
# ---------------------------------------------------------------------------

def _align_scene_sync(
    scene: Scene,
    video_path: str,
    audio_path: str,
    output_path: str,
    ffmpeg_exe: str,
    has_audio: bool = True,
    aspect_ratio: str = "9:16",
) -> str:
    """单分镜音画对齐(同步阻塞,供 asyncio.to_thread 调用)。

    输出对齐后的 MP4(含音视频)。
    V4.0:has_audio=False 时(配音关闭),视频按自然时长播放,
    附加静音音轨(anullsrc)供 xfade 链使用。
    V11.0:aspect_ratio 动态适配 9:16/16:9/1:1 分辨率。
    """
    dv = scene.assets.video_clip.duration or 0.0
    if dv <= 0:
        raise RuntimeError(f"分镜 {scene.scene_id} 视频时长异常: {dv}s")

    v_in = ffmpeg.input(video_path)

    # V11.0 动态比例适配(模糊背景填充,杜绝拉伸变形):
    W, H = _get_aspect_resolution(aspect_ratio)
    src = v_in.video
    bg = (
        src
        .filter("scale", W, H, force_original_aspect_ratio="increase")
        .filter("crop", W, H)
        .filter("boxblur", 40, 1)            # 强模糊,营造高级虚化背景
        .filter("eq", brightness=-0.18, saturation=0.75)  # 轻微压暗,衬托前景
    )
    fg = src.filter("scale", W, H, force_original_aspect_ratio="decrease")
    v = ffmpeg.filter([bg, fg], "overlay", x="(W-w)/2", y="(H-h)/2")
    v = v.filter("setsar", 1)

    if has_audio:
        da = scene.assets.audio.duration or 0.0
        if da <= 0:
            raise RuntimeError(
                f"分镜 {scene.scene_id} 音频时长异常: {da}s"
            )
        a_in = ffmpeg.input(audio_path)
        # 音画对齐:根据 Dv/Da 比值选择策略
        ratio = da / dv
        if abs(ratio - 1.0) <= 0.02:
            v = v.filter("setpts", "PTS-STARTPTS")
            logger.debug("[%s] 时长匹配(Dv=%.3f, Da=%.3f)", scene.scene_id, dv, da)
        elif ratio >= 0.8:
            v = v.filter("setpts", f"PTS*{ratio}")
            v = v.filter("trim", duration=da).filter("setpts", "PTS-STARTPTS")
            logger.info(
                "[%s] setpts 对齐(Dv=%.3f -> Da=%.3f, ratio=%.3f)",
                scene.scene_id, dv, da, ratio,
            )
        else:
            v = v.filter("trim", duration=da).filter("setpts", "PTS-STARTPTS")
            logger.info(
                "[%s] trim 截断(Dv=%.3f -> Da=%.3f, 丢弃 %.3fs)",
                scene.scene_id, dv, da, dv - da,
            )
        v = v.filter("fps", fps=settings.OUTPUT_FPS)
        a = a_in.audio.filter("atrim", duration=da).filter("asetpts", "PTS-STARTPTS")
        target_duration = da
    else:
        # V4.0 无配音模式:视频按自然时长,附加静音音轨
        v = v.filter("setpts", "PTS-STARTPTS").filter("fps", fps=settings.OUTPUT_FPS)
        # anullsrc 生成与视频等长的静音音轨(xfade 链需要音频流存在)
        a = ffmpeg.input(
            f"anullsrc=channel_layout=stereo:sample_rate=44100",
            f="lavfi", t=str(round(dv, 3)),
        ).audio
        target_duration = dv
        logger.info("[%s] 无配音模式,视频自然时长 %.3fs + 静音轨", scene.scene_id, dv)

    # 高质量中间编码(CRF=18 视觉无损,避免二次编码损失)
    out = ffmpeg.output(
        v, a, output_path,
        vcodec="libx264", crf=18, pix_fmt="yuv420p",
        acodec="aac", ab="192k",
        r=settings.OUTPUT_FPS,
        **{"movflags": "+faststart"},
    )
    # === V-FFMPEG-GUARD: 编译命令 + 统一执行器(-y + stdin=DEVNULL + timeout=300) ===
    cmd_args = ffmpeg.compile(out, cmd=ffmpeg_exe, overwrite_output=True)
    # 统一执行器会打印完整命令并强制 stdin=DEVNULL,彻底杜绝交互卡死
    _run_ffmpeg_cmd(cmd_args, f"对齐[{scene.scene_id}]", timeout=300)

    # 产物校验:对齐输出必须真实存在且非空
    _verify_product(output_path, f"对齐[{scene.scene_id}]")
    logger.info(
        "[%s] 对齐完成 -> %s (时长 %.3fs)", scene.scene_id, output_path, target_duration
    )
    return output_path


async def _align_all_scenes(
    project: VideoProject,
    video_paths: List[str],
    audio_paths: List[str],
    storage_root: str,
) -> List[str]:
    """并行对所有分镜执行音画对齐,返回对齐后的 MP4 路径列表(按分镜顺序)。

    V4.0:audio_paths 为空(配音关闭)时,每分镜走无音频对齐分支。
    """
    ffmpeg_exe = _get_ffmpeg_exe()
    temp_dir = Path(storage_root) / "temp"
    has_audio = bool(audio_paths) and project.input.enable_voiceover
    aspect_ratio = project.input.aspect_ratio

    async def _one(scene: Scene, vp: str, ap: str) -> str:
        out = str(temp_dir / f"{scene.scene_id}_aligned.mp4")
        await asyncio.to_thread(
            _align_scene_sync, scene, vp, ap, out, ffmpeg_exe, has_audio, aspect_ratio
        )
        return out

    # 无配音时 audio_paths 为空,用 "" 占位
    ap_iter = iter(audio_paths) if has_audio else iter([""] * len(video_paths))
    tasks = [
        _one(s, vp, next(ap_iter))
        for s, vp in zip(project.scenes, video_paths)
    ]
    aligned = await asyncio.gather(*tasks)
    return list(aligned)


# ---------------------------------------------------------------------------
# 旁白拼接(concat demuxer,无重编码) + SRT 字幕生成
# ---------------------------------------------------------------------------

def _concat_voiceover(
    audio_paths: List[str], output_path: str, storage_root: str
) -> str:
    """用 ffmpeg concat demuxer 拼接所有分镜旁白为单条 voiceover.mp3。

    concat demuxer 无重编码,速度快且无损。要求各音频参数一致
    (CosyVoice 输出均为同参数 mp3,满足)。
    """
    # 生成 filelist(用绝对路径 + 正斜杠,规避 concat demuxer 相对路径解析歧义)
    list_path = Path(storage_root) / "temp" / "voiceover_list.txt"
    lines = []
    for ap in audio_paths:
        # concat demuxer 的 file 行:单引号包裹,反斜杠转正斜杠,单引号转义
        abs_path = str(Path(ap).resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{abs_path}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")

    ffmpeg_exe = _get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        output_path,
    ]
    logger.info("[Compositor] voiceover 拼接命令: %s", " ".join(cmd))
    _run_ffmpeg_cmd(cmd, "voiceover拼接", timeout=120)
    logger.info("[Compositor] voiceover 拼接完成: %s", output_path)
    return output_path


def _format_srt_time(t: float) -> str:
    """秒数转 SRT 时间格式 HH:MM:SS,mmm。"""
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:  # 四舍五入进位
        ms = 0
        t += 1
    s = int(t) % 60
    m = int(t // 60) % 60
    h = int(t // 3600)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_subtitle_segments(
    project: VideoProject,
) -> List[tuple[float, float, str]]:
    """V12.0 构建字幕片段列表:按句切分长旁白,返回 (start, end, text) 列表。

    每条字幕不超过 ~40 字符,按字数比例分配显示时间。
    供 drawtext 滤镜链精确控制字幕显示时间与位置(废弃不可控的 SRT subtitles 滤镜)。
    """
    import re
    segments: List[tuple[float, float, str]] = []
    cursor = 0.0
    for scene in project.scenes:
        da = scene.assets.audio.duration or 0.0
        text = scene.narration.strip()
        if not text:
            cursor += da
            continue
        # 按句号/问号/感叹号/逗号/分号切分(保留分隔符)
        parts = re.split(r'(?<=[.!?;，。！？；])\s*', text)
        parts = [s.strip() for s in parts if s.strip()]
        # 合并过短片段(<8 字符),拆分过长片段(>40 字符硬切)
        merged: List[str] = []
        for seg in parts:
            if merged and len(merged[-1]) < 8:
                merged[-1] = merged[-1] + seg
            elif len(seg) > 40:
                for i in range(0, len(seg), 40):
                    merged.append(seg[i:i + 40])
            else:
                merged.append(seg)
        if not merged:
            merged = [text[:40]]
        # 按字数比例分配时间
        total_chars = sum(len(s) for s in merged)
        seg_cursor = cursor
        for seg_text in merged:
            seg_dur = da * (len(seg_text) / total_chars) if total_chars > 0 else da / len(merged)
            segments.append((seg_cursor, seg_cursor + seg_dur, seg_text))
            seg_cursor += seg_dur
        cursor += da
    return segments


def _generate_srt(project: VideoProject, srt_path: str) -> str:
    """根据各分镜旁白文本与音频时长生成 SRT 字幕文件(仅用于下载,不再用于烧录)。

    V11.0: 按句号/逗号切分长文本为多条短字幕。
    V12.0: 复用 _build_subtitle_segments,字幕烧录已改用 drawtext 滤镜链。
    """
    segments = _build_subtitle_segments(project)
    lines: List[str] = []
    for idx, (seg_start, seg_end, seg_text) in enumerate(segments, 1):
        lines.append(str(idx))
        lines.append(f"{_format_srt_time(seg_start)} --> {_format_srt_time(seg_end)}")
        lines.append(seg_text)
        lines.append("")
    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
    logger.info("[Compositor] SRT 字幕生成(仅下载用): %s (%d 条)", srt_path, len(segments))
    return srt_path


# ---------------------------------------------------------------------------
# 阶段 C:最终合成(xfade + tpad + ducking + 字幕,单次 ffmpeg)
# ---------------------------------------------------------------------------

def _build_final_graph(
    project: VideoProject,
    aligned_paths: List[str],
    bgm_path: str,
    output_path: str,
    ffmpeg_exe: str,
    n: int,
    durations: List[float],
    total_duration: float,
    transition: float,
    has_voiceover: bool,
    sub_segments: list,
    sub_fontfile: str | None,
    sub_font_size: int,
    sub_margin: int,
    vid_h: int,
    hook_text: str | None,
    voiceover_path: str,
    voiceover_ok: bool,
    opts: dict,
) -> "object":
    """按档位 opts 构建 ffmpeg filter graph,返回 output 节点。

    opts 结构:
        use_subtitles: bool  —— 是否烧录 drawtext 字幕
        use_hook:      bool  —— 是否叠加 Hook 花字
        audio_mode:    str   —— voiceover_bgm / voiceover_only / bgm_only / none
    """
    # ---- 视频轨:xfade 链 ----
    aligned_inputs = [ffmpeg.input(p) for p in aligned_paths]
    v_streams = [
        ai.video
        .filter("setpts", "PTS-STARTPTS")
        .filter("fps", fps=settings.OUTPUT_FPS)
        for ai in aligned_inputs
    ]

    if n == 1:
        chain = v_streams[0]
    else:
        chain = v_streams[0]
        cumulative = durations[0]
        for k in range(1, n):
            offset = cumulative - k * transition
            chain = ffmpeg.filter(
                [chain, v_streams[k]],
                "xfade",
                transition="fade",
                duration=transition,
                offset=round(offset, 3),
            )
            cumulative += durations[k]
        pad_duration = round((n - 1) * transition, 3)
        if pad_duration > 0:
            chain = chain.filter(
                "tpad", stop_mode="clone", stop_duration=pad_duration
            )

    # ---- Hook 花字 ----
    if opts.get("use_hook") and hook_text and hook_text.strip():
        fontfile = _resolve_hook_fontfile()
        drawtext_kwargs: dict = {
            "text": _escape_drawtext_text(hook_text.strip()),
            "fontsize": 72,
            "fontcolor": "white",
            "borderw": 4,
            "bordercolor": "black",
            "x": "(w-text_w)/2",
            "y": "(h-text_h)/2",
            "alpha": "if(lt(t,2),1,if(lt(t,3),3-t,0))",
            "enable": "between(t,0,3)",
        }
        if fontfile:
            drawtext_kwargs["fontfile"] = fontfile
        chain = chain.filter("drawtext", **drawtext_kwargs)

    # ---- 字幕烧录(drawtext 链) ----
    if opts.get("use_subtitles"):
        for seg_start, seg_end, seg_text in sub_segments:
            dt_sub_kwargs: dict = {
                "text": _escape_drawtext_text(seg_text),
                "fontsize": sub_font_size,
                "fontcolor": "white",
                "borderw": 3,
                "bordercolor": "black",
                "x": "(w-text_w)/2",
                "y": f"h-th-{sub_margin}",
                "enable": f"between(t,{seg_start:.3f},{seg_end:.3f})",
            }
            if sub_fontfile:
                dt_sub_kwargs["fontfile"] = sub_fontfile
            chain = chain.filter("drawtext", **dt_sub_kwargs)

    # ---- 音频轨 ----
    audio_mode = opts.get("audio_mode", "none")
    final_audio = None

    if audio_mode == "voiceover_bgm" and voiceover_ok:
        # 有配音:voiceover(主) + BGM(sidechaincompress ducking) amix
        bgm_in = (
            ffmpeg.input(bgm_path, stream_loop=-1)
            if (bgm_path and Path(bgm_path).exists())
            else None
        )
        if bgm_in is None:
            audio_mode = "voiceover_only"
        else:
            voice_main = ffmpeg.input(voiceover_path)
            voice_sc = ffmpeg.input(voiceover_path)
            bgm = (
                bgm_in.audio
                .filter("atrim", duration=round(total_duration, 3))
                .filter("asetpts", "PTS-STARTPTS")
            )
            bgm_ducked = ffmpeg.filter(
                [bgm, voice_sc.audio],
                "sidechaincompress",
                threshold=0.05, ratio=8, attack=5, release=300, makeup=2,
            ).filter("volume", 0.35)
            final_audio = ffmpeg.filter(
                [voice_main.audio, bgm_ducked],
                "amix", inputs=2, duration="first", dropout_transition=0,
            )

    if audio_mode == "voiceover_only" and voiceover_ok:
        # 仅旁白(无字幕/BGM 的降级档位)
        final_audio = ffmpeg.input(voiceover_path).audio

    elif audio_mode == "bgm_only":
        # 仅 BGM 循环(无配音模式 / 配音缺失降级)
        if bgm_path and Path(bgm_path).exists():
            final_audio = (
                ffmpeg.input(bgm_path, stream_loop=-1).audio
                .filter("atrim", duration=round(total_duration, 3))
                .filter("asetpts", "PTS-STARTPTS")
                .filter("volume", 0.5)
            )
        else:
            logger.warning("[Compositor] bgm_only 但 BGM 文件不可用,退化为纯视频")
            final_audio = None

    # ---- 输出 ----
    common = dict(
        vcodec="libx264",
        crf=settings.OUTPUT_CRF,
        pix_fmt="yuv420p",
        preset="medium",
        r=settings.OUTPUT_FPS,
        **{"movflags": "+faststart"},
    )
    if final_audio is not None:
        out = ffmpeg.output(
            chain, final_audio, output_path, acodec="aac", ab="192k", **common
        )
    else:
        # 极简档位:纯视频,无音频轨
        out = ffmpeg.output(chain, output_path, **common)
    return out


def _run_ffmpeg(out: "object", ffmpeg_exe: str, output_path: str, level_name: str) -> None:
    """执行单次 ffmpeg 合成(经统一执行器),校验产物。

    失败时抛出 RuntimeError(携带完整命令与 stderr 末段),供降级档位捕获。
    """
    # 编译完整命令(必须成功——否则无法用带超时的 subprocess 执行)
    cmd_args = ffmpeg.compile(out, cmd=ffmpeg_exe, overwrite_output=True)
    # 统一执行器:强制 -y + stdin=DEVNULL + timeout=300 + 完整命令/ stderr 日志
    _run_ffmpeg_cmd(cmd_args, level_name, timeout=300)
    # 产物校验:必须真实存在且非空
    _verify_product(output_path, level_name)


def _compose_ultra_minimal(
    input_paths: List[str],
    output_path: str,
    ffmpeg_exe: str,
    aspect_ratio: str = "9:16",
) -> None:
    """极简保底合成:仅把分镜 MP4 顺序拼接成可播放视频(不加任何字幕/音频/转场)。

    这是"无论发生什么,只要分镜视频在就必出 MP4"的终极兜底:
      - 先试 concat demuxer + -c copy(极速,要求参数一致;aligned 视频必然一致)
      - 失败则降级为 scale 统一分辨率 + filter_complex concat 重编码(纯视频)
    两档均经统一执行器:强制 -y + stdin=DEVNULL + timeout=300 + 完整命令/ stderr。
    """
    valid = [
        p for p in input_paths
        if p and os.path.exists(p) and os.path.getsize(p) > 0
    ]
    if len(valid) < 1:
        raise RuntimeError("[Compositor] 极简保底失败: 没有任何有效分镜视频可拼接")

    target_w, target_h = _get_aspect_resolution(aspect_ratio)

    if len(valid) == 1:
        cmd = [ffmpeg_exe, "-y", "-i", valid[0], "-c", "copy", output_path]
        _run_ffmpeg_cmd(cmd, "L4_极简拼接(单分镜)", timeout=300)
        _verify_product(output_path, "L4_极简拼接")
        return

    # 多分镜:先试 concat demuxer -c copy(最快)
    list_path = os.path.join(os.path.dirname(output_path), "_ultra_minimal_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in valid:
            ap = os.path.abspath(p).replace("\\", "/").replace("'", r"'\''")
            f.write(f"file '{ap}'\n")
    copy_cmd = [
        ffmpeg_exe, "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", output_path,
    ]
    try:
        _run_ffmpeg_cmd(copy_cmd, "L4_极简拼接(copy)", timeout=300)
        _verify_product(output_path, "L4_极简拼接")
        return
    except Exception as copy_exc:  # noqa: BLE001
        logger.warning(
            "[Compositor] L4 -c copy 拼接失败,降级为重编码拼接: %s",
            copy_exc,
        )

    # 重编码拼接(纯视频,scale 统一分辨率,无音频/字幕/转场)
    inputs = [ffmpeg.input(p) for p in valid]
    vstreams = [
        ip.video.filter("scale", target_w, target_h).filter("setsar", 1)
        for ip in inputs
    ]
    vcat = ffmpeg.concat(*vstreams, v=1, a=0)
    out = ffmpeg.output(
        vcat, output_path,
        vcodec="libx264", crf=23, pix_fmt="yuv420p",
        preset="veryfast", r=settings.OUTPUT_FPS,
        **{"movflags": "+faststart"},
    )
    re_cmd = ffmpeg.compile(out, cmd=ffmpeg_exe, overwrite_output=True)
    _run_ffmpeg_cmd(re_cmd, "L4_极简拼接(重编码)", timeout=300)
    _verify_product(output_path, "L4_极简拼接")


def _compose_final_sync(
    project: VideoProject,
    aligned_paths: List[str],
    audio_paths: List[str],
    bgm_path: str,
    srt_path: Optional[str],
    output_path: str,
    storage_root: str,
    ffmpeg_exe: str,
) -> str:
    """最终合成(同步阻塞)——带三级降级档位,保证"只要有分镜视频就出 MP4"。

    档位设计(逐档简化,任一档失败自动退到下一档):
      L1 完美合成 : 视频xfade转场 + Hook花字 + 字幕烧录 + 旁白 + BGM(ducking)
      L2 降级合成 : 视频xfade转场 + 仅旁白(去掉字幕/BGM/Hook花字)
      L3 极简合成 : 纯视频xfade转场拼接(无任何音频与字幕)

    只要分镜视频在,至少 L3 一定能吐出可播放的 MP4。
    """
    scenes = project.scenes
    n = len(scenes)
    has_voiceover = project.input.enable_voiceover
    transition = settings.TRANSITION_DURATION

    # 时长:有配音用音频时长(音画已对齐),无配音用视频原始时长
    if has_voiceover:
        durations = [s.assets.audio.duration or 0.0 for s in scenes]
    else:
        durations = [s.assets.video_clip.duration or 0.0 for s in scenes]
    total_duration = sum(durations)

    # ---- 字幕片段预构建(供 drawtext 链) ----
    sub_segments = _build_subtitle_segments(project) if has_voiceover else []
    _, vid_h = _get_aspect_resolution(project.input.aspect_ratio)
    sub_font_size = 32 if vid_h <= 1080 else 44
    sub_margin = 80
    sub_fontfile = _resolve_subtitle_fontfile() if sub_segments else None
    hook_text = scenes[0].hook_text if scenes else None
    voiceover_path = str(Path(storage_root) / "temp" / "voiceover.mp3")
    voiceover_ok = (
        has_voiceover
        and Path(voiceover_path).exists()
        and Path(voiceover_path).stat().st_size > 0
    )

    if sub_segments:
        logger.info(
            "[Compositor] V12.0 drawtext 字幕: %d 条, FontSize=%d, y=h-th-%d (高度=%d)",
            len(sub_segments), sub_font_size, sub_margin, vid_h,
        )
    logger.info(
        "[Compositor] 最终合成准备: 分镜=%d, 转场=%.1fs, 总时长=%.2fs, "
        "配音=%s, 字幕段=%d, 旁白文件=%s, BGM=%s",
        n, transition, total_duration, has_voiceover, len(sub_segments),
        "有" if voiceover_ok else "无", "有" if bgm_path else "无",
    )

    # 三级档位(从完美到极简)
    levels = [
        (
            "L1_完美合成",
            dict(
                use_subtitles=True, use_hook=True,
                audio_mode=("voiceover_bgm" if voiceover_ok else "bgm_only"),
            ),
        ),
        (
            "L2_降级(去字幕/BGM)",
            dict(
                use_subtitles=False, use_hook=False,
                audio_mode=("voiceover_only" if voiceover_ok else "bgm_only"),
            ),
        ),
        (
            "L3_极简(纯视频)",
            dict(use_subtitles=False, use_hook=False, audio_mode="none"),
        ),
    ]

    last_exc: Exception | None = None
    for lvl_name, opts in levels:
        try:
            logger.info("[Compositor] ▶ 尝试合成档位: %s", lvl_name)
            out = _build_final_graph(
                project, aligned_paths, bgm_path, output_path, ffmpeg_exe,
                n, durations, total_duration, transition, has_voiceover,
                sub_segments, sub_fontfile, sub_font_size, sub_margin, vid_h,
                hook_text, voiceover_path, voiceover_ok, opts,
            )
            _run_ffmpeg(out, ffmpeg_exe, output_path, lvl_name)
            logger.warning(
                "[Compositor] ✅ 合成成功(档位=%s): %s", lvl_name, output_path
            )
            return output_path
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.error(
                "[Compositor] ❌ 档位 %s 失败: %s", lvl_name, exc
            )
            continue

    # === 终极保底(L4): 只要分镜视频在,必出一可播放 MP4 ===
    logger.warning(
        "[Compositor] ⚠️ L1/L2/L3 全部失败,触发极简保底合成: "
        "仅顺序拼接分镜视频(无字幕/无音频/无转场)"
    )
    try:
        _compose_ultra_minimal(
            aligned_paths, output_path, ffmpeg_exe, project.input.aspect_ratio
        )
        logger.warning(
            "[Compositor] ✅ 极简保底合成成功(档位=L4_极简拼接): %s", output_path
        )
        return output_path
    except Exception as l4_exc:  # noqa: BLE001
        logger.error("[Compositor] ❌ 极简保底合成也失败: %s", l4_exc)

    raise RuntimeError(
        f"所有合成档位(L1/L2/L3/L4)均失败,最后错误: {last_exc}; L4: {l4_exc}"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

class Compositor:
    """后期剪辑合成器。

    编排阶段 A/B/C,将 VideoProject 中各分镜的素材合成为最终 MP4。
    """

    async def composite(
        self, project: VideoProject, storage_root: str | None = None
    ) -> str:
        """合成最终视频,更新 project.output。

        V4.0:根据 enable_voiceover 切换合成模式:
          - True: voiceover 拼接 + SRT 字幕 + BGM ducking
          - False: 跳过 voiceover/SRT,仅 BGM 循环

        Args:
            project: 已完成阶段①②③(④可选)的 VideoProject
            storage_root: 本地存储根目录(默认 settings.STORAGE_ROOT)

        Returns:
            最终 MP4 的本地路径
        """
        if not project.scenes:
            raise RuntimeError("项目无分镜,无法合成")
        storage_root = storage_root or settings.STORAGE_ROOT

        ffmpeg_exe = _get_ffmpeg_exe()
        has_voiceover = project.input.enable_voiceover

        # ---- FFmpeg 环境自检:确认可执行文件能真正运行,而非仅路径存在 ----
        try:
            _ver = subprocess.run(
                [ffmpeg_exe, "-version"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if _ver.returncode != 0:
                raise RuntimeError(f"ffmpeg -version 返回非零: {_ver.stderr[-500:]}")
            _ver_line = _ver.stdout.splitlines()[0] if _ver.stdout else ffmpeg_exe
            logger.info("[%s] FFmpeg 环境自检通过: %s", project.project_id, _ver_line)
        except Exception as ffchk:  # noqa: BLE001
            raise RuntimeError(
                f"FFmpeg 环境自检失败(命令无法执行): {ffchk}"
            ) from ffchk

        logger.info(
            "[%s] 阶段⑤ 合成开始(ffmpeg: %s, 配音=%s)",
            project.project_id, ffmpeg_exe, has_voiceover,
        )

        # 阶段 A:素材准备
        video_paths, audio_paths, bgm_path = await _prepare_assets(
            project, storage_root
        )

        # ---- 输入文件自检(防"无米之炊") ----
        logger.info(
            "[Compositor] 输入文件自检: 视频(%d个), 音频(%d个), 字幕(%s), BGM(%s)",
            len(video_paths), len(audio_paths),
            "有" if has_voiceover else "无",
            "有" if bgm_path else "无",
        )

        # === V-FFMPEG-GUARD: 逐个 os.path.getsize 强制校验所有输入产出物 ===
        # 视频已由 _prepare_assets 下载到本地;音频已在阶段④落盘。此处在真正
        # 调用 FFmpeg 之前做最后一道硬门槛:任一文件不存在或 0 字节立刻抛异常,
        # 绝不让 FFmpeg 处理空文件而卡死或产出损坏视频。
        _labeled_inputs: List[tuple[str, str]] = [
            (f"视频[{s.scene_id}]", vp)
            for s, vp in zip(project.scenes, video_paths)
        ]
        if has_voiceover:
            _labeled_inputs += [
                (f"音频[{s.scene_id}]", ap)
                for s, ap in zip(project.scenes, audio_paths)
            ]
        if bgm_path:
            _labeled_inputs.append(("BGM", bgm_path))
        _assert_inputs_nonempty(project.project_id, _labeled_inputs)

        # 阶段 B:音画对齐(并行)
        try:
            aligned_paths = await _align_all_scenes(
                project, video_paths, audio_paths, storage_root
            )
        except Exception as align_exc:  # noqa: BLE001
            # 对齐阶段任一分镜异常(含超时)不得让整条流水线失败——
            # 降级为直接使用原始分镜视频进行后续的最终保底拼接。
            logger.warning(
                "[Compositor] ⚠️ 音画对齐阶段异常(%s),降级为直接使用原始分镜视频拼接",
                align_exc,
            )
            aligned_paths = list(video_paths)

        # 对齐产物强制生死校验:任一对齐视频缺失/0字节立刻抛异常,
        # 不让最终合成去处理坏文件(坏文件会导致 FFmpeg 卡死或产出损坏)。
        _assert_inputs_nonempty(
            project.project_id,
            [(f"对齐视频[{i}]", p) for i, p in enumerate(aligned_paths)],
        )

        # 拼接 voiceover + 生成 SRT(仅配音模式)
        # V7.0: 配音关闭时 srt_path=None,彻底杜绝字幕残留
        srt_path: Optional[str] = None
        if has_voiceover:
            srt_path = str(Path(storage_root) / "temp" / f"{project.project_id}.srt")
            voiceover_path = str(Path(storage_root) / "temp" / "voiceover.mp3")
            _concat_voiceover(audio_paths, voiceover_path, storage_root)
            _generate_srt(project, srt_path)
        else:
            logger.info("[%s] 配音关闭,跳过 voiceover 拼接与 SRT 生成(srt_path=None)", project.project_id)
            # V7.0: 清理可能残留的旧 SRT 和 voiceover 文件(防止跨任务污染)
            stale_srt = Path(storage_root) / "temp" / f"{project.project_id}.srt"
            stale_voiceover = Path(storage_root) / "temp" / "voiceover.mp3"
            for stale in [stale_srt, stale_voiceover]:
                if stale.exists():
                    stale.unlink()
                    logger.info("[Compositor] 清理残留文件: %s", stale.name)

        # 阶段 C:最终合成
        outputs_dir = Path(storage_root) / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(outputs_dir / f"{project.project_id}.mp4")

        await asyncio.to_thread(
            _compose_final_sync,
            project, aligned_paths, audio_paths, bgm_path,
            srt_path, output_path, storage_root, ffmpeg_exe,
        )

        # 更新 project 输出
        if has_voiceover:
            total_duration = sum(
                (s.assets.audio.duration or 0.0) for s in project.scenes
            )
            project.output.subtitle_url = srt_path
        else:
            total_duration = sum(
                (s.assets.video_clip.duration or 0.0) for s in project.scenes
            )
            project.output.subtitle_url = None  # V7.0: 无配音时清除字幕URL
        project.output.local_path = output_path
        # V17.2: 相对路径,经 /storage 静态挂载对外提供(非系统绝对路径 /app/...),
        # 浏览器可直链 https://域名/storage/outputs/xxx.mp4 访问。
        project.output.final_video_url = f"/storage/outputs/{project.project_id}.mp4"
        project.output.duration_sec = round(total_duration, 3)

        for s in project.scenes:
            s.status = SceneStatus.SYNCED

        logger.info(
            "[%s] 阶段⑤ 合成完成: %s (时长 %.2fs)",
            project.project_id, output_path, total_duration,
        )
        return output_path
