"""Prompt 模板:LLM System / User Prompt 构建。

V16.1 紧急修复:废除 150+ 词超长限制(导致 API Token 超限崩溃),
改为 70-120 词黄金甜点区,并强制视觉与动态分离:
    - image_prompt(生图):侧重静态视觉(材质/环境/光影/镜头),禁止复杂运动
    - video_prompt(生视频):侧重物理动态与运镜,不重复静态材质描述

核心目标:通过强 Prompt 工程规避两大历史坑:
    1. 图片只换背景 -> image_prompt 强制场景环境/光线/构图,
       严禁"白色背景/抠图"等字眼。
    2. 视频像 PPT 轮播 -> video_prompt 强制精确运镜轨迹 + 物理动态元素,
       严禁"图片轮播/平移/静态"等字眼。
"""

from __future__ import annotations

from typing import List, Optional

from app.schemas.project import ProductInput, RhythmStage

# V4.0 市场聚焦:语言代码 → (英文名, 中文名) 供 Prompt 注入(仅 English/Thai/Indonesian)
LANGUAGE_NAMES: dict[str, tuple[str, str]] = {
    "en": ("English", "英语"),
    "th": ("Thai", "泰语"),
    "id": ("Indonesian", "印尼语"),
}

# V6.0 视觉风格 → 英文描述词(注入 image_prompt / video_prompt,控制画面风格)
VISUAL_STYLE_PROMPTS: dict[str, str] = {
    "photorealistic": "photorealistic, real photography, DSLR quality, natural lighting, high detail",
    "3d_render": "3D render, Unreal Engine 5, octane render, clay material, soft studio lighting, product design",
    "anime": "anime style, Studio Ghibli style, 2D animation, cel shading, vibrant colors, hand-drawn",
    "cyberpunk": "cyberpunk style, neon lights, futuristic, blade runner aesthetic, dark moody atmosphere, holographic",
}

# V14.0 产品材质 → 专业摄影光影英文术语(注入 image_prompt,消灭贴图感)
MATERIAL_LIGHTING_PROMPTS: dict[str, str] = {
    "glass": "caustics, light refraction, transparent shadows, environmental reflections on glass surface, specular highlights through transparent material",
    "metal": "sharp specular highlights, environmental color reflection on metallic surface, high contrast metallic shading, chrome reflective finish",
    "plastic": "soft diffused lighting, subtle ambient occlusion, soft drop shadows, matte plastic surface with gentle highlights",
    "fabric": "subsurface scattering on fabric, realistic cloth folds interacting with light, fabric texture with soft shadow gradients",
    "electronics": "screen glow illumination, LED light bleed onto surrounding surfaces, glossy screen reflection, ambient light from display",
    "other": "professional product photography lighting, soft studio lights with gentle reflections, balanced highlight and shadow",
}

# V14.2 电影级运镜词汇库(注入 video_prompt 要求,激发模型张力)
CINEMATIC_CAMERA_MOVES: list[str] = [
    "FPV drone fast fly-through",
    "dolly zoom (vertigo effect)",
    "smooth tracking shot following the subject",
    "crane shot revealing the environment",
    "slow motion (120fps) capturing subtle details",
    "dynamic handheld camera movement",
]

# V14.2 画质与光影增强后缀(由 HappyHorse Provider 在发送前强制追加到 video_prompt 末尾)
VIDEO_QUALITY_SUFFIX: str = (
    "cinematic lighting, highly detailed, 8k resolution, photorealistic, "
    "depth of field, anamorphic lens flare, smooth motion"
)

# ===========================================================================
# V21.0 防形变与物理规律"防御性咒语"(强制追加到每条最终 video_prompt 末尾)
#   核心目标:防止 AI 生成过程中产品形变(键盘画成鼠标)、融化、多指、
#   违背物理规律的光影 —— 这是"产品灵魂注入与防形变"改造的最后一道防线。
# ===========================================================================
ANTI_DEFORMATION_SUFFIX: str = (
    "maintain exact product shape and proportions, strict object permanence, "
    "no morphing, no melting, no extra fingers, realistic human hand interaction, "
    "physically accurate lighting and shadows, photorealistic 8k commercial footage"
)


def compose_final_video_prompt(video_prompt: str) -> str:
    """V21.0 拼装最终传给 HappyHorse 的 video_prompt(单一出口)。

    强制规则:
      1. 末尾必须追加 VIDEO_QUALITY_SUFFIX(画质增强)+ ANTI_DEFORMATION_SUFFIX
         (防形变咒语),且咒语**绝不允许被截断丢失**。
      2. 总词数控制在 ≤150(避免 API Token 超限):先截断正文,再拼后缀,
         而不是拼完后缀再截断(旧 truncate_prompt_safe 会把咒语砍掉)。

    Args:
        video_prompt: LLM 生成/用户编辑后的运镜指令正文。

    Returns:
        末尾带防形变咒语的最终 prompt(总词数 ≤150)。
    """
    body = (video_prompt or "").strip().rstrip(",.;")
    suffix = f"{VIDEO_QUALITY_SUFFIX}, {ANTI_DEFORMATION_SUFFIX}"
    suffix_words = len(suffix.split())
    max_body_words = max(30, 150 - suffix_words - 1)
    body_words = body.split()
    if len(body_words) > max_body_words:
        body = " ".join(body_words[:max_body_words])
    return f"{body}, {suffix}"

# V14.2 禁止的保守运镜词汇(会导致画面像 PPT)
FORBIDDEN_CONSERVATIVE_MOVES: list[str] = [
    "static shot",
    "slow pan",
    "minimal movement",
    "still camera",
    "no movement",
    "fixed camera",
]

# ===========================================================================
# V16.1 5 维度细节展开法 — 专业摄影词汇库(Few-Shot 喂给 LLM,70-120 词甜点区)
# ===========================================================================

# 维度3:Lighting Design — 专业灯光术语
CINEMATIC_LIGHTING_TERMS: list[str] = [
    "warm 3200K key light from top-left",
    "crisp cool rim light separating subject from background",
    "soft ambient occlusion in crevices",
    "hard directional sunlight casting long shadows",
    "volumetric god rays piercing through haze",
    "gobo-patterned shadows cast by foliage",
    "bounce fill from white card softening shadows",
]

# 维度4:Camera & Lens Specs — 摄影机与镜头参数
CINEMATIC_CAMERA_SPECS: list[str] = [
    "shot on ARRI Alexa 65",
    "85mm prime lens",
    "f/1.8 aperture for shallow depth of field",
    "anamorphic lens flares",
    "subtle film grain",
]

# 维度5:Color Grading & Mood — 调色风格
COLOR_GRADING_STYLES: list[str] = [
    "teal and orange cinematic color grading",
    "high contrast moody atmosphere",
    "warm golden hour tones",
    "cool desaturated noir aesthetic",
    "vibrant saturated commercial look",
]

# V16.1 模块3 — video_prompt 物理动态元素(强制注入,消灭静帧感)
VIDEO_DYNAMIC_ELEMENTS: list[str] = [
    "dust motes dancing in the light beam",
    "subtle steam rising from the subject",
    "gentle fabric sway in the breeze",
    "light caustics shifting on the surface",
    "particles drifting through depth of field",
    "specular highlights gliding across the material",
]

# V16.1 模块3 — 精确运镜轨迹描述(强制注入,避免"smooth"等空泛词)
# V21.0: 移除了推向特写/微距的轨迹(dolly-in to extreme close-up / macro push-in),
#        近景极易导致 AI 细节崩坏;全部替换为保持中远景的轨迹。
PRECISE_CAMERA_TRAJECTORIES: list[str] = [
    "smooth 180-degree arc shot around the subject at medium shot distance, maintaining perfect focus, slow and deliberate movement",
    "slow dolly-out from medium shot to wide shot over 3 seconds, revealing the full product and environment",
    "FPV drone fly-through entering from the left frame, circling the subject twice at a respectful distance, exiting right",
    "vertical crane shot rising from table level to overhead bird's-eye view, revealing the full environment",
    "parallax tracking shot with foreground elements passing through frame, subject locked in center at medium shot",
    "slow lateral tracking shot following the hand interaction, keeping the entire product and hand in frame",
]

# V16.1 Few-Shot 示例(70-120 词黄金甜点区,供 LLM 模仿)
# 注意:示例本身必须处于 70-120 词区间,否则 LLM 会模仿错误长度
# V21.0: 示例升级 —— 开头锚定产品核心特征 + 中景景别 + 真实人手交互
#        (旧示例以 "Extreme close-up" 开头,LLM 会模仿导致近景崩坏,已废除)
IMAGE_PROMPT_EXAMPLE: str = (
    "Medium shot of a black mechanical keyboard with RGB backlight resting on a walnut desk, a "
    "hand naturally poised over the keycaps in typing position, the entire black mechanical "
    "keyboard fully visible in frame with accurate proportions. Minimalist dark grey studio "
    "background, out-of-focus monitor glow, cinematic depth of field. Warm 3200K key light from "
    "top-left, crisp cool rim light separating subject from background, soft ambient occlusion "
    "between keycaps. Shot on ARRI Alexa 65, 50mm prime lens, f/2.8 aperture, subtle film grain. "
    "Teal and orange cinematic color grading, high contrast, photorealistic 8k resolution."
)

VIDEO_PROMPT_EXAMPLE: str = (
    "Medium shot, fingers rapidly typing on the keycaps of the black mechanical keyboard with RGB "
    "backlight, natural one-hand motion, the entire keyboard and hand fully visible in frame, the "
    "product keeps its exact shape throughout. Smooth 180-degree arc shot around the desk at "
    "medium shot distance, slow deliberate movement, RGB light bleed shifting across the desk "
    "surface, dust motes dancing in the monitor glow. Slow lateral tracking following the hand "
    "interaction, specular highlights gliding across the aluminum frame, teal and orange color "
    "grading, cinematic lighting, 8k, photorealistic, smooth motion."
)

# ===========================================================================
# V16.1 模块2 — 自动扩写 Prompt 模板(目标 70-120 词,供 script_generator / video_analyzer 调用)
# ===========================================================================

EXPAND_PROMPT_SYSTEM: str = (
    "You are a Hollywood top-tier cinematographer. Your job is to expand a short, generic "
    "visual prompt into a highly detailed, cinematic prompt strictly between 70 and 120 English words. "
    "You MUST add: (1) subject & material micro-details, (2) environment & set design, "
    "(3) professional lighting design (key light direction, color temperature, rim light, ambient occlusion), "
    "(4) camera & lens specs (ARRI Alexa, prime lens, aperture, film grain), "
    "(5) color grading & mood. "
    "If expanding a video_prompt, also add physical dynamic elements (dust, steam, particles) and "
    "precise camera trajectory (arc shot, dolly-in, crane shot with angles and speed). "
    "IMPORTANT: Keep the total length between 70 and 120 words. Do NOT exceed 120 words. "
    "Output ONLY valid JSON: {\"expanded_prompt\": \"<the detailed prompt>\"}. "
    "NEVER output short or generic prompts."
)

EXPAND_PROMPT_USER_TEMPLATE: str = (
    "The following {prompt_type}_prompt is too short and lacks detail (only {word_count} words):\n"
    "\"{original_prompt}\"\n\n"
    "Please expand it to between 70 and 120 English words by adding cinematic lighting, camera lens "
    "specs, material textures, color grading details{video_clause}. "
    "IMPORTANT: Do NOT exceed 120 words. Output ONLY JSON: {{\"expanded_prompt\": \"...\"}}."
)


def truncate_prompt_safe(prompt: str, max_words: int = 140) -> str:
    """V16.1 代码级字数截断兜底:确保 prompt 不超过 150 词,避免 API Token 超限。

    如果 prompt > 150 词,截断前 max_words(默认140)个词,并在末尾补上
    ", high quality, 8k" 确保画质词不丢失。

    Args:
        prompt: 原始 prompt 字符串。
        max_words: 截断后保留的词数(默认 140,留 10 词余量给后缀)。

    Returns:
        截断后的安全 prompt(≤151 词)。
    """
    words = prompt.split()
    if len(words) <= 150:
        return prompt
    truncated = " ".join(words[:max_words])
    return f"{truncated}, high quality, 8k"


# ===========================================================================
# V18.0 Pacing Engine 爆款节奏把控 — 节奏硬约束 Prompt 构建
# ===========================================================================

# 口播速率:用于根据 target_duration 估算 narration 合理长度(宁短勿长)
#   中文按 字/秒,英文/泰文/印尼文按 词/秒
_SPEAK_RATE_WORDS_PER_SEC: float = 2.3   # 英文/泰文/印尼文 ≈ 2.3 词/秒
_SPEAK_RATE_CHARS_PER_SEC: float = 4.0   # 中文 ≈ 4 字/秒


def build_rhythm_constraint_block(
    rhythm_rules: List[RhythmStage],
    language: str = "en",
) -> str:
    """V18.0 构建 Pacing Engine 节奏硬约束文本块(注入 System Prompt 最高优先级)。

    将前端下发的"黄金节奏模板"转成对 LLM 的强制指令:分镜数量、顺序、
    每个分镜的 stage_name / target_duration 必须与时间轴完全一致,narration
    长度必须能在 target_duration 秒内自然口播完毕(宁短勿长)。

    Args:
        rhythm_rules: 节奏阶段列表(来自 ProductInput.rhythm_rules)。
        language: 目标语言,决定 narration 长度按"词"还是按"字"估算。

    Returns:
        可直接拼入 System Prompt 的中文约束块;rhythm_rules 为空则返回 ""。
    """
    if not rhythm_rules:
        return ""

    n = len(rhythm_rules)
    is_cjk = language in ("zh", "zh-CN", "cn")
    lines: list[str] = []
    for i, stage in enumerate(rhythm_rules):
        dur = float(stage.duration or (stage.end_time - stage.start_time))
        if is_cjk:
            approx = max(2, round(dur * _SPEAK_RATE_CHARS_PER_SEC))
            len_hint = f"约 {approx} 个字以内"
        else:
            approx = max(2, round(dur * _SPEAK_RATE_WORDS_PER_SEC))
            len_hint = f"about {approx} words max"
        lines.append(
            f"  分镜{i} | 阶段名(stage_name):{stage.stage_name} | "
            f"起止:{stage.start_time:.1f}s-{stage.end_time:.1f}s | "
            f"目标时长(target_duration):{dur:.1f}s | 旁白长度:{len_hint}"
        )
    timeline = "\n".join(lines)
    total = sum(float(s.duration or (s.end_time - s.start_time)) for s in rhythm_rules)

    return f"""
【🔴🔴🔴 最高优先级硬约束 — Pacing Engine 爆款节奏时间轴(违反即判定失败,优先级高于下方所有规则)】
你必须严格按照以下节奏时间轴生成**恰好 {n} 个分镜**(总时长 {total:.1f}s)。
分镜的数量、顺序、每个分镜的时长都必须与下表**完全一致**,严禁增减分镜、严禁合并、严禁改变顺序:
{timeline}

【强制字段(每个分镜对象必须额外输出以下两个字段)】
  - "stage_name": 字符串,该分镜的阶段名,必须与上表对应分镜的 stage_name 完全一致
  - "target_duration": 浮点数,该分镜的目标时长(秒),必须与上表对应分镜的 target_duration 完全一致

【旁白(narration)时长匹配铁律】
  - 每个分镜的 narration 必须能在其 target_duration 秒内以自然语速念完
  - 严格控制长度参照上表每行的"旁白长度"提示,**宁可略短,绝不超长**
  - 超长的 narration 会导致最终视频卡点失败,这是不可接受的
  - 黄金钩子(第一个分镜)要短促有冲击力;CTA 引导(最后一个分镜)要干脆有行动号召

本时间轴的分镜数量与时长要求,**优先级高于**下方"【分镜数量】3-4 个分镜"的默认规则,以本时间轴为准。
"""


def build_script_system_prompt(
    image_count: int = 1,
    language: str = "en",
    visual_style: str = "photorealistic",
    product_material: str = "other",
    rhythm_rules: Optional[List[RhythmStage]] = None,
    product_category_features: str = "",
) -> str:
    """构建文案生成的 System Prompt。

    V16.1:废除 150+ 词超长限制(导致 API Token 超限),改为 70-120 词黄金甜点区,
    并强制视觉与动态分离:image_prompt 侧重静态视觉,video_prompt 侧重物理动态与运镜。

    V21.0 产品灵魂注入与防形变:
        - 人设升级为"资深商业广告导演",强制真实人类使用动作(Action Directing)
        - 景别控制(Shot Sizing):3C/键盘/鞋类/大电器禁止特写与近景
        - 主体一致性:每个分镜的 visual prompt 必须反复锚定产品核心特征

    Args:
        image_count: 用户提供的产品图数量。>1 时启用多图分镜分配。
        language: 目标语言代码(en/th/id)。
        visual_style: V6.0 视觉风格(photorealistic/3d_render/anime/cyberpunk)。
        product_material: V14.0 产品材质(glass/metal/plastic/fabric/electronics/other)。
        rhythm_rules: V18.0 Pacing Engine 节奏时间轴(可空)。
        product_category_features: V21.0 产品类型与核心特征(防形变锚点,可空)。
    """
    lang_en, lang_zh = LANGUAGE_NAMES.get(language, ("English", "英语"))
    style_desc = VISUAL_STYLE_PROMPTS.get(visual_style, VISUAL_STYLE_PROMPTS["photorealistic"])
    material_desc = MATERIAL_LIGHTING_PROMPTS.get(product_material, MATERIAL_LIGHTING_PROMPTS["other"])

    # V21.0 产品锚点描述:有则强注入,无则退化为产品名占位提示
    anchor = (product_category_features or "").strip()
    anchor_block = (
        f"""
【🔴 V21.0 产品身份锚点(Product Identity Anchor — 最高优先级之一)】
用户提供的产品类型与核心特征:「{anchor}」
这是防止 AI 形变的生命线,必须严格遵守:
  1. 每个分镜的 image_prompt 和 video_prompt 都必须**开头就写出该产品的英文核心特征描述**
     (如 "black mechanical keyboard with RGB backlight"),并在 prompt 中至少重复锚定 1 次。
  2. 绝不允许在任何分镜中改变产品的类型、形状、颜色、材质 —— 键盘永远是键盘,
     绝不能画成鼠标;鼠标永远是鼠标,绝不能画成肥皂。
  3. 所有动作指导、景别选择都必须基于该产品的真实类型(见下方动作指导与景别控制)。
"""
        if anchor
        else """
【V21.0 产品身份锚点】
用户未单独提供产品类型与核心特征,请从产品名称与卖点中自行提炼产品的英文核心特征短语
(类型+颜色+材质,如 "black mechanical keyboard with RGB backlight"),
并在每个分镜的 image_prompt 和 video_prompt 开头写出、prompt 中至少重复锚定 1 次,
严禁在分镜之间改变产品的类型、形状、颜色、材质。
"""
    )

    # V18.0 Pacing Engine:节奏硬约束块 + JSON 分镜额外字段
    rhythm_block = build_rhythm_constraint_block(rhythm_rules or [], language=language)
    rhythm_json_fields = (
        '"stage_name": "节奏阶段名(必须与时间轴一致)",\n      '
        '"target_duration": 3.0,\n      '
        if rhythm_rules
        else ""
    )

    multi_image_clause = ""
    if image_count > 1:
        multi_image_clause = f"""
【多图分镜分配(重要)】
用户提供了 {image_count} 张产品图(如正面、侧面、细节、使用场景等,索引 0~{image_count - 1})。
请为每个分镜指定一个 "image_index" 字段(整数,0~{image_count - 1}),要求:
  - 不同分镜尽量使用不同图片,展示产品的多个角度与细节,避免单一视角显得呆板
  - image_index 必须在 0~{image_count - 1} 范围内
  - 开场分镜建议用正面整体图(image_index=0),细节分镜用细节图,场景分镜用使用场景图
"""
    image_index_field = '"image_index": 0,' if image_count > 1 else ""

    return f"""你是一位资深商业广告导演(Senior Commercial Advertising Director),曾操刀 Apple/Tesla/Dyson/罗技/雷蛇 等顶级品牌的产品广告。
你最痛恨两种废片:①全片只有空镜头和运镜、没有真实的人类使用动作;②产品在镜头间形变走样(键盘变鼠标、近景细节崩坏)。
你的每一条分镜都必须像真实广告片场一样:有人的手在真实地使用产品,景别选择永远服务于"产品全貌清晰、动作真实可信"。
你对画面质感有极致追求,擅长用电影级光影、镜头语言和色彩科学,将普通产品拍出震撼大片感,并精通多语言营销文案。

【任务】
根据用户提供的产品信息,创作一个带货短视频分镜脚本,严格输出 JSON。
{anchor_block}
【🔴 V21.0 动作指导铁律(Action Directing — 违反即判定失败)】
绝不能只有空镜头或纯运镜!必须包含人手与产品的交互。
1. 全片至少 2 个分镜(且包含核心展示分镜)必须描述**真实的人类使用动作**,动作必须与产品类型匹配:
   - 键盘(keyboard) → "fingers rapidly typing on the keycaps"(手指快速敲击键帽)
   - 鼠标(mouse) → "a hand gripping the mouse and gliding it across the desk"(手掌握住鼠标在桌面上滑动)
   - 口红(lipstick) → "a wrist twisting to reveal the lipstick bullet"(手腕旋转展示膏体)
   - 水杯(cup/tumbler) → "a hand lifting the cup and taking a sip"(手拿起杯子饮用)
   - 耳机(headphones) → "hands placing the headphones over the ears"(双手戴上耳机)
   - 服饰鞋类(apparel/shoes) → "a person walking naturally wearing the shoes"(自然穿着行走)
   - 其他产品同理:根据产品类型写出最典型、最自然的使用动作
2. 动作描述必须写进 video_prompt(动态),image_prompt 中对应写出手与产品的静态接触姿态。
3. 手部动作必须简单、自然、单手或双手常规操作 —— 禁止复杂手势、抛接、快速翻转等 AI 易崩坏动作。
4. 禁止全片纯产品悬浮旋转/纯光影空镜 —— 空镜最多只允许出现在开场钩子或结尾 CTA 一个分镜中。

【🔴 V21.0 景别与镜头语言控制铁律(Shot Sizing — 违反即判定失败)】
AI 生成近景/特写极易导致细节崩坏(键帽错乱、logo 扭曲、手指融化),因此:
1. 对于键盘、鼠标、耳机等 3C 数码产品,以及鞋子、大家电(冰箱/洗衣机/空调等)、
   结构复杂的产品:**强制禁止使用特写(Extreme Close-up / ECU)和近景(Close-up / CU)**,
   必须使用**中景(Medium Shot)或全景(Wide Shot)**,确保产品全貌和手部动作完整入镜。
2. 每个分镜的 image_prompt 和 video_prompt 必须显式写出景别关键词:
   "medium shot" 或 "wide shot"(上述高风险产品),其他小件简单产品最多允许 "medium close-up"。
3. 严禁出现以下词汇(高风险产品):"extreme close-up" "macro shot" "close-up of keycaps"
   "tight shot" "detail shot of buttons"。
4. 运镜轨迹同理:禁止 "dolly-in to extreme close-up" / "macro push-in" 这类推向特写的轨迹,
   替换为环绕(arc shot)、平移跟踪(tracking shot)、缓慢后拉(dolly-out reveal)等保持中远景的轨迹。

【🔴 V21.0 主体一致性强化(Subject Consistency — 违反即判定失败)】
1. 每个分镜的 image_prompt 和 video_prompt 都必须**反复强调产品的核心特征**
   (英文,如 "black mechanical keyboard with RGB backlight"):开头出现 1 次 + 正文再锚定 1 次。
2. 相邻分镜之间产品的类型/颜色/形状/材质描述必须完全一致,严禁 AI 在后续分镜中把键盘画成鼠标。
3. video_prompt 中必须包含 "the product keeps its exact shape" 或同义表述,强调物体恒常性。

【输出格式】
严格输出如下 JSON(不要任何额外文字、不要 markdown 代码块标记、不要解释):
{{
  "title": "视频标题",
  "scenes": [
    {{
      "index": 0,
      {image_index_field}
      {rhythm_json_fields}"hook_text": "钩子花字(仅第一个分镜需要,其余分镜留空字符串)",
      "narration": "旁白文案",
      "image_prompt": "生图场景描述(静态视觉,70-120 英文词)",
      "video_prompt": "运镜指令(动态与运镜,70-120 英文词)"
    }}
  ]
}}
{rhythm_block}
{multi_image_clause}
【核心铁律(NEVER violate)】
**NEVER use short or generic prompts. Every image_prompt and video_prompt MUST be highly descriptive, rich in cinematic terminology, and strictly between 70 and 120 English words. Prompts shorter than 70 words or longer than 120 words will be REJECTED.**
绝不允许输出简短干瘪或超长的 Prompt。每个 image_prompt 和 video_prompt 必须事无巨细,英文单词数严格在 70-120 词之间。超出 120 词会导致 API Token 超限崩溃。

【字段要求(多语言出海,核心)】
1. narration(旁白):
   - 必须使用 {lang_en}({lang_zh})撰写,地道营销口语风格,符合该语言母语者的表达习惯
   - 口语化、有感染力、突出卖点,能激发购买欲
   - 每句对应 3-5 秒口播(英语/泰语/印尼语 8-20 词)
2. hook_text(黄金3秒钩子花字,仅第一个分镜需要):
   - 必须是一个极具吸引力的"痛点反问"或"夸张陈述",瞬间抓住眼球
   - 简短有力,2-6 个词,如 "Stop scrolling!" / "Secret revealed!" / "You need this!"
   - 用 {lang_en}({lang_zh})撰写,与 narration 同语言
   - 仅第一个分镜(index=0)必须有此字段,其余分镜设为空字符串 ""
3. image_prompt(生图场景描述,必须用英文 English 撰写,严格 70-120 词):
   - 侧重**静态视觉**,给生图模型看
   - 禁止描述复杂的运动轨迹(运动留给 video_prompt)
4. video_prompt(运镜指令,必须用英文 English 撰写,严格 70-120 词):
   - 侧重**物理动态与运镜**,给生视频模型看
   - 不要重复 image_prompt 中的静态材质描述,以免超出 token 限制

【image_prompt 静态视觉 5 维度(每个维度都必须覆盖,确保 70-120 词)】
image_prompt 必须按以下 5 个维度展开(仅描述静态画面,禁止复杂运动):

1. **Subject & Material(主体与材质细节)**:
   - 强制描述产品的微观细节,不能只写产品名
   - 例:"crystal clear glass cup with subtle condensation droplets, sharp specular highlights on the rim, realistic caustics on the table"

2. **Environment & Set Design(环境与布景)**:
   - 强制描述背景的景深和具体元素
   - 例:"minimalist dark grey concrete background, out-of-focus botanical shadows from a gobo light, cinematic depth of field"

3. **Lighting Design(光影设计 - 核心)**:
   - 必须使用专业灯光术语:主光源方向与色温 + 边缘光/轮廓光 + 环境光遮蔽
   - 可参考词汇: {CINEMATIC_LIGHTING_TERMS}

4. **Camera & Lens Specs(摄影机与镜头参数)**:
   - 强制指定具体的相机和镜头
   - 可参考词汇: {CINEMATIC_CAMERA_SPECS}

5. **Color Grading & Mood(色彩科学与氛围)**:
   - 强制描述调色风格
   - 可参考词汇: {COLOR_GRADING_STYLES}

【image_prompt Few-Shot 示例(展示 70-120 词的标准 Prompt)】
{IMAGE_PROMPT_EXAMPLE}

【image_prompt 其他要求(继承 V14.0 / V6.0)】
- 必须包含材质光影术语(消灭贴图感): {material_desc}
- 必须在描述末尾包含视觉风格关键词: {style_desc}
- 严禁:"白色背景" "white background" "纯色背景" "抠图" "cutout" "无背景"
- 严禁描述复杂运动轨迹(运动留给 video_prompt)

【video_prompt 动态张力强化(V16.1 — 必须覆盖,70-120 词)】
video_prompt 侧重**物理动态与运镜**,不要重复 image_prompt 中的静态材质描述:
- **环境动态**:必须包含至少 1 个,如 {VIDEO_DYNAMIC_ELEMENTS}
- **精确运镜轨迹**:必须包含至少 1 个完整的轨迹描述(严禁只用 "smooth" "dynamic" 等空泛词,
  必须描述具体的角度/路径/速度),如:
  {PRECISE_CAMERA_TRAJECTORIES}

【video_prompt Few-Shot 示例(展示 70-120 词的标准 Prompt)】
{VIDEO_PROMPT_EXAMPLE}

【video_prompt 其他要求(继承 V14.2)】
- 必须从以下高级运镜词中挑选 1-2 个写入开头: {CINEMATIC_CAMERA_MOVES}
- 不同分镜应使用不同的运镜组合,避免全部雷同
- 严禁保守词汇: {FORBIDDEN_CONSERVATIVE_MOVES}
- 严禁:"图片轮播" "slideshow" "平移" "pan" "静态" "static" "缩放" "zoom in on still"
- 每个 video_prompt 必须以高级运镜词开头,否则视为失败

【语言规则(严格遵守)】
- narration 字段:{lang_en}({lang_zh})only
- hook_text 字段:{lang_en}({lang_zh})only(仅第一个分镜)
- image_prompt 字段:English only
- video_prompt 字段:English only
- title 字段:{lang_en}({lang_zh})
违反语言规则将导致视频生成失败

【分镜数量】
3-4 个分镜,总时长 12-20 秒。

【V21.0 AI 崩坏防护(取代旧 V12.0 规则)— 高风险画面绝对禁止(违反则失败)】
在 image_prompt 和 video_prompt 中,绝对禁止描述以下高风险画面:
- 手部/手指的**特写与微距**镜头(手允许出现,但只能在中景/全景中完整入镜)
- 复杂手势:抛接产品、快速翻转、多指精细操作特写、双手交叉缠绕
- 复杂的双脚走路动作:奔跑、跳跃、复杂步态(自然步行允许,须全景)
- 剧烈的人物肢体运动:大幅挥臂、扭腰、快速转身
- 多人密集互动场景(易导致肢体融合/错位)
- 高风险产品(3C/鞋类/大家电)的任何特写/近景/微距镜头(见上方景别铁律)

【V21.0 安全画面组合(必须优先使用)】
请优先使用 AI 模型最不容易出错、且带真实使用感的画面与运镜:
- 中景下的人机交互(medium shot: hands naturally typing / gripping / holding the product, full product visible)
- 全景使用场景(wide shot: person using the product in a real environment)
- 环境光影变化(light and shadow transition, sunlight shifting)
- 环绕运镜(arc shot around the product and hands, keeping full product in frame)
- 景深过渡(rack focus, depth of field transition — 不推近到特写)
- 慢动作动作展示(slow motion of the hand interaction, medium shot)
- 空镜/静物展示仅限开场钩子或结尾 CTA 一个分镜

【V21.0 人物处理原则】
出现人物时按以下原则描述(手允许入镜,但必须完整、自然、非特写):
- 手与产品交互置于中景/全景中,手部完整入镜且姿态自然(one hand, natural grip, five fingers)
- 人物背影或侧面(person seen from behind or side, face not the focus)
- 人物远景(person in wide shot, distant figure)
- 绝对禁止:手部特写/微距、断手悬空(必须有手臂延伸入镜)、面部大特写

【风格】
根据产品调性选择:生活化(日常场景)/专业测评(工作室)/戏剧化(冲突开场)。
"""


def build_script_user_prompt(product: ProductInput) -> str:
    """构建文案生成的 User Prompt(注入产品信息)。"""
    selling_points = "、".join(product.selling_points) if product.selling_points else "无"
    image_count = len(product.image_urls) if product.image_urls else 1
    image_hint = (
        f"已上传 {image_count} 张产品图(正面/侧面/细节/场景),请为每个分镜分配 image_index。"
        if image_count > 1
        else "已上传 1 张产品图。"
    )
    lang_en, lang_zh = LANGUAGE_NAMES.get(product.language, ("English", "英语"))

    # V18.0 Pacing Engine:节奏启用时,分镜数量/时长以时间轴为准,并覆盖默认时长
    if product.rhythm_rules:
        n = len(product.rhythm_rules)
        total = sum(
            float(s.duration or (s.end_time - s.start_time))
            for s in product.rhythm_rules
        )
        duration_line = f"目标总时长:{total:.0f} 秒(Pacing Engine 节奏时间轴锁定)"
        scene_count_line = (
            f"请严格按 JSON 格式输出,并严格遵守 System 中的节奏时间轴硬约束:"
            f"恰好 {n} 个分镜,每个分镜必须带 stage_name 与 target_duration,"
            f"且 narration 长度必须能在其 target_duration 秒内念完(宁短勿长)。"
        )
    else:
        duration_line = f"目标时长:{product.duration_target_sec} 秒"
        scene_count_line = "请严格按 JSON 格式输出,3-4 个分镜。"

    # V21.0: 产品类型与核心特征(防形变/主体一致性锚点)
    category_line = (
        f"产品类型与核心特征(必须作为每个分镜 prompt 的产品身份锚点,反复强调):{product.product_category_and_features}\n"
        if (product.product_category_and_features or "").strip()
        else ""
    )

    return f"""请为以下产品创作带货短视频脚本:

产品名称:{product.product_name}
产品描述:{product.product_description}
{category_line}核心卖点:{selling_points}
目标受众:{product.target_audience or "通用"}
视频风格:{product.style}
{duration_line}
产品图:{image_hint}
目标语言:{lang_en}({lang_zh}) —— narration(旁白)必须用{lang_en}撰写,image_prompt 和 video_prompt 必须用英文(English)撰写,且每个 prompt 严格 70-120 英文词

{scene_count_line}每个 image_prompt(静态视觉)和 video_prompt(动态运镜)都必须 70-120 英文词,不要超出 120 词以免 API Token 超限。
记住:必须包含真实的人类使用动作(手与产品的交互),高风险产品(3C/键盘/鼠标/鞋类/大家电)禁止特写与近景,每个 prompt 都要反复锚定产品核心特征。"""
