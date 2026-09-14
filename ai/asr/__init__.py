"""ASR 模态：语音识别。

base 定义 AsrProvider 抽象，qwen_asr 提供 Modal 云端 Qwen3-ASR 的
HTTPS 客户端实现（主 ASR），factory 提供默认 Provider 入口
get_default_provider（按配置选择实现，业务层只依赖抽象与工厂，
不 import 具体实现模块）。模型标识与服务端点全部走配置
（asr_primary_model / modal_asr_url），本地不加载任何模型权重。
"""
