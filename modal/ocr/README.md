# Modal OCR 服务（PP-OCRv6 + PaddleOCR-VL-1.6）

在 [Modal](https://modal.com) 上部署画面文字识别服务，供本仓库 `ai/ocr/remote.py` 的 `RemoteOcrProvider` 以 HTTPS 调用。

主模型为 **PP-OCRv6**（T4 GPU，经济规格）；**PaddleOCR-VL-1.6** 作为同容器独立方法 `recognize_fallback`，仅在已检出文字且最低置信度低于 `0.6` 时调用，或由请求显式指定 `model=PaddleOCR-VL-1.6`。VL 本地推理后端使用 paddleocr 3.7 的 `vl_rec_backend="native"`（官方文档旧称 `paddle`；T4 不用 `vllm-server`）。

## 模型来源与 License（已核实，禁止静默替换）

| 配置标识 | 官方获取方式 | License |
| --- | --- | --- |
| `PP-OCRv6` | PaddleOCR 3.x `PaddleOCR(ocr_version="PP-OCRv6")`，由 PaddleX 拉取流水线权重。HuggingFace 对应拆分仓库：[`PaddlePaddle/PP-OCRv6_mobile_det`](https://huggingface.co/PaddlePaddle/PP-OCRv6_mobile_det)、[`PaddlePaddle/PP-OCRv6_mobile_rec`](https://huggingface.co/PaddlePaddle/PP-OCRv6_mobile_rec)（另有 medium/server 变体）。**不是**单一 `PP-OCRv6` repo；`ocr_version` 官方映射为 `PP-OCRv6_mobile`，本服务不改成 medium/server。 | Apache-2.0（HF 模型卡） |
| `PaddleOCR-VL-1.6` | HuggingFace [`PaddlePaddle/PaddleOCR-VL-1.6`](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)；VLM 组件 [`PaddlePaddle/PaddleOCR-VL-1.6-0.9B`](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-0.9B)。Python API 必须 `PaddleOCRVL(pipeline_version="v1.6")`。**默认 `PaddleOCRVL()` 是 v1.5**，禁止静默落到 1.5。T4 上使用 `vl_rec_backend="paddle"`（vLLM 要求 SM80+，T4 为 SM75）。 | Apache-2.0（仓库 `LICENSE`） |

权重写入 Modal Volume `ai-comic-ocr-cache`（`PADDLEX_HOME` + HuggingFace Hub 缓存），冷启动后不再重复下载。

已知契约差异：PaddleOCR-VL 主输出为文档解析 `parsing_res_list`（`block_content` / `block_bbox`），不是 PP-OCRv6 的 `rec_texts/rec_scores/rec_polys`。服务端映射为 `OcrTextLine` JSON；VL 块若无置信度则填 `1.0`。

## HTTPS 契约

`POST /recognize`

- multipart 字段 `file`：JPEG 帧
- 表单字段 `model`：`PP-OCRv6` 或 `PaddleOCR-VL-1.6`
- 成功：`{"lines":[{"text": "...", "bbox": [[x,y],...], "confidence": 0.0~1.0}]}`
- 空图/无文字：`{"lines":[]}`（HTTP 200）
- 畸形输入（空文件、非 JPEG、未知 model、缺字段）：HTTP 4xx

另有 `GET /health`。

## 部署步骤

1. 本机已登录 Modal（`modal token new`）。本项目使用 profile `jadecake5`。
2. 确认 secret（公开模型也可工作，secret 用于 HuggingFace 限流）：

```bash
modal secret list
# 需要 huggingface-secret（已有则跳过）
# modal secret create huggingface-secret HF_TOKEN=hf_xxx
```

3. 在仓库根目录部署：

```bash
modal deploy modal/ocr/serve.py
```

部署成功后会打印 ASGI URL，形如：

```
https://jadecake5--ai-comic-review-ocr-ocrservice-web.modal.run
```

把该 URL（不含路径）写入本地 `.env` 的 `MODAL_OCR_URL`，并将 `OCR_PROVIDER=modal`。**不要**把真实 URL 写进 `.env.example`。

4. 停止：

```bash
modal app stop ai-comic-review-ocr
```

## curl 验证

把 `$MODAL_OCR_URL` 换成部署输出的 HTTPS 地址。工作区无现成视频帧时，可用下面命令生成一张带字 JPEG 再调用。

```bash
python -c "from PIL import Image, ImageDraw, ImageFont; im=Image.new('RGB',(640,200),(255,255,255)); d=ImageDraw.Draw(im); d.text((30,70),'HELLO 2026',fill=(0,0,0)); im.save('ocr-test.jpg','JPEG')"

curl -sS -X POST "$MODAL_OCR_URL/recognize" \
  -F "file=@ocr-test.jpg;type=image/jpeg" \
  -F "model=PP-OCRv6"

# 畸形输入应 4xx
curl -sS -o - -w "\nHTTP %{http_code}\n" -X POST "$MODAL_OCR_URL/recognize" \
  -F "file=@ocr-test.jpg;type=image/jpeg" \
  -F "model=not-a-model"

# 空图应 200 且 lines=[]
python -c "from PIL import Image; Image.new('RGB',(320,120),(255,255,255)).save('ocr-blank.jpg','JPEG')"
curl -sS -X POST "$MODAL_OCR_URL/recognize" \
  -F "file=@ocr-blank.jpg;type=image/jpeg" \
  -F "model=PP-OCRv6"
```

显式走 VL 兜底端点（更慢、更贵）：

```bash
curl -sS -X POST "$MODAL_OCR_URL/recognize" \
  -F "file=@ocr-test.jpg;type=image/jpeg" \
  -F "model=PaddleOCR-VL-1.6"
```

## 成本注意

- GPU：T4 × 1，约 $0.59/小时（按容器运行时间计费，以 Modal 账单为准）。
- `scaledown_window=300`：空闲 5 分钟后缩容，避免空转。
- 首次冷启动会下载 PP-OCRv6 权重，可能数分钟；VL-1.6 仅低置信或显式调用时再加载，首次更慢。
- 不要对无文字帧走 VL（服务端已禁止：空结果不兜底），以免 VLM 幻觉与额外费用。
- 并发限制为 1，适合审核流水线逐帧调用，不适合高 QPS。

## 实测记录

工作区 `storage/frames` 无现成视频帧，使用与本地引擎测试相同的带字 JPEG（`HELLO 2026`）验证契约。

- 部署命令：`modal deploy modal/ocr/serve.py`
- App：`ai-comic-review-ocr`（profile `jadecake5`）
- 端点 URL：`https://jadecake5--ai-comic-review-ocr-ocrservice-web.modal.run`
- `GET /health`：`{"status":"ok","primary":"PP-OCRv6","fallback":"PaddleOCR-VL-1.6"}`
- `POST /recognize` `model=PP-OCRv6` 带字 JPEG（200）：

```json
{"lines":[{"text":"HELLO 2026","bbox":[[28.0,65.0],[403.0,65.0],[403.0,122.0],[28.0,122.0]],"confidence":0.9998272061347961}]}
```

- 空图（200）：`{"lines":[]}`
- 未知 model（400）：`不支持的模型标识 'not-a-model'`
- 非 JPEG（400）：`仅接受 JPEG 图片`
- `model=PaddleOCR-VL-1.6`（200，首次加载约 25s）：

```json
{"lines":[{"text":"HELLO 2026","bbox":[[28.0,66.0],[405.0,66.0],[405.0,123.0],[28.0,123.0]],"confidence":1.0}]}
```

VL 块无逐行置信度，服务端按契约填 `1.0`。
