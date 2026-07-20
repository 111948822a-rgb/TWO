"""项目接口:创建任务 / 查询状态 / 下载视频 / 批量生成 / CSV导入 / ZIP打包。

任务状态由 SQLite 持久化驱动(多 Gunicorn worker 安全),后台用 asyncio.create_task 跑 run_pipeline,状态实时落盘。
图片输入支持多文件上传:image_file(List[UploadFile])逐张抠图+规范化+上传 OSS,
LLM 会为每个分镜分配 image_index 选择对应产品图;亦可单图回退 image_url。

V5.0 新增:
    - POST /api/projects/batch      批量生成(语言×氛围×脚本数 笛卡尔积)
    - POST /api/projects/import-csv CSV 文本导入批量创建
    - GET  /api/projects/zip-download?task_ids=id1,id2  一键打包下载
    - 批量任务串行执行(规避阿里云 API 并发限流)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.routes.auth import get_current_user
from app.core.config import settings
from app.core.database import delete_project, get_project_detail, sync_project_from_model
from app.pipelines.orchestrator import run_pipeline
from app.schemas.project import (
    KeyframeImage,
    ProjectStatus,
    VideoProject,
    VideoClip,
)
from app.services.image_generator import ImageGenerator
from app.services.video_generator import VideoGenerator
from app.utils.image_utils import ensure_rgba_transparent, normalize_to_rgba_png
from app.utils.oss_client import delete_oss_object, upload_image_to_oss, upload_video_to_oss

logger = logging.getLogger(__name__)

# V17.0: 所有业务路由强制登录(未登录拒绝访问), 当前用户通过 get_current_user 注入
router = APIRouter(
    prefix="/api/projects",
    tags=["projects"],
    dependencies=[Depends(get_current_user)],
)

# SQLite 状态驱动:任务进度/状态/当前阶段全部持久化到 SQLite(projects 表),
# 多 Gunicorn worker 通过共享挂载盘上的同一 SQLite 文件读取最新状态,无需 Redis。
# _task_refs 仅持有后台 asyncio 任务的引用(防 GC / 支持取消),不作为状态来源,
# 所有状态读取一律走数据库(见 load_project)。
_task_refs: Dict[str, asyncio.Task] = {}  # 后台任务引用(防 GC / 取消用)

# V17.4: STAGE_LABELS 集中到 app.core.constants(避免与 orchestrator 循环依赖)
from app.core.constants import STAGE_LABELS


# ---------------------------------------------------------------------------
# 公共辅助:图片上传处理 + OSS 自检
# ---------------------------------------------------------------------------

async def _process_uploads(
    valid_files: List[UploadFile], task_id: str,
    defringe_strength: str = "medium",
) -> tuple[str, List[str]]:
    """逐张抠图+规范化+上传 OSS,返回 (主图URL, 全部图URL列表)。

    V6.1: defringe_strength 控制抠图后的边缘腐蚀与羽化强度。
    """
    uploads_dir = Path(settings.STORAGE_ROOT) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    image_urls: List[str] = []
    for i, f in enumerate(valid_files):
        raw_content = await f.read()
        suffix = Path(f.filename).suffix or ".png"
        raw_path = uploads_dir / f"{task_id}_{i}_raw{suffix}"
        raw_path.write_bytes(raw_content)

        # 核心格式兜底:确保 RGBA 透明 PNG(覆盖 SKIP_MATTING 两条分支)
        try:
            upload_content = ensure_rgba_transparent(
                raw_content, settings.SKIP_MATTING,
                defringe_strength=defringe_strength,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 图%d RGBA 兜底失败,用原图: %s", task_id, i, exc)
            upload_content = raw_content

        # 尺寸与长宽比自适应(200~4096、长宽比≤2.5)
        try:
            upload_content = normalize_to_rgba_png(upload_content)
            norm_path = uploads_dir / f"{task_id}_{i}_normalized.png"
            norm_path.write_bytes(upload_content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 图%d 尺寸规范化失败,用原数据: %s", task_id, i, exc)

        # 上传 OSS
        try:
            url = upload_image_to_oss(upload_content, f"{task_id}_{i}.png")
            image_urls.append(url)
            logger.info("[%s] 图%d 上传 OSS 完成: %s", task_id, i, url)
        except RuntimeError as exc:
            logger.warning("[%s] 图%d OSS 上传失败: %s", task_id, i, exc)

    if not image_urls:
        raise HTTPException(
            status_code=400,
            detail="所有图片 OSS 上传失败",
        )
    return image_urls[0], image_urls


async def _check_oss_urls(check_urls: List[str], task_id: str) -> None:
    """OSS URL 连通性自检(防呆:Bucket 权限未公共读 / 网络不通)。"""
    if not check_urls:
        return
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=10.0)
        ) as client:
            for u in check_urls:
                resp = await client.get(u, follow_redirects=True)
                if resp.status_code == 403:
                    raise HTTPException(
                        status_code=400,
                        detail="OSS Bucket 权限未设置为公共读(403 Forbidden),请到阿里云 OSS 控制台将 Bucket 读写权限改为「公共读」",
                    )
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"OSS 图片 URL 无法公网访问(HTTP {resp.status_code}): {u}",
                    )
        logger.info("[%s] OSS URL 自检通过(%d 张)", task_id, len(check_urls))
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"OSS URL 自检失败(网络错误): {exc}",
        ) from exc


def _parse_selling_points(selling_points: str) -> List[str]:
    """解析卖点字符串(逗号分隔,兼容中英文逗号)。"""
    return [
        s.strip()
        for s in selling_points.replace("，", ",").split(",")
        if s.strip()
    ]


async def _run_safe(
    project: VideoProject,
    from_stage: Optional[ProjectStatus] = None,
    until: Optional[ProjectStatus] = None,
) -> None:
    """后台运行流水线(run_pipeline 已捕获异常并写入 project.error)。

    V7.0: from_stage 参数支持导演模式从指定阶段继续执行。
    V9.0: until 参数支持候选池模式跑到指定阶段后暂停(AWAITING_SELECTION)。
    V10.0 P0: finally 块强制同步最终状态到 SQLite,绝不允许静默卡死。
    """
    try:
        await run_pipeline(project, from_stage=from_stage, until=until)
    except Exception as exc:  # noqa: BLE001
        import traceback
        project.status = ProjectStatus.FAILED
        project.error = f"{type(exc).__name__}: {exc}"
        project.technical_traceback = traceback.format_exc()
        logger.exception("[%s] 后台流水线异常", project.project_id)
    finally:
        # P0: 确保最终状态总是同步到 SQLite,绝不允许任务静默卡死
        try:
            sync_project_from_model(project)
        except Exception as sync_exc:  # noqa: BLE001
            logger.warning(
                "[%s] 最终状态同步 SQLite 失败(非致命): %s",
                project.project_id, sync_exc,
            )


async def _run_batch_sequential(projects: List[VideoProject]) -> None:
    """批量任务串行执行(规避阿里云 API 并发限流)。

    图片(通义万相)/视频(HappyHorse 1.1) 接口"同时处理中任务数=1",
    多个任务若并发会触发限流,故同批内串行。
    """
    for i, project in enumerate(projects):
        logger.info(
            "[%s] 批量任务 %d/%d 开始 (lang=%s, vibe=%s)",
            project.project_id, i + 1, len(projects),
            project.input.language, project.input.vibe,
        )
        await _run_safe(project)


def _spawn_task(key: str, coro) -> asyncio.Task:
    """启动后台流水线任务并持有引用(防 GC),任务完成后自动清理引用。

    仅作为 asyncio.Task 的生命周期句柄 —— 任务状态本身由 SQLite 承载,
    不在此处缓存任何状态数据。
    """
    task = asyncio.create_task(coro)
    def _on_done(_t):
        if _task_refs.get(key) is task:
            _task_refs.pop(key, None)
    task.add_done_callback(_on_done)
    _task_refs[key] = task
    return task


def load_project(task_id: str) -> Optional[VideoProject]:
    """从 SQLite 重建 VideoProject(唯一事实来源)。

    替代原内存 _tasks 字典,使任意 Gunicorn worker 都能读取到最新任务状态,
    彻底解决多 worker 进度不一致 / 丢进度问题。
    """
    db_data = get_project_detail(task_id)
    if not db_data:
        return None
    scenes_data = db_data.get("scenes_data") or {}
    try:
        return VideoProject.model_validate(scenes_data)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] 从 SQLite 重建项目失败: %s", task_id, exc)
        return None


# ---------------------------------------------------------------------------
# 单任务接口
# ---------------------------------------------------------------------------

@router.post("")
async def create_project(
    product_name: str = Form(...),
    selling_points: str = Form(""),
    target_audience: str = Form(""),
    duration_target_sec: int = Form(15),
    style: str = Form("生活化"),
    language: str = Form("en"),
    enable_voiceover: bool = Form(True),
    vibe: str = Form("upbeat"),
    visual_style: str = Form("photorealistic"),
    defringe_strength: str = Form("medium"),
    aspect_ratio: str = Form("9:16"),
    product_material: str = Form("other"),
    image_url: Optional[str] = Form(None),
    image_file: List[UploadFile] = File([]),
    current_user: dict = Depends(get_current_user),
):
    """创建视频生成任务,异步触发 5 阶段流水线,返回 task_id。

    V17.0: 标记 creator_name(当前登录用户的显示名), 便于全员共享历史下追溯创建者。
    """
    task_id = uuid.uuid4().hex[:12]

    white_image_url = (image_url or "").strip()
    image_urls: List[str] = []

    # 多文件上传优先:image_file > image_url
    valid_files = [f for f in image_file if f and f.filename]
    if valid_files:
        logger.info("[%s] 收到 %d 张产品图,开始逐张处理", task_id, len(valid_files))
        white_image_url, image_urls = await _process_uploads(
            valid_files, task_id, defringe_strength=defringe_strength
        )
    elif not white_image_url:
        raise HTTPException(
            status_code=400,
            detail="请上传 image_file 或填写 image_url",
        )

    # OSS URL 连通性自检
    check_urls = image_urls if image_urls else (
        [white_image_url] if white_image_url.startswith("http") else []
    )
    await _check_oss_urls(check_urls, task_id)

    sp = _parse_selling_points(selling_points)

    project = VideoProject(
        project_id=task_id,
        input={
            "product_name": product_name,
            "product_description": selling_points,
            "selling_points": sp,
            "target_audience": target_audience,
            "white_image_url": white_image_url,
            "image_urls": image_urls,
            "duration_target_sec": duration_target_sec,
            "style": style,
            "language": language,
            "enable_voiceover": enable_voiceover,
            "vibe": vibe,
            "visual_style": visual_style,
            "defringe_strength": defringe_strength,
            "aspect_ratio": aspect_ratio,
            "product_material": product_material,
        },
    )
    sync_project_from_model(project, creator_name=current_user["display_name"])
    _spawn_task(task_id, _run_safe(project))

    logger.info(
        "[%s] 任务已创建, 主图=%s, 多图=%d 张",
        task_id, white_image_url, len(image_urls),
    )
    return {"task_id": task_id, "status": project.status.value}


# ---------------------------------------------------------------------------
# V5.0 批量生成:爆款矩阵 (语言×氛围×脚本数 笛卡尔积)
# ---------------------------------------------------------------------------

@router.post("/batch")
async def create_batch(
    product_name: str = Form(...),
    selling_points: str = Form(""),
    target_audience: str = Form(""),
    duration_target_sec: int = Form(15),
    style: str = Form("生活化"),
    enable_voiceover: bool = Form(True),
    languages: List[str] = Form(...),
    vibes: List[str] = Form(...),
    scripts_per_combo: int = Form(1),
    visual_style: str = Form("photorealistic"),
    defringe_strength: str = Form("medium"),
    aspect_ratio: str = Form("9:16"),
    product_material: str = Form("other"),
    image_url: Optional[str] = Form(None),
    image_file: List[UploadFile] = File([]),
    current_user: dict = Depends(get_current_user),
):
    """批量生成:语言×氛围×脚本数 笛卡尔积,一键创建多个任务。

    V17.0: 批量任务同样标记 creator_name(当前登录用户)。

    所有任务共享同一组产品图(只上传一次 OSS),同批内串行执行
    (规避阿里云 API 并发限流)。

    Args:
        languages: 目标语言数组(en/th/id)
        vibes: 视频氛围数组(upbeat/premium/chill)
        scripts_per_combo: 每个组合生成几个脚本(1-3)
    """
    batch_id = uuid.uuid4().hex[:8]

    # 参数校验
    if scripts_per_combo < 1 or scripts_per_combo > 3:
        raise HTTPException(
            status_code=400,
            detail="scripts_per_combo 必须在 1-3 之间",
        )
    valid_langs = ["en", "th", "id"]
    valid_vibes = ["upbeat", "premium", "chill", "cinematic", "viral", "asmr", "urgent"]
    valid_styles = ["photorealistic", "3d_render", "anime", "cyberpunk"]
    for lang in languages:
        if lang not in valid_langs:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的语言: {lang}, 可选: {valid_langs}",
            )
    for vibe in vibes:
        if vibe not in valid_vibes:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的氛围: {vibe}, 可选: {valid_vibes}",
            )
    if visual_style not in valid_styles:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视觉风格: {visual_style}, 可选: {valid_styles}",
        )
    valid_defringe = ["off", "light", "medium", "heavy"]
    if defringe_strength not in valid_defringe:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的去白边强度: {defringe_strength}, 可选: {valid_defringe}",
        )
    valid_materials = ["glass", "metal", "plastic", "fabric", "electronics", "other"]
    if product_material not in valid_materials:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的产品材质: {product_material}, 可选: {valid_materials}",
        )
    if not languages or not vibes:
        raise HTTPException(
            status_code=400, detail="languages 和 vibes 不能为空"
        )

    # 图片处理(所有任务共享同一组产品图)
    white_image_url = (image_url or "").strip()
    image_urls: List[str] = []

    valid_files = [f for f in image_file if f and f.filename]
    if valid_files:
        logger.info(
            "[%s] 批量任务收到 %d 张产品图", batch_id, len(valid_files)
        )
        white_image_url, image_urls = await _process_uploads(
            valid_files, batch_id, defringe_strength=defringe_strength
        )
    elif not white_image_url:
        raise HTTPException(
            status_code=400,
            detail="请上传 image_file 或填写 image_url",
        )

    # OSS 自检(同批只检一次,共享图)
    check_urls = image_urls if image_urls else (
        [white_image_url] if white_image_url.startswith("http") else []
    )
    await _check_oss_urls(check_urls, batch_id)

    sp = _parse_selling_points(selling_points)

    # 笛卡尔积:languages × vibes × scripts_per_combo
    # V13.0: 每个子任务创建用 try/except 包裹,失败则跳过不返回假 ID
    combos: List[Dict] = []
    batch_projects: List[VideoProject] = []
    failed_count = 0
    for lang in languages:
        for vibe in vibes:
            for script_idx in range(1, scripts_per_combo + 1):
                task_id = uuid.uuid4().hex[:12]
                try:
                    project = VideoProject(
                        project_id=task_id,
                        input={
                            "product_name": product_name,
                            "product_description": selling_points,
                            "selling_points": sp,
                            "target_audience": target_audience,
                            "white_image_url": white_image_url,
                            "image_urls": image_urls,
                            "duration_target_sec": duration_target_sec,
                            "style": style,
                            "language": lang,
                            "enable_voiceover": enable_voiceover,
                            "vibe": vibe,
                            "visual_style": visual_style,
                            "defringe_strength": defringe_strength,
                            "aspect_ratio": aspect_ratio,
                            "product_material": product_material,
                        },
                    )
                    batch_projects.append(project)
                    sync_project_from_model(project, creator_name=current_user["display_name"])
                    combos.append({
                        "task_id": task_id,
                        "language": lang,
                        "vibe": vibe,
                        "script_index": script_idx,
                    })
                except Exception as exc:  # noqa: BLE001
                    failed_count += 1
                    logger.error(
                        "[%s] 批量子任务创建失败 (lang=%s, vibe=%s, idx=%d): %s",
                        task_id, lang, vibe, script_idx, exc,
                    )
                    # 不加入 combos,前端绝不会收到不存在的 task_id

    if failed_count > 0:
        logger.warning(
            "[%s] 批量创建完成: 成功 %d 个, 失败 %d 个",
            batch_id, len(combos), failed_count,
        )

    if not combos:
        raise HTTPException(
            status_code=500,
            detail=f"批量创建全部失败({failed_count} 个子任务),请检查日志",
        )

    # 串行执行(规避阿里云限流)
    _spawn_task(
        f"batch_{batch_id}",
        _run_batch_sequential(batch_projects),
    )

    total = len(combos)
    logger.info(
        "[%s] 批量任务已创建: %d 个 (lang=%s × vibe=%s × scripts=%d)",
        batch_id, total, languages, vibes, scripts_per_combo,
    )
    return {
        "batch_id": batch_id,
        "total": total,
        "tasks": combos,
    }


# ---------------------------------------------------------------------------
# V5.0 CSV 文本导入批量创建
# ---------------------------------------------------------------------------

@router.post("/import-csv")
async def import_csv(
    csv_file: UploadFile = File(...),
    language: str = Form("en"),
    vibe: str = Form("upbeat"),
    enable_voiceover: bool = Form(True),
    current_user: dict = Depends(get_current_user),
):
    """CSV 文本导入:解析表格为每行创建一个任务(默认英语+动感BGM)。

    CSV 格式:
        product_name, selling_points, image_urls
        Premium Tumbler, "24h insulation, leak-proof", https://oss.com/a.png,https://oss.com/b.png

    image_urls 列多个 URL 用逗号分隔(需用引号包裹避免 CSV 逗号歧义)。
    """
    if not csv_file.filename or not csv_file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 .csv 文件")

    content = await csv_file.read()
    # 兼容 BOM + 多种编码
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    required_cols = {"product_name", "selling_points", "image_urls"}
    if not reader.fieldnames or not required_cols.issubset(
        set(f.strip() for f in reader.fieldnames)
    ):
        raise HTTPException(
            status_code=400,
            detail=f"CSV 缺少必要列,需要: {required_cols}",
        )

    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV 无数据行")

    created: List[Dict] = []
    csv_projects: List[VideoProject] = []
    for i, row in enumerate(rows):
        product_name = (row.get("product_name") or "").strip()
        selling_points = (row.get("selling_points") or "").strip()
        image_urls_str = (row.get("image_urls") or "").strip()

        if not product_name or not image_urls_str:
            logger.warning("CSV 第 %d 行缺少 product_name 或 image_urls,跳过", i + 2)
            continue

        image_urls = [
            u.strip() for u in image_urls_str.split(",") if u.strip()
        ]
        if not image_urls:
            continue

        task_id = uuid.uuid4().hex[:12]
        sp = _parse_selling_points(selling_points)

        project = VideoProject(
            project_id=task_id,
            input={
                "product_name": product_name,
                "product_description": selling_points,
                "selling_points": sp,
                "target_audience": "",
                "white_image_url": image_urls[0],
                "image_urls": image_urls,
                "duration_target_sec": 15,
                "style": "生活化",
                "language": language,
                "enable_voiceover": enable_voiceover,
                "vibe": vibe,
            },
        )
        csv_projects.append(project)
        sync_project_from_model(project, creator_name=current_user["display_name"])
        created.append({
            "task_id": task_id,
            "language": language,
            "vibe": vibe,
            "script_index": 1,
            "product_name": product_name,
        })

    if not created:
        raise HTTPException(
            status_code=400, detail="CSV 解析后无有效任务行"
        )

    # 串行执行(规避阿里云限流)
    csv_batch_id = f"csv_{uuid.uuid4().hex[:8]}"
    _spawn_task(
        csv_batch_id,
        _run_batch_sequential(csv_projects),
    )

    logger.info("[CSV导入] 创建 %d 个任务", len(created))
    return {
        "batch_id": csv_batch_id,
        "total": len(created),
        "tasks": created,
    }


# ---------------------------------------------------------------------------
# V5.0 ZIP 打包下载
# ---------------------------------------------------------------------------

@router.get("/zip-download")
async def zip_download(task_ids: str):
    """一键打包下载多个已完成视频为 ZIP。

    用法: GET /api/projects/zip-download?task_ids=id1,id2,id3
    """
    ids = [t.strip() for t in task_ids.split(",") if t.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="task_ids 不能为空")

    files: List[tuple[str, Path]] = []
    missing: List[str] = []
    for tid in ids:
        project = load_project(tid)
        if not project:
            missing.append(tid)
            continue
        if project.status != ProjectStatus.COMPLETED or not project.output.local_path:
            missing.append(tid)
            continue
        path = Path(project.output.local_path)
        if not path.exists():
            missing.append(tid)
            continue
        files.append((tid, path))

    if not files:
        raise HTTPException(
            status_code=404,
            detail=f"没有可下载的已完成视频,缺失: {missing}",
        )

    # 生成 ZIP(内存缓冲,开发期任务数少可接受)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for tid, path in files:
            zf.write(path, f"{tid}.mp4")
    buf.seek(0)

    logger.info("[ZIP] 打包 %d 个视频 (跳过 %d 个)", len(files), len(missing))
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=batch_videos.zip"
        },
    )


# ---------------------------------------------------------------------------
# V7.0 导演模式:两阶段生成 API
# ---------------------------------------------------------------------------

class SceneEdit(BaseModel):
    """分镜编辑器提交的单分镜修改(用户可编辑字段)。"""

    scene_id: str
    narration: str
    image_prompt: str
    video_prompt: str
    visual_style: Optional[str] = None


class ContinueGenerationRequest(BaseModel):
    """导演模式继续生成请求体。"""

    scenes: List[SceneEdit]
    # V9.0 Director Mode Pro: 候选池模式每分镜生成 N 个候选(1=普通模式,2-3=候选池)
    candidates_per_scene: int = 1


@router.post("/generate-script")
async def generate_script(
    product_name: str = Form(...),
    selling_points: str = Form(""),
    target_audience: str = Form(""),
    duration_target_sec: int = Form(15),
    style: str = Form("生活化"),
    language: str = Form("en"),
    enable_voiceover: bool = Form(True),
    vibe: str = Form("upbeat"),
    visual_style: str = Form("photorealistic"),
    defringe_strength: str = Form("medium"),
    aspect_ratio: str = Form("9:16"),
    product_material: str = Form("other"),
    candidates_per_scene: int = Form(1),
    image_url: Optional[str] = Form(None),
    image_file: List[UploadFile] = File([]),
    current_user: dict = Depends(get_current_user),
):
    """V7.0 导演模式阶段①:只生成文案与分镜,返回 task_id + 完整 scenes JSON。

    V17.0: 标记 creator_name(当前登录用户)。

    用户在前端分镜编辑器中修改后,调用 /{task_id}/continue-generation 继续。
    同步执行(LLM 调用通常 5-15 秒),完成后直接返回分镜数据。

    V9.0: candidates_per_scene > 1 时启用候选池模式(导演模式 Pro),
    阶段②③每分镜生成 N 个候选,VID_GEN 完成后暂停为 AWAITING_SELECTION。
    """
    task_id = uuid.uuid4().hex[:12]

    white_image_url = (image_url or "").strip()
    image_urls: List[str] = []

    valid_files = [f for f in image_file if f and f.filename]
    if valid_files:
        white_image_url, image_urls = await _process_uploads(
            valid_files, task_id, defringe_strength=defringe_strength
        )
    elif not white_image_url:
        raise HTTPException(
            status_code=400,
            detail="请上传 image_file 或填写 image_url",
        )

    check_urls = image_urls if image_urls else (
        [white_image_url] if white_image_url.startswith("http") else []
    )
    await _check_oss_urls(check_urls, task_id)

    sp = _parse_selling_points(selling_points)

    project = VideoProject(
        project_id=task_id,
        input={
            "product_name": product_name,
            "product_description": selling_points,
            "selling_points": sp,
            "target_audience": target_audience,
            "white_image_url": white_image_url,
            "image_urls": image_urls,
            "duration_target_sec": duration_target_sec,
            "style": style,
            "language": language,
            "enable_voiceover": enable_voiceover,
            "vibe": vibe,
            "visual_style": visual_style,
            "defringe_strength": defringe_strength,
            "aspect_ratio": aspect_ratio,
            "product_material": product_material,
        },
        config={"candidates_per_scene": max(1, min(3, candidates_per_scene))},
    )
    sync_project_from_model(project, creator_name=current_user["display_name"])

    logger.info(
        "[%s] 导演模式阶段①:开始生成文案分镜(候选数=%d)",
        task_id, project.config.candidates_per_scene,
    )

    # 同步执行阶段①(只跑 SCRIPTING)
    try:
        await run_pipeline(project, until=ProjectStatus.SCRIPTING)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] 导演模式阶段①异常", task_id)
        raise HTTPException(
            status_code=500,
            detail=f"文案分镜生成失败: {project.error or str(exc)}",
        ) from exc

    if project.status == ProjectStatus.FAILED or not project.scenes:
        raise HTTPException(
            status_code=500,
            detail=f"文案分镜生成失败: {project.error or '未生成任何分镜'}",
        )

    logger.info(
        "[%s] 导演模式阶段①完成,生成 %d 个分镜,等待用户编辑",
        task_id, len(project.scenes),
    )

    return {
        "task_id": task_id,
        "status": project.status.value,
        "scenes": [s.model_dump() if hasattr(s, "model_dump") else s.dict() for s in project.scenes],
    }


# ---------------------------------------------------------------------------
# V15.0 拍同款:上传参考视频 + 产品图,用 Qwen-VL 分析视频提取分镜
# ---------------------------------------------------------------------------

@router.post("/clone")
async def clone_video(
    product_name: str = Form(...),
    selling_points: str = Form(""),
    target_audience: str = Form(""),
    style: str = Form("生活化"),
    language: str = Form("en"),
    enable_voiceover: bool = Form(True),
    vibe: str = Form("upbeat"),
    visual_style: str = Form("photorealistic"),
    defringe_strength: str = Form("medium"),
    aspect_ratio: str = Form("9:16"),
    product_material: str = Form("other"),
    reference_video: UploadFile = File(...),
    image_file: List[UploadFile] = File([]),
    current_user: dict = Depends(get_current_user),
):
    """V15.0 拍同款:上传参考视频 + 产品图,自动分析视频分镜并复刻。

    V17.0: 标记 creator_name(当前登录用户)。

    流程:参考视频→OSS→Qwen-VL分析→分镜提取→图片生成→视频生成→合成
    """
    task_id = uuid.uuid4().hex[:12]

    # 1. 上传参考视频到 OSS
    if not reference_video or not reference_video.filename:
        raise HTTPException(status_code=400, detail="请上传参考视频(MP4)")

    video_content = await reference_video.read()
    video_size_mb = len(video_content) / (1024 * 1024)
    if video_size_mb > 50:
        raise HTTPException(
            status_code=400,
            detail=f"参考视频过大({video_size_mb:.1f}MB),请压缩到 50MB 以内",
        )

    logger.info(
        "[%s] 拍同款:收到参考视频 %s (%.1fMB),开始上传 OSS",
        task_id,
        reference_video.filename,
        video_size_mb,
    )

    # 保存到本地临时目录
    temp_dir = Path(settings.STORAGE_ROOT) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    video_suffix = Path(reference_video.filename).suffix or ".mp4"
    local_video_path = temp_dir / f"{task_id}_ref_video{video_suffix}"
    local_video_path.write_bytes(video_content)

    # 上传 OSS
    try:
        reference_video_url = upload_video_to_oss(
            video_content, reference_video.filename
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"参考视频上传 OSS 失败: {exc}",
        )

    # 2. 处理产品图(复用现有抠图+OSS 上传逻辑)
    valid_files = [f for f in image_file if f and f.filename]
    if not valid_files:
        raise HTTPException(
            status_code=400,
            detail="请上传至少一张产品图(image_file)",
        )

    logger.info(
        "[%s] 拍同款:收到 %d 张产品图,开始处理",
        task_id,
        len(valid_files),
    )
    white_image_url, image_urls = await _process_uploads(
        valid_files, task_id, defringe_strength=defringe_strength
    )

    # OSS URL 连通性自检
    await _check_oss_urls(image_urls, task_id)

    sp = _parse_selling_points(selling_points)

    # 3. 创建项目(clone_mode=True)
    project = VideoProject(
        project_id=task_id,
        input={
            "product_name": product_name,
            "product_description": selling_points,
            "selling_points": sp,
            "target_audience": target_audience,
            "white_image_url": white_image_url,
            "image_urls": image_urls,
            "duration_target_sec": 15,
            "style": style,
            "language": language,
            "enable_voiceover": enable_voiceover,
            "vibe": vibe,
            "visual_style": visual_style,
            "defringe_strength": defringe_strength,
            "aspect_ratio": aspect_ratio,
            "product_material": product_material,
            "reference_video_url": reference_video_url,
            "clone_mode": True,
        },
    )
    sync_project_from_model(project, creator_name=current_user["display_name"])
    _spawn_task(task_id, _run_safe(project))

    logger.info(
        "[%s] 拍同款任务已创建, 参考视频=%s, 产品图=%d 张",
        task_id,
        reference_video_url,
        len(image_urls),
    )
    return {
        "task_id": task_id,
        "status": project.status.value,
        "clone_mode": True,
        "reference_video_url": reference_video_url,
    }


@router.post("/{task_id}/continue-generation")
async def continue_generation(
    task_id: str,
    body: ContinueGenerationRequest,
):
    """V7.0 导演模式阶段②:接收用户编辑后的 scenes,从图片生成阶段继续。

    请求体 JSON:
        {
            "scenes": [
                {
                    "scene_id": "scene_001",
                    "narration": "编辑后的旁白",
                    "image_prompt": "编辑后的生图提示词",
                    "video_prompt": "编辑后的运镜提示词",
                    "visual_style": "photorealistic"
                },
                ...
            ],
            "candidates_per_scene": 1
        }

    V9.0 Director Mode Pro:当 candidates_per_scene > 1 时,阶段②③生成 N 个候选,
    VID_GEN 完成后暂停为 AWAITING_SELECTION,等待用户选择候选素材。
    """
    project = load_project(task_id)
    if not project:
        raise HTTPException(status_code=404, detail="任务不存在")

    if not project.scenes:
        raise HTTPException(
            status_code=400,
            detail="该项目尚未生成初始分镜,无法继续",
        )

    # 用用户编辑更新内存中的 scenes(按 scene_id 匹配)
    edit_map = {e.scene_id: e for e in body.scenes}
    updated_count = 0
    for scene in project.scenes:
        edit = edit_map.get(scene.scene_id)
        if edit:
            scene.narration = edit.narration
            scene.image_prompt = edit.image_prompt
            scene.video_prompt = edit.video_prompt
            scene.visual_style = edit.visual_style
            updated_count += 1

    if updated_count == 0:
        raise HTTPException(
            status_code=400,
            detail="未匹配到任何可更新的分镜(scene_id 不一致)",
        )

    # V9.0: 更新候选池配置
    n_candidates = max(1, min(3, body.candidates_per_scene))
    project.config.candidates_per_scene = n_candidates
    director_mode_pro = n_candidates > 1

    logger.info(
        "[%s] 导演模式:用户编辑了 %d/%d 个分镜,从阶段②继续(候选数=%d,Pro=%s)",
        task_id, updated_count, len(project.scenes),
        n_candidates, director_mode_pro,
    )

    # 重置进度与错误状态,从 IMG_GEN 阶段继续执行(后台)
    project.progress = 0.0
    project.error = None
    project.technical_traceback = None

    # V9.0: 候选池模式跑到 VID_GEN 后暂停为 AWAITING_SELECTION
    if director_mode_pro:
        _spawn_task(
            task_id,
            _run_safe(
                project,
                from_stage=ProjectStatus.IMG_GEN,
                until=ProjectStatus.VID_GEN,
            ),
        )
        return {
            "task_id": task_id,
            "status": "img_gen",
            "director_mode_pro": True,
            "candidates_per_scene": n_candidates,
            "updated_scenes": updated_count,
            "total_scenes": len(project.scenes),
        }

    _spawn_task(
        task_id,
        _run_safe(project, from_stage=ProjectStatus.IMG_GEN),
    )

    return {
        "task_id": task_id,
        "status": "img_gen",
        "director_mode_pro": False,
        "updated_scenes": updated_count,
        "total_scenes": len(project.scenes),
    }


# ---------------------------------------------------------------------------
# V9.0 导演模式 Pro:候选选择与局部重试 API
# ---------------------------------------------------------------------------

class CandidateSelection(BaseModel):
    """单个分镜的候选选择。"""

    scene_id: str
    image_index: int = Field(0, ge=0, description="选中的候选图片索引")
    video_index: int = Field(0, ge=0, description="选中的候选视频索引")


class SelectCandidatesRequest(BaseModel):
    """候选选择请求体。"""

    selections: List[CandidateSelection]


@router.post("/{task_id}/select-candidates")
async def select_candidates(task_id: str, body: SelectCandidatesRequest):
    """V9.0 导演模式 Pro:用户选择候选素材后,从阶段④音频合成继续执行。

    请求体 JSON:
        {
            "selections": [
                {"scene_id": "scene_001", "image_index": 0, "video_index": 1},
                {"scene_id": "scene_002", "image_index": 2, "video_index": 0},
                ...
            ]
        }

    前置条件:任务状态为 awaiting_selection。
    执行后:将用户选择的候选设为主素材,从 AUDIO_GEN(或 COMPOSITING)继续。
    """
    project = load_project(task_id)
    if not project:
        raise HTTPException(status_code=404, detail="任务不存在")

    if project.status != ProjectStatus.AWAITING_SELECTION:
        raise HTTPException(
            status_code=400,
            detail=f"任务当前状态为 {project.status.value},非 awaiting_selection,无法选择候选",
        )

    sel_map = {s.scene_id: s for s in body.selections}
    updated_count = 0
    for scene in project.scenes:
        sel = sel_map.get(scene.scene_id)
        if not sel:
            # 未显式选择的分镜默认使用第 0 个候选
            if scene.candidate_images:
                scene.assets.keyframe_image = scene.candidate_images[0]
            if scene.candidate_videos:
                scene.assets.video_clip = scene.candidate_videos[0]
            continue

        # 应用用户选择的图片
        if 0 <= sel.image_index < len(scene.candidate_images):
            scene.assets.keyframe_image = scene.candidate_images[sel.image_index]
        # 应用用户选择的视频
        if 0 <= sel.video_index < len(scene.candidate_videos):
            scene.assets.video_clip = scene.candidate_videos[sel.video_index]
        updated_count += 1

    logger.info(
        "[%s] 用户选择了 %d/%d 个分镜的候选素材,从阶段④继续",
        task_id, updated_count, len(project.scenes),
    )

    # 从 AUDIO_GEN 阶段继续(关闭配音时从 COMPOSITING)
    from_stage = (
        ProjectStatus.AUDIO_GEN
        if project.input.enable_voiceover
        else ProjectStatus.COMPOSITING
    )
    project.status = from_stage
    project.error = None
    project.technical_traceback = None

    _spawn_task(
        task_id,
        _run_safe(project, from_stage=from_stage),
    )

    return {
        "task_id": task_id,
        "status": from_stage.value,
        "updated_scenes": updated_count,
        "total_scenes": len(project.scenes),
    }


class RegenerateAssetRequest(BaseModel):
    """重新生成单个候选素材请求。"""

    scene_id: str
    asset_type: str = Field(..., description="image 或 video")
    candidate_index: int = Field(..., ge=0, description="要重新生成的候选索引")


@router.post("/{task_id}/regenerate-asset")
async def regenerate_asset(task_id: str, body: RegenerateAssetRequest):
    """V9.0 导演模式 Pro:重新生成单个候选图片或视频(局部重试)。

    请求体 JSON:
        {
            "scene_id": "scene_001",
            "asset_type": "image",   // 或 "video"
            "candidate_index": 1
        }

    适用于候选池模式下用户对某个候选不满意,单独重新生成。
    不会推进状态机,仅替换对应候选素材。
    """
    project = load_project(task_id)
    if not project:
        raise HTTPException(status_code=404, detail="任务不存在")

    scene = next(
        (s for s in project.scenes if s.scene_id == body.scene_id), None
    )
    if not scene:
        raise HTTPException(
            status_code=404, detail=f"分镜 {body.scene_id} 不存在"
        )

    if body.asset_type == "image":
        image_gen = ImageGenerator()
        subject_url = project.input.get_image_url(scene.image_index)
        try:
            await image_gen.generate_for_scene(scene, subject_url)
            if not (scene.assets.keyframe_image and scene.assets.keyframe_image.url):
                raise RuntimeError("生图返回空 URL")
            new_img = KeyframeImage(
                url=scene.assets.keyframe_image.url,
                local_path=scene.assets.keyframe_image.local_path,
            )
            if body.candidate_index < len(scene.candidate_images):
                scene.candidate_images[body.candidate_index] = new_img
            else:
                scene.candidate_images.append(new_img)
            # 同步主素材(若替换的是第 0 个候选)
            if body.candidate_index == 0:
                scene.assets.keyframe_image = new_img
            _sync_db_via_helper(project)
            logger.info(
                "[%s] 分镜 %s 候选图片 %d 重新生成成功",
                task_id, body.scene_id, body.candidate_index,
            )
            return {
                "task_id": task_id,
                "scene_id": body.scene_id,
                "asset_type": "image",
                "candidate_index": body.candidate_index,
                "url": new_img.url,
                "status": "regenerated",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"图片重新生成失败: {exc}",
            ) from exc

    elif body.asset_type == "video":
        video_gen = VideoGenerator()
        # 使用对应候选图片作为视频生成输入
        if body.candidate_index < len(scene.candidate_images):
            scene.assets.keyframe_image = scene.candidate_images[body.candidate_index]
        try:
            await video_gen.generate_for_scene(scene)
            if not (scene.assets.video_clip and scene.assets.video_clip.url):
                raise RuntimeError("生视频返回空 URL")
            new_vid = VideoClip(
                url=scene.assets.video_clip.url,
                local_path=scene.assets.video_clip.local_path,
                duration=scene.assets.video_clip.duration,
            )
            if body.candidate_index < len(scene.candidate_videos):
                scene.candidate_videos[body.candidate_index] = new_vid
            else:
                scene.candidate_videos.append(new_vid)
            if body.candidate_index == 0:
                scene.assets.video_clip = new_vid
            _sync_db_via_helper(project)
            logger.info(
                "[%s] 分镜 %s 候选视频 %d 重新生成成功",
                task_id, body.scene_id, body.candidate_index,
            )
            return {
                "task_id": task_id,
                "scene_id": body.scene_id,
                "asset_type": "video",
                "candidate_index": body.candidate_index,
                "url": new_vid.url,
                "status": "regenerated",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"视频重新生成失败: {exc}",
            ) from exc

    raise HTTPException(
        status_code=400, detail="asset_type 必须为 image 或 video"
    )


def _sync_db_via_helper(project: VideoProject) -> None:
    """同步 SQLite 的辅助函数(与 orchestrator 的 _sync_db 逻辑一致)。"""
    try:
        sync_project_from_model(project)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] SQLite 同步失败(非致命): %s", project.project_id, exc)


# ---------------------------------------------------------------------------
# 查询与下载
# ---------------------------------------------------------------------------

@router.post("/{task_id}/retry-composite")
async def retry_composite(task_id: str):
    """V10.1 P0 兜底:独立合成最终视频(仅执行阶段⑤ FFmpeg 合成)。

    适用场景:自动合成失败或卡死,但已生成至少 1 个有效视频片段。
    从 SQLite scenes_data 重建项目,仅跑 COMPOSITING 阶段。
    """
    # 1. 直接从 SQLite 重建项目(唯一事实来源,多 worker 安全)
    db_data = get_project_detail(task_id)
    if not db_data:
        raise HTTPException(status_code=404, detail="任务不存在(数据库无记录)")
    scenes_data = db_data.get("scenes_data") or {}
    try:
        project = VideoProject.model_validate(scenes_data)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"从数据库重建项目失败: {exc}",
        ) from exc
    logger.info("[%s] retry-composite: 从 SQLite 重建项目成功", task_id)

    # 2. 检查有效视频片段
    valid_scenes = [
        s for s in project.scenes
        if s.assets.video_clip and s.assets.video_clip.url
    ]
    if not valid_scenes:
        raise HTTPException(
            status_code=400,
            detail=f"没有可用的视频片段(全部 {len(project.scenes)} 个分镜均无 video_url),无法合成",
        )

    # 3. 重置状态,仅从 COMPOSITING 阶段执行
    project.status = ProjectStatus.COMPOSITING
    project.progress = 0.0
    project.error = None
    project.technical_traceback = None
    sync_project_from_model(project)

    logger.info(
        "[%s] retry-composite: 启动独立合成(%d 个有效分镜)",
        task_id, len(valid_scenes),
    )

    # 4. 后台执行(仅 COMPOSITING 阶段)
    _spawn_task(
        task_id,
        _run_safe(project, from_stage=ProjectStatus.COMPOSITING),
    )

    return {
        "task_id": task_id,
        "status": "compositing",
        "message": f"已启动独立合成,共 {len(valid_scenes)} 个有效分镜",
        "valid_scenes": len(valid_scenes),
    }


# ---------------------------------------------------------------------------
# V14.0 删除任务:级联清理(内存 + SQLite + 本地文件 + OSS)
# ---------------------------------------------------------------------------

@router.delete("/{task_id}")
async def delete_project_task(task_id: str):
    """V14.0: 删除任务,级联清理所有相关资源。

    清理顺序:
        1. 取消正在运行的 asyncio 后台任务
        2. 取消后台任务并从 _task_refs 中移除
        3. 删除 SQLite 记录
        4. 删除本地文件(storage/temp/、storage/outputs/、storage/audios/)
        5. best-effort 删除 OSS 上传的图片(失败仅记日志)
    """
    # 1. 取消后台任务
    bg_task = _task_refs.pop(task_id, None)
    if bg_task and not bg_task.done():
        bg_task.cancel()
        logger.info("[%s] 已取消后台任务", task_id)

    # 2. 从 SQLite 重建项目(用于收集需清理的 OSS/本地 URL),取消后台任务
    project = load_project(task_id)
    oss_urls_to_delete: List[str] = []
    local_paths_to_delete: List[str] = []

    if project:
        # 收集 OSS URL(产品图)
        if project.input.white_image_url:
            oss_urls_to_delete.append(project.input.white_image_url)
        oss_urls_to_delete.extend(project.input.image_urls or [])
        # 收集分镜素材 URL(场景图/视频/音频)
        for scene in project.scenes:
            if scene.assets.keyframe_image.url:
                oss_urls_to_delete.append(scene.assets.keyframe_image.url)
            if scene.assets.video_clip.url:
                oss_urls_to_delete.append(scene.assets.video_clip.url)
        # 收集本地路径
        if project.output.local_path:
            local_paths_to_delete.append(project.output.local_path)
        for scene in project.scenes:
            if scene.assets.video_clip.local_path:
                local_paths_to_delete.append(scene.assets.video_clip.local_path)
            if scene.assets.audio.local_path:
                local_paths_to_delete.append(scene.assets.audio.local_path)

    # 3. 删除 SQLite 记录
    delete_project(task_id)

    # 4. 删除本地临时文件(通配符匹配 task_id)
    storage_root = Path(settings.STORAGE_ROOT)
    cleanup_dirs = ["temp", "outputs", "audios", "uploads"]
    for subdir in cleanup_dirs:
        target_dir = storage_root / subdir
        if target_dir.exists():
            try:
                for f in target_dir.glob(f"{task_id}*"):
                    try:
                        f.unlink()
                        logger.info("[%s] 已删除本地文件: %s", task_id, f)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[%s] 删除文件失败(占用?): %s -> %s", task_id, f, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 扫描目录 %s 失败: %s", task_id, subdir, exc)

    # 删除显式收集的本地路径
    for lp in local_paths_to_delete:
        try:
            p = Path(lp)
            if p.exists():
                p.unlink()
                logger.info("[%s] 已删除输出文件: %s", task_id, p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 删除输出文件失败: %s -> %s", task_id, lp, exc)

    # 5. best-effort 删除 OSS 对象
    for url in oss_urls_to_delete:
        delete_oss_object(url)

    logger.info("[%s] V14.0 删除完成(DB+本地文件+OSS 已级联清理)", task_id)
    return {"task_id": task_id, "deleted": True}


@router.get("/{task_id}")
async def get_project(task_id: str):
    """查询任务进度与结果。

    V9.0:当状态为 awaiting_selection 时,额外返回候选池数据(candidate_images/
    candidate_videos),供前端渲染候选选择界面。
    """
    project = load_project(task_id)
    if not project:
        raise HTTPException(status_code=404, detail="任务不存在")

    # V17.0: 附带创建者标记(全员共享历史下的来源追溯)
    detail = get_project_detail(task_id)
    creator_name = detail.get("creator_name") if detail else None

    status = project.status
    video_url = None
    if status == ProjectStatus.COMPLETED and project.output.local_path:
        video_url = f"/outputs/{Path(project.output.local_path).name}"

    scenes_info = []
    for s in project.scenes:
        info = {
            "scene_id": s.scene_id,
            "status": s.status.value,
            "narration": (s.narration[:60] + "...") if len(s.narration) > 60 else s.narration,
            "hook_text": s.hook_text,
            "image_index": s.image_index,
            "error": s.error,
        }
        # V9.0: 候选池模式下暴露候选素材 URL
        if status == ProjectStatus.AWAITING_SELECTION:
            info["candidate_images"] = [
                {"url": c.url, "index": idx}
                for idx, c in enumerate(s.candidate_images) if c.url
            ]
            info["candidate_videos"] = [
                {"url": c.url, "index": idx, "duration": c.duration}
                for idx, c in enumerate(s.candidate_videos) if c.url
            ]
        scenes_info.append(info)

    # V17.3: 耗时 / ETA 估算
    now = datetime.utcnow()
    start = project.started_at or project.created_at
    elapsed_seconds = None
    if start:
        try:
            sd = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
            if status in (ProjectStatus.COMPLETED, ProjectStatus.FAILED) and project.completed_at:
                cd = project.completed_at if isinstance(project.completed_at, datetime) else datetime.fromisoformat(str(project.completed_at))
                elapsed_seconds = max(0, int((cd - sd).total_seconds()))
            else:
                elapsed_seconds = max(0, int((now - sd).total_seconds()))
        except Exception:
            elapsed_seconds = None
    # 各阶段预估剩余秒数(经验值,仅用于进度页安抚性展示)
    _ETA_MAP = {
        "pending": 30, "scripting": 30, "img_gen": 60,
        "vid_gen": 120, "audio_gen": 30, "compositing": 30,
        "awaiting_selection": 0, "completed": 0, "failed": 0,
    }
    estimated_remaining_seconds = 0 if status in (ProjectStatus.COMPLETED, ProjectStatus.FAILED) else _ETA_MAP.get(status.value, 0)

    logs_payload = [
        (l.model_dump() if hasattr(l, "model_dump") else l) for l in (project.logs or [])
    ]

    return {
        "task_id": task_id,
        "status": status.value,
        "stage_label": STAGE_LABELS.get(status, status.value),
        "progress": project.progress,
        "video_url": video_url,
        "duration_sec": project.output.duration_sec,
        "video_engine": project.output.video_engine,
        "error": project.error,
        "technical_traceback": project.technical_traceback,
        "creator_name": creator_name,
        "created_at": detail.get("created_at") if detail else None,
        "started_at": detail.get("started_at") if detail else None,
        "completed_at": detail.get("completed_at") if detail else None,
        "elapsed_seconds": elapsed_seconds,
        "estimated_remaining_seconds": estimated_remaining_seconds,
        "logs": logs_payload,
        "director_mode_pro": project.config.candidates_per_scene > 1,
        "candidates_per_scene": project.config.candidates_per_scene,
        "scenes": scenes_info,
    }


@router.get("/{task_id}/download")
async def download_video(task_id: str):
    """下载最终 MP4。

    V17.2: 显式 filename=AI_Video_{task_id}.mp4,
    由 FileResponse 注入 Content-Disposition: attachment; filename=...,
    浏览器强制下载(而非直接播放);Content-Type 固定 video/mp4。
    """
    project = load_project(task_id)
    if not project:
        raise HTTPException(status_code=404, detail="任务不存在")
    if project.status != ProjectStatus.COMPLETED or not project.output.local_path:
        raise HTTPException(status_code=409, detail="视频尚未生成完成")
    path = Path(project.output.local_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=f"AI_Video_{task_id}.mp4",
    )
