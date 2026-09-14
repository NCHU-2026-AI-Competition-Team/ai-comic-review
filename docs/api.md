# HTTP API 文档

本文档与当前后端实现（`backend/app/api/videos.py`、`backend/app/api/health.py`、`backend/app/schemas/`）逐端点核对，字段名与错误码均以代码为准。

- 基址示例一律使用 `http://127.0.0.1:8000`
- 接口前缀：`/api`
- `video_id` 必须是 UUID（大小写不敏感，响应中规范为带连字符的小写形式）
- 除另有说明外，错误响应体为 `{"detail": "<中文字符串>"}`
- FastAPI / Pydantic 对 JSON 请求体、查询枚举、缺失必填文件的校验失败时，`detail` 可能是错误对象数组（HTTP 422）

---

## POST /api/videos

上传单个视频，校验扩展名与采样参数后落盘原始文件与任务记录，并**同步**触发抽帧。成功返回 201。

抽帧参数优先级：**任务级覆盖 > 全局配置**（`FRAME_EXTRACTION_FPS` / `SCENE_THRESHOLD` / `SCENE_MAX_FRAMES`）。未提供覆盖字段时行为与旧客户端一致。

### 请求参数

`Content-Type: multipart/form-data`

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `file` | form | 文件 | 是 | 原始视频。扩展名仅允许 `.mp4` / `.mov` / `.mkv`（大小写不敏感） |
| `sampling` | form | 字符串 | 否 | 采样模式。`fixed_fps`（默认）或 `scene`。非法值 400 |
| `frame_fps` | form | 浮点 | 否 | 仅 `sampling=fixed_fps` 时有效，必须 `> 0`。缺省或空白回退 `FRAME_EXTRACTION_FPS`（默认 `2.0`） |
| `scene_threshold` | form | 浮点 | 否 | 仅 `sampling=scene` 时有效，必须落在开区间 `(0, 1)`。缺省或空白回退 `SCENE_THRESHOLD`（默认 `0.4`） |
| `scene_max_frames` | form | 整数 | 否 | 仅 `sampling=scene` 时有效，必须是 `> 0` 的整数（不允许 `1.5` 这类小数形式）。缺省或空白回退 `SCENE_MAX_FRAMES`（默认 `500`） |

与采样模式不匹配的字段组合（例如 `fixed_fps` 携带 `scene_threshold` / `scene_max_frames`，或 `scene` 携带 `frame_fps`）返回 422。非数、越界同样 422，错误信息为中文字符串。校验失败时不落盘文件与任务记录。

本接口响应模型为 `VideoUploadResponse`，**不含** `frame_fps` / `scene_threshold` / `scene_max_frames`；覆盖值写入任务记录，由 `GET /api/videos/{video_id}` 回显。`frames.sampling` 记录本次抽帧**实际生效**的 `fps` 或 `threshold`。

### 响应示例（201）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "filename": "clip.mp4",
  "status": "processed",
  "sampling": "fixed_fps",
  "metadata": {
    "duration": 2.0,
    "width": 320,
    "height": 240,
    "fps": 10.0,
    "codec": "h264",
    "bitrate": 1500000
  },
  "frames": {
    "sampling": {
      "method": "fixed_fps",
      "fps": 5.0,
      "threshold": null
    },
    "count": 2,
    "frames": [
      {
        "frame_id": "frame_000000",
        "timestamp_ms": 0,
        "timestamp": "00:00:00.000",
        "path": "/api/videos/12345678-1234-1234-1234-1234567890ab/frames/frame_000000.jpg"
      },
      {
        "frame_id": "frame_000001",
        "timestamp_ms": 200,
        "timestamp": "00:00:00.200",
        "path": "/api/videos/12345678-1234-1234-1234-1234567890ab/frames/frame_000001.jpg"
      }
    ]
  }
}
```

`status` 取值：`processed`（抽帧完成）/ `processing`（处理模块缺失时保持该状态）/ `failed`（处理异常时任务记为 failed，本接口返回 500）。`metadata` 与 `frames` 在尚未处理时为 `null`。

镜头模式时 `frames.sampling` 为：

```json
{
  "method": "scene_change",
  "fps": null,
  "threshold": 0.25
}
```

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/videos" \
  -F "file=@clip.mp4" \
  -F "sampling=fixed_fps" \
  -F "frame_fps=5"

curl -X POST "http://127.0.0.1:8000/api/videos" \
  -F "file=@clip.mp4" \
  -F "sampling=scene" \
  -F "scene_threshold=0.25" \
  -F "scene_max_frames=12"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | 扩展名不在 `.mp4` / `.mov` / `.mkv` | `不支持的视频格式 '{ext}'，仅允许 [...]` |
| 400 | `sampling` 不是 `fixed_fps` 或 `scene` | `不支持的采样模式 '{sampling}'，仅允许 [...]` |
| 413 | 文件超过 `MAX_UPLOAD_SIZE_MB`（默认 500） | `上传文件超过大小限制` |
| 422 | 缺少 `file`，或文件名为空 | FastAPI 校验错误数组 |
| 422 | `frame_fps` 非数、非有限值或 `<= 0` | `frame_fps 必须是大于 0 的数字` |
| 422 | `scene_threshold` 非数或未落在 `(0, 1)` | `scene_threshold 必须是 (0, 1) 区间内的数字` |
| 422 | `scene_max_frames` 非整数或 `<= 0` | `scene_max_frames 必须是大于 0 的整数` |
| 422 | `fixed_fps` 携带 `scene_threshold` | `scene_threshold 仅在 sampling=scene 时有效` |
| 422 | `fixed_fps` 携带 `scene_max_frames` | `scene_max_frames 仅在 sampling=scene 时有效` |
| 422 | `scene` 携带 `frame_fps` | `frame_fps 仅在 sampling=fixed_fps 时有效` |
| 500 | 抽帧 / 元数据解析失败 | `视频处理失败: ...`（任务 `status=failed`，`error` 为原始异常信息） |

---

## GET /api/videos/{video_id}

查询任务记录：状态、采样模式、任务级覆盖参数、元数据与抽帧结果。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 上传时返回的任务标识 |

### 响应示例（200）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "filename": "clip.mp4",
  "status": "processed",
  "sampling": "fixed_fps",
  "frame_fps": 5.0,
  "scene_threshold": null,
  "scene_max_frames": null,
  "error": null,
  "metadata": {
    "duration": 2.0,
    "width": 320,
    "height": 240,
    "fps": 10.0,
    "codec": "h264",
    "bitrate": 1500000
  },
  "frames": {
    "sampling": {
      "method": "fixed_fps",
      "fps": 5.0,
      "threshold": null
    },
    "count": 2,
    "frames": []
  },
  "created_at": "2026-09-15T12:00:00"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `sampling` | 上传时的采样模式：`fixed_fps` 或 `scene` |
| `frame_fps` | 任务级固定帧率覆盖；未提供时为 `null`，抽帧回退全局 `FRAME_EXTRACTION_FPS` |
| `scene_threshold` | 任务级镜头阈值覆盖；未提供时为 `null`，抽帧回退全局 `SCENE_THRESHOLD` |
| `scene_max_frames` | 任务级最大抽帧数覆盖；未提供时为 `null`，抽帧回退全局 `SCENE_MAX_FRAMES` |
| `error` | 处理失败时的错误信息，否则 `null` |
| `frames.sampling` | 实际抽帧使用的参数：`fixed_fps` 必有 `fps` 且 `threshold` 为 `null`；`scene_change` 必有 `threshold` 且 `fps` 为 `null` |
| `created_at` | 任务创建时间（ISO8601） |

### curl 示例

```bash
curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 500 | `job.json` 损坏或字段无法通过模型校验 | `任务记录文件损坏` |

---

## GET /api/videos/{video_id}/file

流式返回原始视频，支持**单区间** HTTP Range。响应 `Content-Type` 按后缀映射：`.mp4` → `video/mp4`，`.mov` → `video/quicktime`，`.mkv` → `video/x-matroska`，其他回退 `application/octet-stream`。

本接口不读取任务记录，只按 `video_id` 在上传目录枚举 `.mp4` / `.mov` / `.mkv`。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `Range` | header | 字符串 | 否 | 见下方三种单区间形式 |

### Range 三种形式

设文件大小为 `file_size`，解析结果为闭区间 `[start, end]`。

| 形式 | 示例 | 行为 |
| --- | --- | --- |
| `bytes=start-end` | `bytes=5-9` | 返回指定闭区间。`end >= file_size` 时裁剪到 `file_size-1`，仍返回 206 |
| `bytes=start-` | `bytes=5-` | 从 `start` 读到文件末尾 |
| `bytes=-suffix` | `bytes=-5` | 取文件末尾 `suffix` 字节。`suffix` 超过文件大小时返回全量（206，`bytes 0-{file_size-1}/{file_size}`） |

合法但不可满足（`start >= file_size`、`start > end`、`suffix <= 0`、空文件上的 Range）返回 **416**，并带响应头 `Content-Range: bytes */{file_size}`。

多区间（含逗号）或无法识别的畸形值（如 `bytes=abc-def`）**忽略 Range**，返回 200 全量，并带 `Accept-Ranges: bytes`。

无 `Range` 时返回 200 全量，响应头含 `Accept-Ranges: bytes`。

成功的区间响应为 206，响应头：

- `Content-Range: bytes {start}-{end}/{file_size}`
- `Accept-Ranges: bytes`
- `Content-Length: {end-start+1}`

本接口成功时响应体为二进制，不是 JSON。

### curl 示例

```bash
curl -O "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/file"

curl -H "Range: bytes=0-1023" \
  "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/file"

curl -H "Range: bytes=1024-" \
  "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/file"

curl -H "Range: bytes=-512" \
  "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/file"
```

### 错误码

| HTTP | 条件 | `detail` / 备注 |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 上传目录中找不到对应视频文件 | `视频文件不存在` |
| 416 | Range 合法但不可满足 | `Requested Range Not Satisfiable`；响应头 `Content-Range: bytes */{file_size}` |

---

## GET /api/videos/{video_id}/frames

读取抽帧清单 `storage/frames/{video_id}/frames.json`。不依赖任务记录是否存在。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |

### 响应示例（200）

```json
{
  "sampling": {
    "method": "fixed_fps",
    "fps": 2.0,
    "threshold": null
  },
  "count": 1,
  "frames": [
    {
      "frame_id": "frame_000000",
      "timestamp_ms": 0,
      "timestamp": "00:00:00.000",
      "path": "/api/videos/12345678-1234-1234-1234-1234567890ab/frames/frame_000000.jpg"
    }
  ]
}
```

`sampling.fps` / `sampling.threshold` 为本次抽帧实际生效值（任务覆盖或全局缺省）。

### curl 示例

```bash
curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/frames"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | `frames.json` 尚未生成 | `抽帧结果尚未生成` |
| 500 | `frames.json` 非法 JSON 或无法通过 `FramesInfo` 校验 | `抽帧结果文件损坏` |

---

## GET /api/videos/{video_id}/frames/{filename}

按文件名返回帧图片。`filename` 必须是纯文件名（不能含路径分隔），后缀仅允许 `.jpg` / `.jpeg` / `.png`。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `filename` | path | 字符串 | 是 | 帧文件名，例如 `frame_000000.jpg` |

成功时响应体为图片二进制（通常 `image/jpeg`），不是 JSON。

### curl 示例

```bash
curl -O "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/frames/frame_000000.jpg"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 文件不存在、后缀不允许、或路径不安全 | `帧图片不存在` |

---

## POST /api/videos/{video_id}/ocr

对已完成抽帧的视频**同步**执行 OCR，结果落盘 `storage/outputs/{video_id}/ocr.json`。当前实现**总是重新识别**，不复用已有结果。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `force` | query | 布尔 | 否 | 默认 `false`。与 ASR 接口对齐的保留参数；OCR **忽略该值**，始终重新识别 |

### 响应示例（200）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "modality": "ocr",
  "event_count": 1,
  "reused": false
}
```

`reused` 因 OCR 总是重跑而恒为 `false`。无文本帧不计入事件。

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/ocr"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 409 | 尚未生成 `frames.json` | `尚未完成抽帧，请先完成视频处理` |
| 500 | 任务记录损坏 | `任务记录文件损坏` |
| 500 | `frames.json` 损坏 | `帧清单文件损坏` |

---

## POST /api/videos/{video_id}/asr

对视频同步执行 ASR：必要时从原始视频提取音频，调用云端识别，结果落盘 `asr.json`。

幂等语义：默认若已有完好的 `asr.json` 则直接复用并标注 `reused=true`；`force=true` 才忽略已有结果重新识别。损坏的 `asr.json` 会被删除后重跑。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `force` | query | 布尔 | 否 | 默认 `false`。为 `true` 时忽略已有 `asr.json` 强制重跑 |

### 响应示例（200）

首次：

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "modality": "asr",
  "event_count": 2,
  "reused": false
}
```

再次调用且未带 `force=true`：

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "modality": "asr",
  "event_count": 2,
  "reused": true
}
```

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/asr"

curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/asr?force=true"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 409 | 找不到原始视频文件 | `找不到原始视频文件，请重新上传` |
| 409 | 视频不含音轨 | `视频不含音轨，无法执行语音识别` |
| 500 | 任务记录损坏 | `任务记录文件损坏` |
| 500 | 提取出的音频损坏或格式不符 | `音频文件损坏或格式不符` |
| 502 | 云端 ASR 服务异常或响应结构非法 | `云端语音识别服务异常` |
| 503 | 未配置云端 ASR 服务端点 | `未配置云端 ASR 服务端点` |
| 504 | 音频提取超时 | `音频提取超时` |
| 504 | 云端识别超时 | `云端语音识别超时` |

---

## POST /api/videos/{video_id}/review

对已完成抽帧的视频同步执行 VLM 主审，读取已落盘的 OCR / ASR 作为文本上下文（缺失则留空），结果落盘 `vlm.json`。

幂等语义：默认复用已有完好的 `vlm.json` 并标注 `reused=true`；`force=true` 才重新审核。

高风险 / 低置信 / JSON 不合格 / 模态冲突时会尝试 32B 复审。32B 未部署或复审失败时**不中断主链路**：保留 8B 结果，`needs_escalation=true`，`escalation_status=pending_review`。

`escalation_status` 取值：`not_needed` / `pending_review` / `completed`。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `force` | query | 布尔 | 否 | 默认 `false`。为 `true` 时忽略已有 `vlm.json` 强制重跑 |

### 响应示例（200）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "modality": "vlm",
  "event_count": 1,
  "reused": false,
  "needs_escalation": true,
  "escalation_status": "pending_review"
}
```

复用已有结果时 `reused` 为 `true`，其余字段按已落盘事件汇总。

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/review"

curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/review?force=true"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 409 | 尚未生成 `frames.json` | `尚未完成抽帧，请先完成视频处理` |
| 500 | 任务记录损坏 | `任务记录文件损坏` |
| 500 | `frames.json` 损坏 | `帧清单文件损坏` |
| 502 | 云端 VLM 服务异常或响应结构非法 | `云端视觉审核服务异常` |
| 503 | 未配置云端 VLM 服务端点 | `未配置云端 VLM 服务端点` |
| 504 | 云端视觉审核超时 | `云端视觉审核超时` |

---

## GET /api/videos/{video_id}/events

读取已生成的模态时间线事件。本接口**不查询任务记录**，只读取 `storage/outputs/{video_id}/{modality}.json`。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `modality` | query | 字符串 | 否 | 事件来源模态，默认 `ocr`。允许值：`ocr` / `asr` / `vision` / `vlm` |

当前流水线会产出 `ocr` / `asr` / `vlm`；`vision` 在枚举中保留，尚无对应生成器。非法 `modality` 由 FastAPI 返回 422（`detail` 为校验错误数组）。

### 响应示例（200）

OCR：

```json
[
  {
    "id": "ocr-frame_000000",
    "video_id": "12345678-1234-1234-1234-1234567890ab",
    "modality": "ocr",
    "start_ms": 0,
    "end_ms": 0,
    "content": "台词一\n台词二",
    "confidence": 0.9,
    "metadata": {
      "frame_id": "frame_000000",
      "lines": [
        {
          "text": "台词一",
          "confidence": 0.95,
          "box": [[0, 0], [10, 0], [10, 10], [0, 10]]
        }
      ]
    }
  }
]
```

ASR 事件 `metadata` 含 `language`、`segment_index`。VLM 事件 `metadata` 含 `risk`、`category`、`severity`、`evidence`、`reason`、`suggestion`、`needs_escalation`、`escalation_status`、`escalation_reasons`、`model`、`batch_index`、`review_start_ms`、`review_end_ms`、`frame_ids`。

### curl 示例

```bash
curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/events?modality=ocr"

curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/events?modality=asr"

curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/events?modality=vlm"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 对应模态结果文件不存在 | `事件尚未生成` |
| 422 | `modality` 不在允许枚举中 | FastAPI 校验错误数组 |
| 500 | 文件损坏、字段校验失败，或文件内 `video_id` / `modality` 与请求不一致 | `事件文件损坏` |

---

## GET /api/videos/{video_id}/report

读取结构化审核报告。需要任务记录存在，且已生成 `vlm.json`。`risk_events` 元素类型与时间线事件相同（`TimelineEvent`），按 `metadata.severity` 降序（high > medium > low > none）。

`verdict`：尚未提交人工复核时为 `null`；提交后为 `HumanVerdict`（不含内部字段 `updated_at`）。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |

### 响应示例（200）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "filename": "clip.mp4",
  "status": "processed",
  "sampling": "fixed_fps",
  "metadata": {
    "duration": 2.0,
    "width": 320,
    "height": 240,
    "fps": 10.0,
    "codec": "h264",
    "bitrate": 1500000
  },
  "modalities": {
    "ocr": {"ran": false, "event_count": 0},
    "asr": {"ran": false, "event_count": 0},
    "vlm": {"ran": true, "event_count": 1}
  },
  "risk_events": [
    {
      "id": "vlm-0000-frame_000000",
      "video_id": "12345678-1234-1234-1234-1234567890ab",
      "modality": "vlm",
      "start_ms": 0,
      "end_ms": 0,
      "content": "第1帧含暴力",
      "confidence": 0.95,
      "metadata": {
        "risk": true,
        "category": "violence",
        "severity": "high",
        "evidence": "第1帧含暴力",
        "reason": "画面冲突",
        "suggestion": "拦截",
        "needs_escalation": true,
        "escalation_status": "pending_review",
        "escalation_reasons": [],
        "model": "qwen3-vl-8b-instruct",
        "batch_index": 0,
        "review_start_ms": 0,
        "review_end_ms": 500,
        "frame_ids": ["frame_000000"]
      }
    }
  ],
  "overall": {
    "risk": true,
    "category": "violence",
    "severity": "high",
    "confidence": 0.95,
    "suggestion": "拦截",
    "reason": "画面冲突",
    "needs_escalation": true,
    "escalation_status": "pending_review"
  },
  "verdict": null
}
```

`overall.risk` 为布尔值。`overall.escalation_status` 取值：`not_needed` / `pending_review` / `completed`。`modalities` 固定包含 `ocr` / `asr` / `vlm` 三个键，`ran` 表示对应结果文件是否已落盘且可读。

提交复核后 `verdict` 示例：

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "decision": "false_positive",
  "note": "误报",
  "event_id": null,
  "created_at": "2026-09-14T10:00:00Z"
}
```

### curl 示例

```bash
curl "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/report"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 404 | `vlm.json` 不存在 | `审核报告尚未生成` |
| 500 | 任务记录损坏 | `任务记录文件损坏` |
| 500 | `vlm.json` 损坏或与请求不一致 | `审核结果文件损坏` |

---

## POST /api/videos/{video_id}/verdict

提交或覆盖人工复核决策，落盘 `storage/outputs/{video_id}/verdict.json`。重复提交保留首次 `created_at`，刷新内部 `updated_at`；**HTTP 响应不含 `updated_at`**。不要求审核报告已生成，但任务记录必须存在。

### 请求参数

| 名称 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `video_id` | path | UUID 字符串 | 是 | 任务标识 |
| `decision` | JSON body | 字符串 | 是 | `approve` / `reject` / `false_positive` |
| `note` | JSON body | 字符串 | 否 | 复核备注，默认空串 |
| `event_id` | JSON body | 字符串或 `null` | 否 | 针对单条事件；`null` 表示针对整体结论 |

`Content-Type: application/json`。

### 响应示例（200）

```json
{
  "video_id": "12345678-1234-1234-1234-1234567890ab",
  "decision": "approve",
  "note": "",
  "event_id": null,
  "created_at": "2026-09-14T10:00:00Z"
}
```

### curl 示例

```bash
curl -X POST "http://127.0.0.1:8000/api/videos/12345678-1234-1234-1234-1234567890ab/verdict" \
  -H "Content-Type: application/json" \
  -d "{\"decision\":\"approve\",\"note\":\"\",\"event_id\":null}"
```

### 错误码

| HTTP | 条件 | `detail` |
| --- | --- | --- |
| 400 | `video_id` 不是合法 UUID | `video_id 格式非法` |
| 404 | 任务记录不存在 | `视频不存在` |
| 422 | `decision` 不在允许枚举，或 JSON 体无法通过模型校验 | FastAPI 校验错误数组 |
| 500 | 任务记录损坏 | `任务记录文件损坏` |

---

## GET /api/health

健康检查，不依赖存储或外部服务。

### 请求参数

无。

### 响应示例（200）

```json
{
  "status": "ok"
}
```

### curl 示例

```bash
curl "http://127.0.0.1:8000/api/health"
```

### 错误码

无业务错误码。进程存活即返回 200。
