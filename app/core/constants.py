"""全局常量:阶段中文标签与耗时估算。

供进度页 Stepper / 日志展示、Orchestrator 阶段日志统一引用。
集中定义以避免循环依赖:projects 与 orchestrator 都会用到 STAGE_LABELS,
而 projects 在顶层导入 orchestrator(反向导入会触发循环依赖),
因此抽离到独立的无环依赖模块。

键类型:ProjectStatus 枚举(与 orchestrator / routes 中 stage / status 的类型一致),
取值 .get(stage, stage.value) 时 stage 即枚举,未命中回退枚举的 .value 字符串。
"""
from __future__ import annotations

from app.schemas.project import ProjectStatus

# 阶段中文标签(进度页 Stepper / 日志展示)
STAGE_LABELS: dict[ProjectStatus, str] = {
    ProjectStatus.PENDING: "排队中",
    ProjectStatus.SCRIPTING: "正在生成文案分镜",
    ProjectStatus.IMG_GEN: "正在生成场景图片",
    ProjectStatus.VID_GEN: "正在生成视频片段(耗时较长)",
    ProjectStatus.AUDIO_GEN: "正在合成旁白音频",
    ProjectStatus.COMPOSITING: "正在后期剪辑合成",
    ProjectStatus.COMPLETED: "已完成",
    ProjectStatus.FAILED: "失败",
    ProjectStatus.AWAITING_SELECTION: "等待选择候选素材",
}

# 各阶段预估剩余秒数(经验值,仅用于进度页安抚性展示)
STAGE_ETA_SECONDS: dict[ProjectStatus, int] = {
    ProjectStatus.PENDING: 30,
    ProjectStatus.SCRIPTING: 30,
    ProjectStatus.IMG_GEN: 60,
    ProjectStatus.VID_GEN: 120,
    ProjectStatus.AUDIO_GEN: 30,
    ProjectStatus.COMPOSITING: 30,
    ProjectStatus.AWAITING_SELECTION: 0,
    ProjectStatus.COMPLETED: 0,
    ProjectStatus.FAILED: 0,
}
