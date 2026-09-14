# AI Comic Review

AI 漫画/视频内容审核系统（开发中）。当前阶段已实现：上传视频后自动解析元数据（时长、分辨率、帧率、编码、码率），按固定帧率（默认 2 FPS）或镜头切换检测抽取关键帧，并对帧图片执行 OCR（PaddleOCR PP-OCRv6）产出统一时间线事件，前端页面展示视频信息、帧列表与识别结果。后续将在此基础上接入 ASR、视觉理解（VLM）与多模态风险融合判定，完成对漫画/视频内容的自动化审核。

## 功能

- 视频上传：支持 `.mp4` / `.mov` / `.mkv`，单个文件，大小上限默认 500 MB（可配置）
- 元数据解析：基于 ffprobe 提取时长、分辨率、帧率、编码格式、码率
- 关键帧抽取：基于 ffmpeg 按固定帧率抽帧，生成帧图片与 `frames.json` 帧清单（含毫秒级时间戳）
- 镜头切换检测：可选 scene 采样模式，基于 ffmpeg 场景分数识别镜头边界并逐边界精确抽帧（阈值与帧数上限可配置）
- 画面文字识别（OCR）：基于 PaddleOCR（主模型 PP-OCRv6）逐帧识别文字，聚合为统一时间线事件（TimelineEvent）并落盘 `ocr.json`；前端一键触发并展示识别结果
- 状态查询：查询视频任务状态（processing / processed / failed）、帧清单与帧图片
- 前端页面：拖拽/点选上传、采样模式选择、处理状态轮询、元数据与帧网格展示、OCR 事件列表

## 技术栈

- 后端：Python 3.11、FastAPI、Uvicorn、Pydantic / pydantic-settings、FFmpeg（ffmpeg / ffprobe 命令行）
- AI：PaddleOCR（主 OCR 模型 PP-OCRv6，CPU 版 paddlepaddle；模型权重首次运行自动下载到用户缓存目录，不进入仓库）+ torch（OCR 链路显式依赖，Windows 下须先于 paddle 加载）
- 前端：React 18、TypeScript、Vite 5
- 测试：pytest、FastAPI TestClient（httpx）
- 部署：docker-compose（后端服务）

## 目录结构

```
├── ai/                     # AI 模块（多模态能力，Provider 抽象 + 配置驱动）
│   ├── asr/                #   语音转写（占位）
│   ├── ocr/                #   画面文字识别：base.py Provider 抽象 + paddleocr.py PP-OCRv6 实现 + factory.py 默认 Provider 入口
│   ├── risk/               #   多模态风险融合判定（占位）
│   ├── video/              #   视频元数据解析与抽帧（占位，当前由后端 services/video 承担）
│   └── vlm/                #   视觉语言大模型理解（占位）
├── backend/
│   ├── app/
│   │   ├── api/            # 路由层：health.py、videos.py（上传/查询/OCR/事件）
│   │   ├── core/           # 配置：config.py（pydantic-settings）
│   │   ├── schemas/        # 数据模型：video.py、events.py（统一时间线事件）
│   │   ├── services/       # 业务层：video/（ffprobe/抽帧/镜头检测）、ocr_pipeline.py（OCR 编排）、registry.py
│   │   └── main.py         # FastAPI 应用入口
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/videos.ts   # 后端 API 封装
│       ├── components/     # UploadPanel / VideoInfoPanel / FramesGrid / OcrPanel
│       └── App.tsx         # 页面状态机（上传 → 处理 → 展示）
├── storage/                # 运行时数据（见下文）
│   ├── uploads/            #   原始视频与任务记录 job.json
│   ├── frames/             #   抽帧结果 frames/{video_id}/
│   └── outputs/            #   审核产出 outputs/{video_id}/ocr.json
├── tests/                  # pytest 测试
├── docker-compose.yml
├── pytest.ini
└── .env.example            # 配置项模板
```

## 启动方式

### 后端（本地）

前置要求：Python 3.11+，且 `ffmpeg`、`ffprobe` 已安装并加入 PATH（视频解析与抽帧依赖它们）。

```bash
pip install -r backend/requirements.txt
cp .env.example .env   # 可选，默认值即可运行
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

接口前缀为 `/api`：

- `GET /api/health` — 健康检查
- `POST /api/videos` — 上传视频（multipart 字段名 `file`）
- `GET /api/videos/{video_id}` — 查询任务状态与结果
- `GET /api/videos/{video_id}/frames` — 帧清单
- `GET /api/videos/{video_id}/frames/{filename}` — 帧图片
- `POST /api/videos/{video_id}/ocr` — 对已完成抽帧的视频同步执行 OCR（未抽帧返回 409）
- `GET /api/videos/{video_id}/events?modality=ocr` — 查询已生成的 OCR 时间线事件

### 前端（本地）

前置要求：Node.js 与 npm。

```bash
cd frontend
npm install
npm run dev
```

开发服务器默认监听 5173 端口，已通过 Vite proxy 将 `/api` 转发到 `http://localhost:8000`；也可以用环境变量 `VITE_API_BASE_URL` 指定后端地址。生产构建：`npm run build`。

### Docker

```bash
cp .env.example .env
docker compose up backend
```

compose 会在容器启动时安装 `backend/requirements.txt` 并以 8000 端口启动 uvicorn，仓库目录挂载到 `/app`。

> 已知限制：当前 compose 使用 `python:3.11-slim` 镜像，未内置 FFmpeg，容器内视频处理会因找不到 ffmpeg/ffprobe 而标记为 failed；如需在 Docker 中完整运行，请先在镜像中安装 FFmpeg。

## 运行测试

```bash
pip install -r backend/requirements.txt   # 已包含 pytest 与 httpx
python -m pytest tests/ -v
```

说明：测试通过 `STORAGE_DIR` 环境变量隔离到临时目录，不会污染仓库下的 `storage/`；涉及真实 ffmpeg/ffprobe 的用例在本机未安装 FFmpeg 时会自动跳过；OCR 引擎集成测试在 paddleocr 或 torch 未安装时自动跳过（已安装时需联网下载模型权重，首次较慢）。

OCR 说明：引擎为 PaddleOCR 官方包（`paddleocr` + CPU 版 `paddlepaddle`），主模型 PP-OCRv6 由配置 `OCR_PRIMARY_MODEL` 指定；模型权重首次运行自动下载到用户缓存目录（`~/.paddlex`），不会进入仓库。`torch` 为 OCR 链路显式依赖：Windows 下必须先于 paddle 加载，否则 paddle 预装的冲突 DLL 会导致 torch 的 shm.dll 解析失败（WinError 127），拖垮整条导入链，详见 `backend/requirements.txt` 注释。

## 已知问题与技术债

- `POST /api/videos/{video_id}/ocr` 为同步执行：长视频逐帧识别会长时间占用 worker 连接与事件循环。后续切片将改为异步任务（提交后立即返回任务 ID，提供状态查询接口），当前阶段调用方需容忍较长响应时间。

## 配置项

均可用同名环境变量或 `.env` 文件覆盖（见 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `development` | 应用环境 |
| `BACKEND_HOST` / `BACKEND_PORT` | `127.0.0.1` / `8000` | 后端监听地址与端口 |
| `STORAGE_DIR` | `storage` | 存储目录（相对路径基于仓库根目录解析） |
| `FRAME_EXTRACTION_FPS` | `2.0` | 抽帧帧率 |
| `SCENE_THRESHOLD` | `0.4` | 镜头切换检测的场景分数阈值（0, 1 区间） |
| `SCENE_MAX_FRAMES` | `500` | 镜头切换检测的最大抽帧数，超出截断 |
| `MAX_UPLOAD_SIZE_MB` | `500` | 上传文件大小上限（MB） |
| `OCR_PRIMARY_MODEL` | `PP-OCRv6` | 主 OCR 模型（PaddleOCR ocr_version） |
| `OCR_FALLBACK_MODEL` | `PaddleOCR-VL-1.6` | 备用疑难 OCR 模型标识（仅记录不加载，后续切片接入） |
| `OCR_LANG` | `ch` | OCR 识别语言 |
| `OCR_USE_GPU` | `false` | OCR 是否使用 GPU（默认 CPU） |
