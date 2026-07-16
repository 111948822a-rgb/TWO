"""全流程测试脚本:5 个阶段端到端跑通,输出最终 MP4。

运行:
    cd ai-video-commerce
    python test_pipeline_full.py

流程:
    阶段① 文案分镜(DeepSeek)        ~5s
    阶段② 场景图片(通义万相)         ~60s
    阶段③ 视频片段(通义万相 wan2.2)  ~4-20min(串行,受接口限流)
    阶段④ 旁白音频(CosyVoice)        ~10s
    阶段⑤ 后期合成(FFmpeg)           ~20-40s
    总计约 5-25 分钟,请耐心等待。

前提:
    1. .env 中配置 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY
    2. TEST_SUBJECT_IMAGE_URL 为公网可访问的透明 PNG
    3. 已安装:ffmpeg-python, imageio-ffmpeg, mutagen, openai, httpx, rembg

输出:
    最终 MP4 保存到 storage/outputs/{project_id}.mp4
    脚本末尾会打印其本地绝对路径。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path,便于直接运行
sys.path.insert(0, str(Path(__file__).parent))

from app.pipelines.orchestrator import run_pipeline
from app.schemas.project import ProductInput, VideoProject

# ---------------------------------------------------------------------------
# 测试输入:高端不锈钢保温杯(与 step1_2 / step3_4 一致,便于对比)
# ---------------------------------------------------------------------------

TEST_PRODUCT = ProductInput(
    product_name="高端不锈钢保温杯",
    product_description="316不锈钢内胆,24小时超长保温,商务便携设计,防滑底座",
    selling_points=["24小时保温", "316不锈钢", "商务便携", "防滑底座"],
    target_audience="都市白领、商务人士",
    white_image_url="placeholder",
    duration_target_sec=15,
    style="生活化",
)

# 主体图 URL(必须是公网可访问的透明背景 PNG)
# 通义万相 background-generation 接口要求 base_image_url 为透明 RGBA PNG。
TEST_SUBJECT_IMAGE_URL = (
    "https://vision-poster.oss-cn-shanghai.aliyuncs.com/lllcho.lc/"
    "data/test_data/images/main_images/new_main_img/a.png"
)


def _hr(title: str) -> str:
    """生成分隔线标题。"""
    return "\n" + "=" * 64 + f"\n{title}\n" + "=" * 64


def _file_size_mb(path: str) -> str:
    """返回文件大小(MB)。"""
    try:
        size = os.path.getsize(path)
        return f"{size / 1024 / 1024:.2f} MB"
    except OSError:
        return "(未知)"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    TEST_PRODUCT.white_image_url = TEST_SUBJECT_IMAGE_URL
    project = VideoProject(project_id="test-full", input=TEST_PRODUCT)

    print(_hr("全流程测试:文案 → 图片 → 视频 → 音频 → 合成"))
    print("⚠️  视频生成耗时较长(4-20 分钟),请耐心等待...")
    print(f"项目 ID: {project.project_id}")

    # 跑完整 5 个阶段(不传 until,跑到底)
    await run_pipeline(project)

    # ----- 阶段① DeepSeek 分镜脚本 -----
    print(_hr("【阶段① DeepSeek 生成的分镜脚本】"))
    for s in project.scenes:
        print(f"\n--- 分镜 {s.scene_id} (index={s.index}) ---")
        print(f"  旁白 narration:    {s.narration}")
        print(f"  生图 image_prompt: {s.image_prompt}")
        print(f"  运镜 video_prompt: {s.video_prompt}")

    # ----- 阶段② 通义万相场景图 -----
    print(_hr("【阶段② 通义万相生成的场景图】"))
    for s in project.scenes:
        print(f"  分镜 {s.scene_id}: {s.assets.keyframe_image.url}")

    # ----- 阶段③ 通义万相视频 -----
    print(_hr("【阶段③ 通义万相生成的视频片段】"))
    for s in project.scenes:
        clip = s.assets.video_clip
        print(f"  分镜 {s.scene_id}: 时长 {clip.duration}s | URL: {clip.url}")

    # ----- 阶段④ CosyVoice 音频 -----
    print(_hr("【阶段④ CosyVoice 生成的旁白音频】"))
    total_duration = 0.0
    for s in project.scenes:
        audio = s.assets.audio
        dur = audio.duration or 0
        total_duration += dur
        print(f"  分镜 {s.scene_id}: {dur:.3f}s | {audio.local_path}")
    print(f"  >>> 旁白总时长: {total_duration:.3f}s")

    # ----- 阶段⑤ FFmpeg 合成结果 -----
    print(_hr("【阶段⑤ FFmpeg 最终合成结果】"))
    output = project.output
    if output.local_path and Path(output.local_path).exists():
        abs_path = str(Path(output.local_path).resolve())
        print(f"  ✅ 最终视频已生成")
        print(f"  本地绝对路径: {abs_path}")
        print(f"  文件大小: {_file_size_mb(output.local_path)}")
        print(f"  视频时长: {output.duration_sec:.2f}s")
        print(f"  字幕文件: {output.subtitle_url}")
        print(f"  分镜数:   {len(project.scenes)}")
        print(f"  转场次数: {max(0, len(project.scenes) - 1)} (xfade fade 0.4s)")
    else:
        print(f"  ❌ 最终视频未生成: {output.local_path}")

    # ----- 结果汇总 -----
    print(_hr("测试结果汇总"))
    if project.error:
        print(f"❌ 全流程测试失败: {project.error}")
        print(f"   最终状态: {project.status.value}")
    else:
        print(f"✅ 全流程测试成功完成!")
        print(f"   状态: {project.status.value}")
        print(f"   进度: {project.progress}")
        if output.local_path:
            print(f"   🎬 最终视频: {Path(output.local_path).resolve()}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
