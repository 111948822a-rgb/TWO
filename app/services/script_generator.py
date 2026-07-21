"""文案与分镜生成服务。

调用 DeepSeek LLM,根据产品信息生成结构化分镜脚本。
强 System Prompt(见 utils/prompt_templates.py)确保:
    - 严格 JSON 输出(配合 response_format=json_object)
    - image_prompt 含 5 维度静态视觉细节,严禁"白色背景/抠图"
    - video_prompt 含专业运镜词 + 精确运镜轨迹,严禁"图片轮播/平移"

V16.1 紧急修复:
    - 废除 150+ 词超长限制,改为 70-120 词黄金甜点区
    - 视觉与动态分离:image_prompt 侧重静态,video_prompt 侧重动态
    - 鲁棒 JSON 解析:正则提取 + 清理 markdown + 容错
    - 扩写阈值从 <100 改为 <70(目标 70-120 词)
    - 字数兜底扩写:通过 asyncio.gather 并发,不阻塞前端
    - 日志关键字:[Prompt Engine] 分镜 X 原始词数: Y, 扩写后词数: Z
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from app.providers.llm.base import ILLMProvider
from app.providers.llm.deepseek import DeepSeekProvider
from app.schemas.project import Scene, VideoProject
from app.utils.prompt_templates import (
    EXPAND_PROMPT_SYSTEM,
    EXPAND_PROMPT_USER_TEMPLATE,
    build_script_system_prompt,
    build_script_user_prompt,
)

logger = logging.getLogger(__name__)

# V16.1:词数阈值。低于此值触发 LLM 自动扩写(目标 70-120 词甜点区)
_MIN_PROMPT_WORD_COUNT = 70


def _robust_json_parse(raw: str, project_id: str = "") -> dict:
    """V16.1 鲁棒 JSON 解析:清理 markdown 标记 + 正则提取 + 容错。

    LLM 在生成较长文本时极易破坏 JSON 格式,此函数通过以下步骤增强鲁棒性:
    1. 清理首尾的 markdown 代码块标记(如 ```json ... ```)
    2. 尝试直接 json.loads
    3. 失败则用正则 r'\\{.*\\}' 提取最外层 JSON 对象
    4. 依然失败则清理未转义换行符后重试
    5. 全部失败则打印原始脏数据并抛出
    """
    text = raw.strip()

    # 步骤1:清理 markdown 代码块标记
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text)
        text = re.sub(r'\n?```\s*$', '', text)

    # 步骤2:尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 步骤3:正则提取最外层 JSON 对象
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 步骤4:清理未转义换行符后重试
            cleaned = candidate.replace('\n', ' ').replace('\r', ' ')
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass

    # 步骤5:全部失败,打印脏数据
    logger.error(
        "[%s] JSON 解析失败,原始脏数据(前500字): %s",
        project_id, raw[:500],
    )
    raise json.JSONDecodeError(
        "JSON 解析失败(已尝试正则提取+清理换行)", raw, 0
    )


async def expand_prompt_if_short(
    provider: ILLMProvider,
    prompt: str,
    prompt_type: str,
    scene_index: int,
    project_id: str,
) -> str:
    """V16.1 字数兜底:如果 prompt 英文词数 < 70,自动调用 LLM 扩写到 70-120 词。

    Args:
        provider: LLM Provider(复用 DeepSeekProvider,扩写是纯文本任务)。
        prompt: 原 prompt 字符串。
        prompt_type: "image" 或 "video",用于日志和扩写方向。
        scene_index: 分镜序号,用于日志。
        project_id: 项目 ID,用于日志。

    Returns:
        扩写后的 prompt(若原始 ≥70 词则原样返回)。
    """
    word_count = len(prompt.split())
    if word_count >= _MIN_PROMPT_WORD_COUNT:
        logger.info(
            "[%s] [Prompt Engine] 分镜 %d %s_prompt 原始词数: %d (≥%d,无需扩写)",
            project_id, scene_index, prompt_type, word_count, _MIN_PROMPT_WORD_COUNT,
        )
        return prompt

    logger.info(
        "[%s] [Prompt Engine] 分镜 %d %s_prompt 原始词数: %d (<%d,触发 LLM 扩写)",
        project_id, scene_index, prompt_type, word_count, _MIN_PROMPT_WORD_COUNT,
    )

    video_clause = (
        ", physical dynamic elements (dust, steam, particles), and precise camera trajectory "
        "(arc shot, dolly-in, crane shot with angles and speed)"
        if prompt_type == "video"
        else ""
    )
    user_prompt = EXPAND_PROMPT_USER_TEMPLATE.format(
        prompt_type=prompt_type,
        word_count=word_count,
        original_prompt=prompt,
        video_clause=video_clause,
    )

    try:
        raw = await provider.chat_json(EXPAND_PROMPT_SYSTEM, user_prompt)
        # V16.1:扩写结果也用鲁棒解析
        data = _robust_json_parse(raw, project_id)
        expanded = data.get("expanded_prompt", "").strip()
        if expanded:
            new_count = len(expanded.split())
            logger.info(
                "[%s] [Prompt Engine] 分镜 %d %s_prompt 扩写后词数: %d (原 %d → 新 %d)",
                project_id, scene_index, prompt_type, new_count, word_count, new_count,
            )
            return expanded
        logger.warning(
            "[%s] [Prompt Engine] 分镜 %d %s_prompt 扩写返回空,保留原 prompt",
            project_id, scene_index, prompt_type,
        )
        return prompt
    except Exception as exc:
        logger.warning(
            "[%s] [Prompt Engine] 分镜 %d %s_prompt 扩写失败: %s,保留原 prompt",
            project_id, scene_index, prompt_type, exc,
        )
        return prompt


class ScriptGenerator:
    """文案分镜生成器。"""

    def __init__(self, provider: ILLMProvider | None = None) -> None:
        self.provider = provider or DeepSeekProvider()

    async def generate(self, project: VideoProject) -> None:
        """根据 project.input 生成分镜,填充 project.scenes。

        V16.1:生成后对所有分镜的 image_prompt / video_prompt 做字数兜底扩写。
        """
        logger.info(
            "[%s] 调用 LLM 生成分镜脚本", project.project_id
        )
        image_count = (
            len(project.input.image_urls) if project.input.image_urls else 1
        )
        # V18.0 Pacing Engine:节奏时间轴作为最高优先级硬约束注入 System Prompt
        rhythm_rules = project.input.rhythm_rules or []
        if rhythm_rules:
            logger.info(
                "[%s] [Pacing Engine] 启用节奏硬约束:%d 个阶段,总时长 %.1fs → %s",
                project.project_id,
                len(rhythm_rules),
                sum(float(s.duration or (s.end_time - s.start_time)) for s in rhythm_rules),
                " | ".join(f"{s.stage_name}({s.duration:.1f}s)" for s in rhythm_rules),
            )
        system_prompt = build_script_system_prompt(
            image_count=image_count,
            language=project.input.language,
            visual_style=project.input.visual_style,
            product_material=project.input.product_material,
            rhythm_rules=rhythm_rules,
        )
        user_prompt = build_script_user_prompt(project.input)

        raw = await self.provider.chat_json(system_prompt, user_prompt)

        # V16.1:鲁棒 JSON 解析(正则提取+清理 markdown+容错)
        data = _robust_json_parse(raw, project.project_id)

        scenes: list[Scene] = []
        for item in data.get("scenes", []):
            # image_index 兜底:LLM 未给或越界时回退 0
            raw_idx = item.get("image_index", 0)
            try:
                img_idx = int(raw_idx)
            except (TypeError, ValueError):
                img_idx = 0
            if img_idx < 0 or (image_count > 1 and img_idx >= image_count):
                img_idx = 0

            scene_index = item["index"]
            # V18.0 Pacing Engine:解析 stage_name / target_duration
            #   优先用 LLM 返回值;缺失或非法时回退到 rhythm_rules 对应 index 的时间轴
            fallback = (
                rhythm_rules[scene_index]
                if rhythm_rules and 0 <= scene_index < len(rhythm_rules)
                else None
            )
            stage_name = str(item.get("stage_name") or (fallback.stage_name if fallback else ""))
            try:
                target_duration = float(item.get("target_duration"))
            except (TypeError, ValueError):
                target_duration = 0.0
            if target_duration <= 0 and fallback is not None:
                target_duration = float(
                    fallback.duration or (fallback.end_time - fallback.start_time)
                )

            scenes.append(
                Scene(
                    scene_id=f"scene_{scene_index + 1:03d}",
                    index=scene_index,
                    narration=item.get("narration", ""),
                    image_prompt=item.get("image_prompt", ""),
                    video_prompt=item.get("video_prompt", ""),
                    hook_text=item.get("hook_text") or None,
                    image_index=img_idx,
                    stage_name=stage_name,
                    target_duration=target_duration,
                )
            )

        if not scenes:
            raise RuntimeError("LLM 未生成任何分镜")

        # V18.0 Pacing Engine:若 LLM 未按时间轴数量生成,强制按 rhythm_rules 补齐/对齐
        #   保证每个分镜都有可执行的 target_duration(否则 FFmpeg 无法精准卡点)
        if rhythm_rules:
            for scene in scenes:
                if 0 <= scene.index < len(rhythm_rules):
                    rule = rhythm_rules[scene.index]
                    if not scene.stage_name:
                        scene.stage_name = rule.stage_name
                    if scene.target_duration <= 0:
                        scene.target_duration = float(
                            rule.duration or (rule.end_time - rule.start_time)
                        )
            aligned = ", ".join(
                f"[{s.index}]{s.stage_name}={s.target_duration:.1f}s" for s in scenes
            )
            logger.info(
                "[%s] [Pacing Engine] 分镜节奏对齐结果(%d 段): %s",
                project.project_id, len(scenes), aligned,
            )

        # V16.1:字数兜底,并发扩写所有分镜的 image_prompt 和 video_prompt
        # asyncio.gather 并发执行,总耗时 ≈ 单次扩写耗时(而非 N×单次),不阻塞前端
        expand_tasks: list[asyncio.Task] = []
        for scene in scenes:
            expand_tasks.append(
                asyncio.create_task(
                    self._expand_scene_field(scene, "image_prompt", "image", project.project_id)
                )
            )
            expand_tasks.append(
                asyncio.create_task(
                    self._expand_scene_field(scene, "video_prompt", "video", project.project_id)
                )
            )
        if expand_tasks:
            await asyncio.gather(*expand_tasks)

        project.scenes = scenes
        logger.info(
            "[%s] 生成 %d 个分镜 (产品图 %d 张,已分配 image_index,V16.1 扩写已完成)",
            project.project_id, len(scenes), image_count,
        )

    async def _expand_scene_field(
        self, scene: Scene, field: str, prompt_type: str, project_id: str
    ) -> None:
        """对单个 scene 的单个字段做字数兜底扩写(就地修改)。"""
        original = getattr(scene, field) or ""
        expanded = await expand_prompt_if_short(
            self.provider, original, prompt_type, scene.index, project_id
        )
        setattr(scene, field, expanded)
