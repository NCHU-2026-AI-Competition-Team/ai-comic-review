# Modal 云端 ASR 部署

把 Qwen3-ASR 转写与 Qwen3-ForcedAligner 强制对齐部署到 Modal GPU，提供与本地 `QwenAsrProvider` 一致的 HTTPS 契约。

本地业务（`ai/asr/qwen_asr.py`）只上传 16kHz 单声道 PCM wav，不加载权重。

## 模型核实（HuggingFace，2026-09-14）

禁止静默替换。以下 repo 均存在且为 Apache-2.0：

| 用途 | 确切 repo id | 体量 | 推理 |
| --- | --- | --- | --- |
| 主转写 | [`Qwen/Qwen3-ASR-1.7B`](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | 约 1.77B，权重约 3.5GB safetensors | 官方 `qwen-asr`：`Qwen3ASRModel.from_pretrained` |
| 时间戳对齐 | [`Qwen/Qwen3-ForcedAligner-0.6B`](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | 约 0.60B，权重约 1.2GB safetensors | 同上 `forced_aligner=` 参数，或 `Qwen3ForcedAligner` |

说明：HuggingFace 另有 `Qwen/Qwen3-ASR-1.7B-hf` 与 `Qwen/Qwen3-ForcedAligner-0.6B-hf`（Transformers 原生封装）。本服务使用官方原版 repo + `qwen-asr`，不替换为 `-hf` 变体。

`qwen-asr` 在 `return_time_stamps=True` 时由 ForcedAligner 产出 `start_time`/`end_time`（秒），服务端再转为契约要求的 `start_ms`/`end_ms`（int 毫秒）。模型不输出置信度，有文本分段时 `confidence` 固定为 `1.0`。

## 服务契约

`POST {BASE_URL}/transcribe`

- multipart 字段 `file`：16kHz 单声道 PCM wav
- 表单字段 `model`：模型标识，接受 `Qwen3-ASR-1.7B`（本地 Provider 默认值）或 `Qwen/Qwen3-ASR-1.7B`
- 成功 JSON：

```json
{
  "segments": [
    {"text": "你好。", "start_ms": 0, "end_ms": 800, "confidence": 1.0}
  ],
  "language": "zh"
}
```

- 空音频 / 无语音：`200`，`segments` 为空列表，`language` 仍为 `zh`（或检测到的语言）
- 畸形输入（缺字段、非 wav、非 16kHz 单声道 PCM、空文件）：明确 `4xx`，JSON `{"error": "..."}`
- 额外提供 `GET /health` 便于探活，本地 Provider 不依赖它

## 前置

1. 安装并登录 Modal CLI：

```powershell
pip install -r modal/requirements.txt
modal token new
```

2. HuggingFace Secret（公开 Apache-2.0 模型也可下载，Secret 用于提高 Hub 限额）：

```powershell
modal secret create huggingface-secret HF_TOKEN=hf_xxx
```

禁止把 token 写入仓库或 `.env.example`。本仓库部署账号已有 `huggingface-secret`。

3. GPU 默认优先 `A10G`，配额不足时回退 `T4`。T4 无原生 bf16，服务会自动改用 fp16。

## 部署

在仓库根目录：

```powershell
modal deploy modal/asr_service.py
```

成功后会打印 HTTPS 地址，形如：

```
https://<workspace>--ai-comic-review-asr.modal.run
```

把该地址写入本机 `.env` 的 `MODAL_ASR_URL`（不要提交 `.env`，`.env.example` 保持空值占位）。

权重缓存在 Modal Volume `huggingface-cache`（挂载 `/root/.cache/huggingface`），重复冷启动不会重新下载整包权重。

停止：

```powershell
modal app stop ai-comic-review-asr
```

## 成本注意

| 项目 | 说明 |
| --- | --- |
| GPU | A10G 约 $1.10/h；T4 约 $0.59/h |
| 空闲 | `scaledown_window=300s`，无请求约 5 分钟后缩到 0 |
| 并发 | `max_containers=1`，同时最多一张 GPU |
| 冷启动 | 首次拉起需把约 5GB 权重装上 GPU，可能数分钟；Volume 命中后会快很多 |
| 本地超时 | 后端 `ASR_REQUEST_TIMEOUT_SECONDS` 默认 120s，冷启动可能不够，先用 `GET /health`（`curl -L`）把容器拉起 |

不要把此服务的 GPU 规格升到 A100/H100，1.7B+0.6B 无必要。

冷启动时 Modal 可能先返回 `303` 并带 `__modal_attempt_token`，请用 `curl -L` 跟随跳转。

## 端点验证

将 `$BASE` 换成部署输出的 URL。冒烟 wav 只生成在 `modal/` 下，已 gitignore，不要提交。

```powershell
$BASE = "https://<workspace>--ai-comic-review-asr.modal.run"

# 1) 探活（冷启动请加 -L，超时拉长）
curl.exe -sS -L --max-time 1800 "$BASE/health"

# 2) 合法静音 wav：应返回空 segments
python -c "import wave; w=wave.open('modal/silence_16k.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x00\x00'*16000); w.close()"
curl.exe -sS -L -F "file=@modal/silence_16k.wav;type=audio/wav" -F "model=Qwen3-ASR-1.7B" "$BASE/transcribe"

# 3) 440Hz 正弦（ffmpeg）：无语音，契约仍应是空 segments + language
ffmpeg -y -f lavfi -i sine=frequency=440:duration=2 -ac 1 -ar 16000 -f wav modal/tone_16k.wav
curl.exe -sS -L -F "file=@modal/tone_16k.wav;type=audio/wav" -F "model=Qwen3-ASR-1.7B" "$BASE/transcribe"

# 4) 畸形输入：非 wav，应 4xx
curl.exe -sS -L -F "file=@modal/README.md;type=audio/wav" -F "model=Qwen3-ASR-1.7B" "$BASE/transcribe"
```

### 实测记录（2026-09-14）

- 部署命令：`modal deploy modal/asr_service.py`
- 工作区：`jadecake5`
- App：`ai-comic-review-asr`
- **HTTPS 端点**：`https://jadecake5--ai-comic-review-asr.modal.run`
- GPU：NVIDIA A10（bf16），权重 Volume：`huggingface-cache`，Secret：`huggingface-secret`
- `.env.example` 的 `MODAL_ASR_URL` 保持空值，未写入真实 URL

| 请求 | 结果 |
| --- | --- |
| `GET /health` | `200` `{"status":"ok","asr_repo":"Qwen/Qwen3-ASR-1.7B","aligner_repo":"Qwen/Qwen3-ForcedAligner-0.6B"}` |
| 1s 静音 wav `/transcribe` | `200` `{"segments":[],"language":"zh"}`（约 33s，含首次推理预热） |
| 2s 440Hz 正弦 `/transcribe` | `200` `{"segments":[],"language":"zh"}` |
| 中文 TTS wav「你好，这是语音识别测试。」 | `200` `{"segments":[{"text":"你好，这是语音识别测试。","start_ms":0,"end_ms":4279,"confidence":1.0}],"language":"zh"}` |
| 上传 `README.md` 冒充 wav | `400` `{"error":"不是合法的 wav 音频：file does not start with RIFF id"}` |
| `model=whisper-large` | `400` `{"error":"不支持的模型标识 'whisper-large'，期望 Qwen3-ASR-1.7B"}` |
| 缺少 `file` | `400` `{"error":"缺少 multipart 字段 file（16kHz 单声道 PCM wav）"}` |
| 8kHz wav | `400` `{"error":"音频采样率非法：8000Hz，期望 16000Hz"}` |

语音样本由本机 Windows SAPI（Microsoft Huihui Desktop）合成后再经 ffmpeg 转为 16kHz 单声道 PCM。返回结构满足 Provider 契约：`segments[].text/start_ms/end_ms/confidence` + `language`，毫秒为 int。

## 文件说明

```
modal/
├── asr_service.py    # Modal App：镜像、GPU、Volume、HTTPS /transcribe
├── asr_logic.py      # 纯逻辑（wav 校验、语言映射、时间戳转分段），供单测
├── requirements.txt  # 仅本地 Modal CLI
└── README.md
```

`modal/` 不是 Python 包（无 `__init__.py`），避免与 Modal SDK 同名冲突。本地测试通过文件路径加载 `asr_logic.py`，见 `tests/test_modal_asr_logic.py`。
