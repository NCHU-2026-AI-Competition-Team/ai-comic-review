# AI Comic Review

AI 漫画/视频内容审核系统（开发中）。当前阶段已实现：上传视频后自动解析元数据（时长、分辨率、帧率、编码、码率），并按固定帧率（默认 2 FPS）抽取关键帧，前端页面展示视频信息与帧列表。后续将在此基础上接入 OCR、ASR、视觉理解（VLM）与多模态风险融合判定，完成对漫画/视频内容的自动化审核。

## 功能

- 视频上传：支持 `.mp4` / `.mov` / `.mkv`，单个文件，大小上限默认 500 MB（可配置）
- 元数据解析：基于 ffprobe 提取时长、分辨率、帧率、编码格式、码率
- 关键帧抽取：基于 ffmpeg 按固定帧率抽帧，生成帧图片与 `frames.json` 帧清单（含毫秒级时间戳）
- 镜头切换检测：可选 scene 采样模式，基于 ffmpeg 场景分数识别镜头边界并逐边界精确抽帧（阈值与帧数上限可配置）
- 状态查询：查询视频任务状态（processing / processed / failed）、帧清单与帧图片
- 前端页面：拖拽/点选上传、采样模式选择、处理状态轮询、元数据与帧网格展示

## 技术栈

- 后端：Python 3.11、FastAPI、Uvicorn、Pydantic / pydantic-settings、FFmpeg（ffmpeg / ffprobe 命令行）
- 前端：React 18、TypeScript、Vite 5
- 测试：pytest、FastAPI TestClient（httpx）
- 部署：docker-compose（后端服务）

## 目录结构

```
├── ai/                     # AI 模块（占位，后续阶段实现）
│   ├── asr/                #   语音转写
│   ├── ocr/                #   画面文字识别
│   ├── risk/               #   多模态风险融合判定
│   ├── video/              #   视频元数据解析与抽帧
│   └── vlm/                #   视觉语言大模型理解
├── backend/
│   ├── app/
│   │   ├── api/            # 路由层：health.py、videos.py
│   │   ├── core/           # 配置：config.py（pydantic-settings）
│   │   ├── schemas/        # 数据模型：video.py、events.py（时间线事件，预留）
│   │   ├── services/       # 业务层：video/（ffprobe/固定帧率抽帧/镜头切换检测）、registry.py（任务记录落盘）
│   │   └── main.py         # FastAPI 应用入口
│   └── requirements.txt
├── frontend/
│   └── src/
│       ├── api/videos.ts   # 后端 API 封装
│       ├── components/     # UploadPanel / VideoInfoPanel / FramesGrid
│       └── App.tsx         # 页面状态机（上传 → 处理 → 展示）
├── storage/                # 运行时数据（见下文）
│   ├── uploads/            #   原始视频与任务记录 job.json
│   ├── frames/             #   抽帧结果 frames/{video_id}/
│   └── outputs/            #   审核产出（预留）
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

说明：测试通过 `STORAGE_DIR` 环境变量隔离到临时目录，不会污染仓库下的 `storage/`；涉及真实 ffmpeg/ffprobe 的用例在本机未安装 FFmpeg 时会自动跳过。

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
