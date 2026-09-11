"""FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health, videos
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_storage_dirs()

    app = FastAPI(title="AI Comic Review API")

    # 允许前端开发服务器（Vite 默认 5173 端口）跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(videos.router, prefix="/api")

    return app


app = create_app()
