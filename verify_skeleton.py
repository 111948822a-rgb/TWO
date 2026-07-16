"""骨架验证脚本:构造一个 VideoProject,运行 run_pipeline,确认状态机能跑通。

运行:
    cd ai-video-commerce
    python verify_skeleton.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 将项目根目录加入 sys.path,便于直接运行
sys.path.insert(0, str(Path(__file__).parent))

from app.schemas.project import ProductInput, ProjectStatus, VideoProject
from app.pipelines.orchestrator import run_pipeline


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    project = VideoProject(
        project_id="test-001",
        input=ProductInput(
            product_name="无线降噪耳机 X1",
            product_description="主动降噪,30 小时续航",
            selling_points=["降噪强", "续航久", "佩戴舒适"],
            white_image_url="https://example.com/product_white.jpg",
            duration_target_sec=15,
        ),
    )

    result = asyncio.run(run_pipeline(project))

    print("\n" + "=" * 60)
    print("Pipeline 执行结果:")
    print(f"  最终状态: {result.status.value}")
    print(f"  进度:     {result.progress}")
    print(f"  分镜数:   {len(result.scenes)}")
    print(f"  总时长:   {result.output.duration_sec}s")
    print(f"  输出路径: {result.output.local_path}")
    print(f"  错误:     {result.error}")
    print("=" * 60)

    # 断言验证
    assert result.status == ProjectStatus.COMPLETED, (
        f"预期 COMPLETED,实际 {result.status}"
    )
    assert result.progress == 1.0
    assert len(result.scenes) == 3
    assert abs(result.output.duration_sec - 11.4) < 0.01  # 3 * 3.8,容浮点误差

    print("\n✅ 骨架验证通过!状态机流转正常。")
    print("\n各分镜最终状态:")
    for s in result.scenes:
        print(
            f"  {s.scene_id}: {s.status.value} | "
            f"img={s.assets.keyframe_image.local_path} | "
            f"vid={s.assets.video_clip.local_path}({s.assets.video_clip.duration}s) | "
            f"audio={s.assets.audio.local_path}({s.assets.audio.duration}s)"
        )


if __name__ == "__main__":
    main()
