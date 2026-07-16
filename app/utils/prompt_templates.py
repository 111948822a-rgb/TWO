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

from app.schemas.project import ProductInput

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
PRECISE_CAMERA_TRAJECTORIES: list[str] = [
    "smooth 180-degree arc shot around the subject, maintaining perfect focus, slow and deliberate movement",
    "slow dolly-in from wide to extreme close-up over 3 seconds, rack focus from background to product",
    "FPV drone fly-through entering from the left frame, circling the subject twice, exiting right",
    "vertical crane shot rising from table level to overhead bird's-eye view, revealing the full environment",
    "parallax tracking shot with foreground elements passing through frame, subject locked in center",
    "macro slow-motion push-in to product surface texture, 120fps capturing light refraction",
]

# V16.1 Few-Shot 示例(70-120 词黄金甜点区,供 LLM 模仿)
# 注意:示例本身必须处于 70-120 词区间,否则 LLM 会模仿错误长度
IMAGE_PROMPT_EXAMPLE: str = (
    "Extreme close-up of a crystal clear glass cup with condensation droplets, sharp specular "
    "highlights on the rim, realistic caustics casting light patterns on the table. Minimalist "
    "dark grey concrete background, out-of-focus botanical shadows from a gobo light, cinematic "
    "depth of field with creamy bokeh. Warm 3200K key light from top-left, crisp cool rim light "
    "separating subject from background, soft ambient occlusion in crevices. Shot on ARRI Alexa "
    "65, 85mm prime lens, f/1.8 aperture, anamorphic lens flares, subtle film grain. Teal and "
    "orange cinematic color grading, high contrast, moody atmosphere, photorealistic 8k resolution."
)

VIDEO_PROMPT_EXAMPLE: str = (
    "Smooth 180-degree arc shot around the subject, maintaining perfect focus, slow deliberate "
    "movement. Dust motes dancing in the light beam, subtle steam rising from the cup, light "
    "caustics shifting on the surface. FPV drone fly-through entering left frame, circling twice, "
    "exiting right with a rack focus from foreground to product. Parallax tracking with foreground "
    "elements passing through frame, specular highlights gliding across the material. Particles "
    "drifting through depth of field, anamorphic lens flares, teal and orange color grading, high "
    "contrast, cinematic lighting, 8k, photorealistic, smooth motion."
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


def build_script_system_prompt(
    image_count: int = 1,
    language: str = "en",
    visual_style: str = "photorealistic",
    product_material: str = "other",
) -> str:
    """构建文案生成的 System Prompt。

    V16.1:废除 150+ 词超长限制(导致 API Token 超限),改为 70-120 词黄金甜点区,
    并强制视觉与动态分离:image_prompt 侧重静态视觉,video_prompt 侧重物理动态与运镜。

    Args:
        image_count: 用户提供的产品图数量。>1 时启用多图分镜分配。
        language: 目标语言代码(en/th/id)。
        visual_style: V6.0 视觉风格(photorealistic/3d_render/anime/cyberpunk)。
        product_material: V14.0 产品材质(glass/metal/plastic/fabric/electronics/other)。
    """
    lang_en, lang_zh = LANGUAGE_NAMES.get(language, ("English", "英语"))
    style_desc = VISUAL_STYLE_PROMPTS.get(visual_style, VISUAL_STYLE_PROMPTS["photorealistic"])
    material_desc = MATERIAL_LIGHTING_PROMPTS.get(product_material, MATERIAL_LIGHTING_PROMPTS["other"])

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

    return f"""你是一位好莱坞顶级摄影指导(Cinematographer)兼电商短视频导演,曾操刀 Apple/Tesla/Dyson 等顶级品牌广告。
你对画面质感有极致追求,擅长用电影级光影、镜头语言和色彩科学,将普通产品拍出震撼大片感。
你精通多语言营销文案,能用镜头语言激发购买欲。

【任务】
根据用户提供的产品信息,创作一个带货短视频分镜脚本,严格输出 JSON。

【输出格式】
严格输出如下 JSON(不要任何额外文字、不要 markdown 代码块标记、不要解释):
{{
  "title": "视频标题",
  "scenes": [
    {{
      "index": 0,
      {image_index_field}
      "hook_text": "钩子花字(仅第一个分镜需要,其余分镜留空字符串)",
      "narration": "旁白文案",
      "image_prompt": "生图场景描述(静态视觉,70-120 英文词)",
      "video_prompt": "运镜指令(动态与运镜,70-120 英文词)"
    }}
  ]
}}
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

【V12.0 AI 崩坏防护 — 高风险画面绝对禁止(违反则失败)】
在 image_prompt 和 video_prompt 中,绝对禁止描述以下高风险画面:
- 复杂的人体手部动作:手部特写、手指交互、拿东西的特写、手部手势
- 复杂的双脚走路动作:奔跑、跳跃、复杂步态
- 剧烈的人物肢体运动:大幅挥臂、扭腰、快速转身
- 多人密集互动场景(易导致肢体融合/错位)

【V12.0 AI 崩坏防护 — 鼓励安全画面(必须优先使用)】
请优先使用 AI 模型最擅长、最不容易出错的画面与运镜:
- 静物展示(product still life display,产品居中展示)
- 微距特写(macro shot, extreme close-up of product texture/detail)
- 环境光影变化(light and shadow transition, sunlight shifting)
- 产品悬浮旋转(product floating and rotating, 360 degree rotation)
- 景深过渡(rack focus, depth of field transition)
- 慢动作特写(slow motion close-up, capturing subtle details)

【V12.0 人物处理原则】
如果必须出现人物,请按以下原则描述(避开手部):
- 人物背影(person seen from behind, back view)
- 人物远景(person in wide shot, distant figure, face blurred)
- 仅展示人物躯干/局部(torso only, partial body, hands NOT visible)
- 绝对禁止:手部特写、手指动作、人物正面手持产品的画面

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
    return f"""请为以下产品创作带货短视频脚本:

产品名称:{product.product_name}
产品描述:{product.product_description}
核心卖点:{selling_points}
目标受众:{product.target_audience or "通用"}
视频风格:{product.style}
目标时长:{product.duration_target_sec} 秒
产品图:{image_hint}
目标语言:{lang_en}({lang_zh}) —— narration(旁白)必须用{lang_en}撰写,image_prompt 和 video_prompt 必须用英文(English)撰写,且每个 prompt 严格 70-120 英文词

请严格按 JSON 格式输出,3-4 个分镜。每个 image_prompt(静态视觉)和 video_prompt(动态运镜)都必须 70-120 英文词,不要超出 120 词以免 API Token 超限。"""


def truncate_prompt_safe(prompt: str, max_words: int = 140) -> str:
    """V16.1 字数截断兜底:如果 prompt > 150 词,截断前 max_words 词并补上画质后缀。

    作为最后一道防线,在将 Prompt 传给底层 API 之前调用,
    确保绝对不会触发 API 的超长报错(PromptTooLong / InvalidParameter)。

    Args:
        prompt: 原始 prompt 字符串。
        max_words: 截断后保留的最大词数(默认 140)。

    Returns:
        截断后的 prompt(若原始 ≤150 词则原样返回)。
    """
    words = prompt.split()
    if len(words) <= 150:
        return prompt
    truncated = " ".join(words[:max_words])
    return f"{truncated}, high quality, 8k"
