"""应用配置：通过 pydantic-settings 读取环境变量。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录（backend/app/core/config.py 向上四级）
ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """全局配置项，均可通过同名环境变量或 .env 文件覆盖。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    storage_dir: str = "storage"
    frame_extraction_fps: float = 2.0
    max_upload_size_mb: int = 500

    @property
    def storage_path(self) -> Path:
        """存储目录的绝对路径，相对路径基于仓库根目录解析。"""
        path = Path(self.storage_dir)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path

    @property
    def uploads_path(self) -> Path:
        return self.storage_path / "uploads"

    @property
    def frames_path(self) -> Path:
        return self.storage_path / "frames"

    @property
    def outputs_path(self) -> Path:
        return self.storage_path / "outputs"

    def ensure_storage_dirs(self) -> None:
        """启动时确保存储目录结构存在。"""
        for path in (self.storage_path, self.uploads_path, self.frames_path, self.outputs_path):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
