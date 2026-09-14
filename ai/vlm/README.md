# vlm

视觉语言大模型（VLM）内容安全主审。

- `base.py`：`VlmProvider` 抽象、主审输入与异常层次
- `schemas.py`：`/review` 严格 JSON 响应模型与解析
- `qwen.py`：Modal 云端 Qwen3-VL HTTPS 客户端（主审 8B）
- `factory.py`：按 `VLM_PROVIDER` 分派默认 Provider
- `escalate.py`：低置信 / 高风险 / JSON 不合格 / 模态冲突的 escalation 判定，以及 OCR/ASR/VLM 风险融合

本期只部署 8B 主审；32B 复审未上线，调用将得到 501，管线保留 8B 结果并在报告中标注「待复审」。
