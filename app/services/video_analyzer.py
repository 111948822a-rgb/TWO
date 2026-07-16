"""V15.0 视频模仿(拍同款)— 多模态视频分析服务。

使用阿里云百炼 Qwen-VL-Max 分析参考视频,提取分镜结构:
    参考视频 MP4 URL → Qwen-VL → JSON(分镜列表)→ Scene 对象

分析维度:
    1. 画面构图与内容描述(visual_description → image_prompt)
    2. 专业运镜提示词(camera_movement → video_prompt)
    3. 镜头时长(duration_seconds)
    4. 营销旁白(narration,基于用户产品信息生成)

设计要点:
    - base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    - model: qwen-vl-max-latest
    - 视频通过 OpenAI 兼容接口的 video_url content type 传入
    - 强制 JSON 输出(response_format=json_object)
"""

from __future__ import annotations

import asyncio
import json
import logging

from openai import AsyncOpenAI

from app.core.config import settings
from app.providers.llm.deepseek import DeepSeekProvider
from app.schemas.project import Scene, VideoProject
from app.services.script_generator import expand_prompt_if_short
from app.utils.prompt_templates import (
    LANGUAGE_NAMES,
    MATERIAL_LIGHTING_PROMPTS,
    VISUAL_STYLE_PROMPTS,
    VIDEO_QUALITY_SUFFIX,
)

logger = logging.getLogger(__name__)

_DASHSCOPE_COMPATIBLE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_VL_MODEL = "qwen-vl-max-latest"

SYSTEM_PROMPT = """你是一位顶级的短视频导演和分镜师。请仔细观看这段参考视频,深度拆解它的视听语言。
我需要你提取出每个镜头的核心要素,以便我用其他产品重新拍摄一遍。

请严格以 JSON 格式输出,结构如下:
{
  "scenes": [
    {
      "scene_index": 镜头序号(整数,从0开始),
      "visual_description": "画面构图与内容描述(不要提原视频的具体产品,只描述场景、光影、物体位置关系,例如:'特写镜头,产品放置在木质桌面中央,侧逆光,背景虚化')",
      "camera_movement": "专业的英文运镜提示词(如:smooth tracking shot following the subject, FPV drone fast fly-through, dolly zoom vertigo effect, crane shot revealing environment)",
      "duration_seconds": 该镜头的预估时长(浮点数,如2.5),
      "narration": "营销旁白文案,用目标语言撰写,突出产品卖点"
    }
  ]
}

关键要求:
1. visual_description 必须用英文(English)描述,只描述场景/光影/构图/物体位置,绝对不提原视频中的具体产品
2. camera_movement 必须用英文(English),使用专业电影运镜术语,每次选择不同的运镜方式
3. narration 必须用目标语言撰写,是针对用户产品的营销文案
4. 每个镜头的 duration_seconds 之和应接近参考视频的总时长
5. 通常提取 3-6 个镜头
6. 不要输出任何额外文字、不要 markdown 代码块标记
"""

USER_PROMPT_TEMPLATE = """请分析这段参考视频,并为我的产品提取分镜方案。

我的产品信息:
- 产品名称:{product_name}
- 产品描述:{product_description}
- 核心卖点:{selling_points}
- 目标语言:{lang_en}({lang_zh})
- 视频氛围:{vibe}
- 产品材质:{product_material}
- 视觉风格:{visual_style}

请基于参考视频的分镜结构、运镜和节奏,为我的产品生成一套高度相似的拍摄方案。
narration(旁白)必须用{lang_en}撰写,visual_description 和 camera_movement 必须用英文(English)撰写。"""


class VideoAnalyzer:
    """多模态视频分析器(基于 Qwen-VL-Max)。"""

    def __init__(self) -> None:
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError(
                "未配置 DASHSCOPE_API_KEY,请在 .env 中设置"
            )
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=_DASHSCOPE_COMPATIBLE_BASE,
        )
        self.model = _VL_MODEL

    async def analyze(self, project: VideoProject) -> None:
        """分析参考视频,提取分镜,填充 project.scenes。

        替代 ScriptGenerator.generate() 的功能,但分镜来源从 LLM 生成变为
        Qwen-VL 视频分析。
        """
        video_url = project.input.reference_video_url
        if not video_url:
            raise RuntimeError("拍同款模式缺少 reference_video_url")

        lang_en, lang_zh = LANGUAGE_NAMES.get(
            project.input.language, ("English", "英语")
        )
        selling_points = "、".join(
            project.input.selling_points
        ) if project.input.selling_points else "无"

        user_prompt = USER_PROMPT_TEMPLATE.format(
            product_name=project.input.product_name,
            product_description=project.input.product_description,
            selling_points=selling_points,
            lang_en=lang_en,
            lang_zh=lang_zh,
            vibe=project.input.vibe,
            product_material=project.input.product_material,
            visual_style=project.input.visual_style,
        )

        logger.info(
            "[%s] [VideoAnalyzer] 调用 Qwen-VL-Max 分析视频: %s",
            project.project_id,
            video_url,
        )

        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {"url": video_url},
                        },
                        {
                            "type": "text",
                            "text": user_prompt,
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=3000,
        )

        raw = resp.choices[0].message.content or ""
        logger.info(
            "[%s] [VideoAnalyzer] Qwen-VL 返回 %d 字符",
            project.project_id,
            len(raw),
        )
        logger.info(
            "[%s] [VideoAnalyzer] 原始返回(前500字): %s",
            project.project_id,
            raw[:500],
        )

        data = json.loads(raw)
        raw_scenes = data.get("scenes", [])
        if not raw_scenes:
            raise RuntimeError("Qwen-VL 未分析出任何分镜")

        # 获取材质光影和视觉风格描述词
        material_desc = MATERIAL_LIGHTING_PROMPTS.get(
            project.input.product_material,
            MATERIAL_LIGHTING_PROMPTS["other"],
        )
        style_desc = VISUAL_STYLE_PROMPTS.get(
            project.input.visual_style,
            VISUAL_STYLE_PROMPTS["photorealistic"],
        )

        image_count = (
            len(project.input.image_urls)
            if project.input.image_urls
            else 1
        )

        scenes: list[Scene] = []
        for item in raw_scenes:
            idx = item.get("scene_index", len(scenes))

            # 构建完整的 image_prompt:视觉描述 + 材质光影 + 视觉风格
            visual_desc = item.get("visual_description", "")
            image_prompt = (
                f"{visual_desc}, {material_desc}, {style_desc}"
                if visual_desc
                else f"{material_desc}, {style_desc}"
            )

            # 构建完整的 video_prompt:运镜 + 画质后缀
            camera_move = item.get("camera_movement", "")
            video_prompt = (
                f"{camera_move}, {VIDEO_QUALITY_SUFFIX}"
                if camera_move
                else VIDEO_QUALITY_SUFFIX
            )

            narration = item.get("narration", "")

            scenes.append(
                Scene(
                    scene_id=f"scene_{idx + 1:03d}",
                    index=idx,
                    narration=narration,
                    image_prompt=image_prompt,
                    video_prompt=video_prompt,
                    hook_text=narration[:30] if idx == 0 and narration else None,
                    image_index=idx % image_count if image_count > 0 else 0,
                )
            )

        # V16.0 模块2:字数兜底,并发扩写所有分镜的 image_prompt 和 video_prompt
        # 拍同款路径用 DeepSeek 做扩写(纯文本任务,便宜且快)
        expand_provider = DeepSeekProvider()
        expand_tasks: list[asyncio.Task] = []
        for scene in scenes:
            expand_tasks.append(
                asyncio.create_task(
                    self._expand_scene_field(scene, "image_prompt", "image", project.project_id, expand_provider)
                )
            )
            expand_tasks.append(
                asyncio.create_task(
                    self._expand_scene_field(scene, "video_prompt", "video", project.project_id, expand_provider)
                )
            )
        if expand_tasks:
            await asyncio.gather(*expand_tasks)

        project.scenes = scenes
        logger.info(
            "[%s] [VideoAnalyzer] 分析完成,提取 %d 个分镜",
            project.project_id,
            len(scenes),
        )
        for s in scenes:
            logger.info(
                "  [%s] 运镜=%s, 时长≈%ss",
                s.scene_id,
                s.video_prompt[:50],
                raw_scenes[s.index].get("duration_seconds", "?"),
            )

    async def _expand_scene_field(
        self, scene: Scene, field: str, prompt_type: str, project_id: str, provider
    ) -> None:
        """V16.0 对单个 scene 的单个字段做字数兜底扩写(就地修改)。"""
        original = getattr(scene, field) or ""
        expanded = await expand_prompt_if_short(
            provider, original, prompt_type, scene.index, project_id
        )
        setattr(scene, field, expanded)
