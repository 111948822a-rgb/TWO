"""第三方 API 适配层:LLM / 图片 / 视频 / TTS Provider 抽象与实现。

设计目标:业务层(services/*)只依赖抽象基类(ILLMProvider / IImageProvider 等),
具体厂商实现可热替换,不影响 pipeline 逻辑。
"""
