"""阶段 ①② 测试脚本:DeepSeek 文案分镜 + 通义万相场景图融合。

运行:
    cd ai-video-commerce
    python test_pipeline_step1_2.py

前提:
    1. .env 中配置 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY
       (DASHSCOPE_API_KEY 须为华北2-北京地域的百炼 API Key)
    2. TEST_SUBJECT_IMAGE_URL 为公网可访问的透明 PNG
       (通义万相 background-generation 要求 base_image_url 为透明背景 RGBA PNG)

注意:
    - 若你的产品图是白底图,需先用 utils/matting.py 抠图为透明 PNG,
      并上传到公网(OSS / 图床 / ngrok)后替换下面的 URL。
    - 当前默认用阿里云文档示例的透明 PNG,可验证融合效果跑通。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 将项目根目录加入 sys.path,便于直接运行
sys.path.insert(0, str(Path(__file__).parent))

from app.pipelines.orchestrator import run_pipeline
from app.schemas.project import ProductInput, ProjectStatus, VideoProject

# ---------------------------------------------------------------------------
# 测试输入:高端不锈钢保温杯
# ---------------------------------------------------------------------------

TEST_PRODUCT = ProductInput(
    product_name="高端不锈钢保温杯",
    product_description="316不锈钢内胆,24小时超长保温,商务便携设计,防滑底座",
    selling_points=["24小时保温", "316不锈钢", "商务便携", "防滑底座"],
    target_audience="都市白领、商务人士",
    white_image_url="placeholder",  # 占位,下方用 SUBJECT_IMAGE_URL 覆盖
    duration_target_sec=15,
    style="生活化",
)

# ---------------------------------------------------------------------------
# 主体图 URL(必须是公网可访问的透明背景 PNG)
# ---------------------------------------------------------------------------
# 通义万相 background-generation 接口要求 base_image_url 为透明 RGBA PNG。
# 这里先用阿里云文档示例的透明 PNG 跑通流程,验证融合效果。
# 实际使用时请替换为你的产品透明 PNG 公网 URL。
TEST_SUBJECT_IMAGE_URL = (
    "https://vision-poster.oss-cn-shanghai.aliyuncs.com/lllcho.lc/"
    "data/test_data/images/main_images/new_main_img/a.png"
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 用透明 PNG URL 作为主体图
    TEST_PRODUCT.white_image_url = TEST_SUBJECT_IMAGE_URL

    project = VideoProject(project_id="test-step12", input=TEST_PRODUCT)

    print("\n" + "=" * 60)
    print("阶段 ①② 测试:DeepSeek 文案 + 通义万相图片融合")
    print("=" * 60)

    # 只跑前两个阶段(scripting + img_gen)
    await run_pipeline(project, until=ProjectStatus.IMG_GEN)

    # ----- 打印 DeepSeek 生成的分镜脚本 -----
    print("\n" + "=" * 60)
    print("【阶段① DeepSeek 生成的分镜脚本】")
    print("=" * 60)
    for s in project.scenes:
        print(f"\n--- 分镜 {s.scene_id} (index={s.index}) ---")
        print(f"  旁白 narration:    {s.narration}")
        print(f"  生图 image_prompt: {s.image_prompt}")
        print(f"  运镜 video_prompt: {s.video_prompt}")

    # ----- 打印通义万相生成的场景图 -----
    print("\n" + "=" * 60)
    print("【阶段② 通义万相生成的场景图】")
    print("=" * 60)
    for s in project.scenes:
        print(f"\n分镜 {s.scene_id} [{s.status.value}]:")
        print(f"  场景图 URL: {s.assets.keyframe_image.url}")

    # ----- 结果汇总 -----
    print("\n" + "=" * 60)
    if project.error:
        print(f"❌ 测试失败: {project.error}")
    else:
        print(
            f"✅ 阶段①② 测试完成,status={project.status.value},"
            f"progress={project.progress}"
        )
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
