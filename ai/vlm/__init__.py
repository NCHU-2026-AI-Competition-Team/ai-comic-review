"""VLM 模态：视觉语言大模型内容安全主审。

base 定义 VlmProvider 抽象与异常，schemas 提供严格 JSON 响应模型，
qwen 提供 Modal 云端 Qwen3-VL 的 HTTPS 客户端实现（主审 8B），
factory 提供默认 Provider 入口 get_default_provider（按 VLM_PROVIDER 分派，
业务层只依赖抽象与工厂，不 import 具体实现模块）。
模型标识与服务端点全部走配置（vlm_primary / modal_vlm_url），本地不加载权重。
32B 复审为 escalation 预留：未部署时保留 8B 结果并标注待复审。
"""
