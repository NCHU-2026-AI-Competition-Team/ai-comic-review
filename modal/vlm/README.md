# modal/vlm —— VLM 主审服务（Modal GPU 云端）

把 Qwen3-VL-8B-Instruct 主审模型部署到 Modal GPU 云端，对视频关键帧 +
OCR/ASR 文本上下文做内容安全审核，输出严格 JSON 契约。

- 模型：`Qwen/Qwen3-VL-8B-Instruct`（HuggingFace 核实：repo id 一致，
  license Apache-2.0，非 gated，原生 256K 上下文， transformers 主线支持）
- 推理引擎：vLLM v0.11.0（该版本起原生支持 Qwen3-VL 系列），
  结构化输出（JSON Schema guided decoding）保证返回格式
- 精度：BF16 基线。审核系统 Recall 优先，禁止为省显存直接上 INT8/INT4
- GPU：L40S（48GB，约 $1.95/h，价格参考见文末）。A10G（22GB）实测 OOM：
  BF16 权重 17.7GB + vLLM 多帧 ViT profiling 峰值 4.6GB 超出容量，
  精度纪律禁止靠 INT8/INT4 省显存，故按实际需求升级
- 32B 复审模型本期**不部署**（用户决定，工期原因），接口已预留
  escalation 模型位：收到 32B 标识返回明确 501

## 文件

| 文件 | 作用 |
| --- | --- |
| `serve.py` | Modal app：镜像构建、GPU 规格、权重 Volume 缓存、FastAPI web 端点 |
| `review_contract.py` | 纯逻辑契约：模型路由、输入校验、输出 JSON Schema、prompt 构建、结果归一化（仅标准库，tests 直接复用） |

## 部署

前置：本机已装并登录 Modal CLI（`modal token new`）。模型公开非 gated，
**无需 HuggingFace token**。

```bash
cd modal/vlm
# Windows 下必须加 PYTHONUTF8=1，否则构建日志里的特殊字符会触发 GBK 编码错误
PYTHONUTF8=1 modal deploy serve.py
```

部署完成后查看端点：

```bash
modal app list
# 端点形如 https://<workspace>--ai-comic-review-vlm-vlmreviewer-web.modal.run
```

首次冷启动会把约 16GB 权重下载进 Volume `ai-comic-review-vlm-hf-cache`
（已启用 hf_transfer 加速），之后启动直接命中缓存。空闲 5 分钟自动缩容
到 0，不产生 GPU 费用；再次请求的冷启动约 1~2 分钟。

停止服务：

```bash
modal app stop ai-comic-review-vlm
```

## HTTPS 接口契约

### POST /review（multipart/form-data）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `frames` | 文件 ×N | 关键帧图片，1~8 帧，单帧 ≤10MB |
| `model` | 文本 | 模型标识，默认/8B 别名走主审；`qwen3-vl-32b-instruct` 等 32B 标识返回 501（escalation 预留，未部署） |
| `ocr_text` | 文本 | 画面文字 OCR 结果（可空，≤2 万字符） |
| `asr_text` | 文本 | 语音转写文本（可空，≤2 万字符） |
| `context` | 文本 | 剧集/场景等上下文（可空） |
| `rules` | 文本 | 审核规则描述（可空，缺省用通用内容安全规范） |
| `frame_timestamps_ms` | 文本 | JSON 整数数组，各帧时间戳毫秒，长度须等于帧数；缺省按 0 起等差 500ms |

### 输出（200，严格 JSON）

```json
{
  "risk": true,
  "category": "blood",
  "severity": "high",
  "confidence": 0.92,
  "start_ms": 500,
  "end_ms": 1000,
  "evidence": "第2帧画面含持刀刺击与鲜血文字",
  "reason": "画面与字幕均指向暴力血腥内容",
  "suggestion": "拦截"
}
```

- `category` 枚举：`none / porn / violence / blood / politics / illegal / abuse / privacy / other`
- `severity` 枚举：`none / low / medium / high`
- `confidence` 为 0~1 数值；`start_ms` / `end_ms` 为非负整数毫秒
- 无风险时 `risk=false`，且 `category`/`severity` 归一化为约定空值
  `none`，`start_ms`/`end_ms` 归零
- 模型输出由 vLLM 结构化输出约束；仍不合规时自动重试一次，再失败返回
  502；输入畸形返回 400/422；32B 标识返回 501

### GET /health

返回模型标识、GPU 规格与 escalation 部署状态，用于存活探活。

## curl 验证示例

```bash
ENDPOINT="https://<workspace>--ai-comic-review-vlm-vlmreviewer-web.modal.run"

# 多帧上传 + OCR/ASR 文本上下文
curl -sS "$ENDPOINT/review" \
  -F "frames=@frame1.png" \
  -F "frames=@frame2.png" \
  -F "frames=@frame3.png" \
  -F "model=qwen3-vl-8b-instruct" \
  -F "frame_timestamps_ms=[0, 500, 1000]" \
  -F "ocr_text=他拿起刀刺向对方，鲜血直流" \
  -F "asr_text=你给我去死吧" \
  -F "context=漫剧第3集 校园场景" \
  -F "rules=严查血腥暴力与色情内容"

# escalation 预留位：应返回 501
curl -sS -o /dev/null -w "%{http_code}\n" "$ENDPOINT/review" \
  -F "frames=@frame1.png" -F "model=qwen3-vl-32b-instruct"
```

## 成本估算（参考 modal价格层级.md）

| GPU | 单价 | 说明 |
| --- | --- | --- |
| L40S 48GB（本期选择） | ~$1.95/h | 8B BF16 权重约 16GB + 多帧 ViT 峰值 + KV cache，余量充足 |
| A10G 24GB | ~$1.10/h | 实测 OOM：权重 17.7GB + vLLM profiling 峰值 4.6GB 超出 22GB 容量 |
| A100-80GB | ~$2.50/h | 未来 32B 复审（BF16 约 65GB 权重）的起步规格 |

按量计费 + 空闲 5 分钟缩容到 0：审核任务为间歇性负载，实际费用约为
`单次审核秒数 × 次数 × $1.95/3600`，冷启动（约 1~2 分钟）也计入。

## 实测记录

端点：`https://jadecake5--ai-comic-review-vlm-vlmreviewer-web.modal.run`
（2026-09-14 真实 `modal deploy` 部署并 curl 验证）

引擎基线（L40S 48GB，BF16，enforce_eager，max_model_len=8192）：

- 权重显存约 17.4GB；vLLM 可用 KV cache 16.56GB（约 12 万 tokens）
- 冷启动（容器拉起 + 引擎加载，权重已命中 Volume 缓存）：约 67 秒

| 用例 | 结果 | 耗时 |
| --- | --- | --- |
| GET /health | 200，返回模型/GPU/escalation 状态 | 冷启动 67s |
| /review 风险场景：3 帧 + OCR「他拿起刀刺向对方，鲜血直流」+ ASR 威胁台词 | 200，`risk=true, category=violence, severity=high, confidence=0.95, start_ms=500, end_ms=1000`（int 毫秒），evidence 正确指向第 2 帧 OCR | 12.5s（首次推理） |
| /review 安全场景：2 帧 + 日常 OCR/ASR | 200，`risk=false` 且 `category/severity=none`、`start_ms/end_ms=0`（归一化生效），confidence=0.99 | 4.5s |
| /review model=qwen3-vl-32b-instruct | 501，明确提示 escalation 未部署 | <1s |
| /review model=未知标识 | 400 | <1s |
| /review 非图片文件 | 422 | <1s |
| /review 缺 frames 字段 | 422（FastAPI 缺参校验） | <1s |

部署过程中的两个真实故障与修复（如实记录）：

1. `transformers>=4.57.0` 未卡上限装到 5.17.0，v5 删除了
   `all_special_tokens_extended`，与 vLLM 0.11.0 不兼容导致 crash-loop；
   修复为 `transformers>=4.57.0,<5`。
2. A10G（22GB）引擎 profiling 阶段 CUDA OOM：BF16 权重 17.36GB +
   多帧 ViT profiling 单次申请 4.62GB 超出容量；按精度纪律不做 INT8/INT4
   降级，升级到 L40S（48GB）后正常。
