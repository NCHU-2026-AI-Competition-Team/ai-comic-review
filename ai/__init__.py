"""AI 模块包：多模态审核能力（OCR/ASR/视觉/VLM/风险融合）。

各模态以 Provider 抽象对外暴露统一接口，业务层（backend/app/services）
只依赖 ai.{modality}.base 定义的抽象，具体引擎实现可独立替换。
"""
