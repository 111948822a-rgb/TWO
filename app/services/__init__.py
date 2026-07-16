"""业务服务层:文案 / 图片 / 视频 / 音频 / 剪辑。

每个 service 负责一个阶段的业务逻辑,调用 providers 层完成实际 API 调用,
更新 VideoProject / Scene 的状态与素材字段。
"""
