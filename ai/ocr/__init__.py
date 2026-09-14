"""OCR 模态：画面文字识别。

base 定义 OcrProvider 抽象，paddleocr 提供 PP-OCRv6 实现（主 OCR），
factory 提供默认 Provider 入口 get_default_provider（按配置选择实现，
业务层只依赖抽象与工厂，不 import 具体实现模块）。
备用疑难 OCR（PaddleOCR-VL）后续以同接口新增实现接入，模型标识见配置。
"""
