"""阶段 ①②③④ 测试脚本:DeepSeek 文案 + 通义万相图片 + 通义万相视频 + CosyVoice 音频。

运行:
    cd ai-video-commerce
    python test_pipeline_step3_4.py

前提:
    1. .env 中配置 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY
       (DASHSCOPE_API_KEY 须为华北2-北京地域的百炼 API Key)
    2. TEST_SUBJECT_IMAGE_URL 为公网可访问的透明 PNG
    3. 已安装 mutagen(pip install mutagen)用于读取音频时长

耗时说明:
    - 阶段① 文案:约 5 秒
    - 阶段② 图片:约 1 分钟(4 张,每张约 13 秒)
    - 阶段③ 视频:约 4-20 分钟(4 段,每段 1-5 分钟,串行执行)
    - 阶段④ 音频:约 10 秒(4 段,并发执行)
    总计约 5-25 分钟,请耐心等待。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.pipelines.orchestrator import run_pipeline
from app.schemas.project import ProductInput, ProjectStatus, VideoProject

# ---------------------------------------------------------------------------
# 测试输入:高端不锈钢保温杯(与 step1_2 一致)
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

TEST_SUBJECT_IMAGE_URL = (
    "https://vision-poster.oss-cn-shanghai.aliyuncs.com/lllcho.lc/"
    "data/test_data/images/main_images/new_main_img/a.png"
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    TEST_PRODUCT.white_image_url = TEST_SUBJECT_IMAGE_URL
    project = VideoProject(project_id="test-step1234", input=TEST_PRODUCT)

    print("\n" + "=" * 60)
    print("阶段 ①②③④ 测试:文案 + 图片 + 视频 + 音频")
    print("=" * 60)
    print("⚠️  视频生成耗时较长(4-20 分钟),请耐心等待...")

    # 跑前四个阶段(scripting + img_gen + vid_gen + audio_gen)
    await run_pipeline(project, until=ProjectStatus.AUDIO_GEN)

    # ----- 阶段① DeepSeek 分镜脚本 -----
    print("\n" + "=" * 60)
    print("【阶段① DeepSeek 生成的分镜脚本】")
    print("=" * 60)
    for s in project.scenes:
        print(f"\n--- 分镜 {s.scene_id} (index={s.index}) ---")
        print(f"  旁白 narration:    {s.narration}")
        print(f"  生图 image_prompt: {s.image_prompt}")
        print(f"  运镜 video_prompt: {s.video_prompt}")

    # ----- 阶段② 通义万相场景图 -----
    print("\n" + "=" * 60)
    print("【阶段② 通义万相生成的场景图】")
    print("=" * 60)
    for s in project.scenes:
        print(f"\n分镜 {s.scene_id} [{s.assets.keyframe_image.url and '已生成' or '缺失'}]")
        print(f"  场景图 URL: {s.assets.keyframe_image.url}")

    # ----- 阶段③ 通义万相视频 -----
    print("\n" + "=" * 60)
    print("【阶段③ 通义万相生成的视频片段】")
    print("=" * 60)
    for s in project.scenes:
        clip = s.assets.video_clip
        print(f"\n分镜 {s.scene_id} [{s.status.value}]:")
        print(f"  视频 URL: {clip.url or '(未生成)'}")
        print(f"  视频时长: {clip.duration}s" if clip.duration else "  视频时长: (未知)")

    # ----- 阶段④ CosyVoice 音频 -----
    print("\n" + "=" * 60)
    print("【阶段④ CosyVoice 生成的旁白音频】")
    print("=" * 60)
    total_duration = 0.0
    for s in project.scenes:
        audio = s.assets.audio
        dur = audio.duration or 0
        total_duration += dur
        print(f"\n分镜 {s.scene_id}:")
        print(f"  本地路径: {audio.local_path or '(未生成)'}")
        print(f"  精确时长: {dur:.3f}s")
    print(f"\n  >>> 旁白总时长: {total_duration:.3f}s")

    # ----- 结果汇总 -----
    print("\n" + "=" * 60)
    if project.error:
        print(f"❌ 测试失败: {project.error}")
    else:
        print(
            f"✅ 阶段①②③④ 测试完成,status={project.status.value},"
            f"progress={project.progress}"
        )
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
