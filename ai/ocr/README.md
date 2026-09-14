# ocr

画面文字识别（OCR）模块。

## 当前实现

- `base.py`：`OcrProvider` 抽象与 `OcrTextLine` 结果结构，业务层只依赖本抽象，不感知具体引擎。
- `paddleocr.py`：主 OCR 实现 `PaddleOcrProvider`，基于 PaddleOCR 官方包（默认 PP-OCRv6，CPU）；结果解析仅兼容 PaddleOCR 3.x `predict()` 的 dict-like 结构（`rec_texts` / `rec_scores` / `rec_polys`），其他结构明确抛 `OcrResultFormatError`。
- `factory.py`：默认 Provider 入口 `get_default_provider()`，按配置 `OCR_PRIMARY_MODEL` 选择实现，进程级线程安全惰性单例；业务层（backend）只允许经此入口获取 Provider。

## 配置项

均可用同名环境变量或 `.env` 覆盖（定义见 `backend/app/core/config.py`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OCR_PRIMARY_MODEL` | `PP-OCRv6` | 主 OCR 模型（PaddleOCR ocr_version） |
| `OCR_FALLBACK_MODEL` | `PaddleOCR-VL-1.6` | 备用疑难 OCR 模型标识（仅记录不加载，后续切片接入） |
| `OCR_LANG` | `ch` | 识别语言 |
| `OCR_USE_GPU` | `false` | 是否使用 GPU（默认 CPU） |

依赖：`paddleocr` + CPU 版 `paddlepaddle` + `torch`（Windows 下须先于 paddle 加载以规避 DLL 冲突，见 requirements.txt 注释）。模型权重首次运行自动下载到用户缓存目录（`~/.paddlex`），不进入仓库。
