"""应用配置：通过 pydantic-settings 读取环境变量。"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOCAL_HTTP_HOSTS = {"127.0.0.1", "localhost"}

# 仓库根目录（backend/app/core/config.py 向上四级）
ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """全局配置项，均可通过同名环境变量或 .env 文件覆盖。"""

    # .env 固定从仓库根目录读取，与 README 的启动方式（根目录 cp .env.example .env）一致
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    app_env: str = "development"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    storage_dir: str = "storage"
    frame_extraction_fps: float = Field(default=2.0, gt=0, description="抽帧帧率，必须大于 0")
    scene_threshold: float = Field(default=0.4, gt=0, lt=1, description="镜头切换检测的场景分数阈值，必须在 (0, 1) 区间")
    scene_max_frames: int = Field(default=500, gt=0, description="镜头切换检测的最大抽帧数，超出部分截断，防止异常视频产生海量帧")
    max_upload_size_mb: int = 500
    # OCR 模型配置（ai/ocr 使用，模型标识不散落在业务代码）
    ocr_primary_model: str = Field(default="PP-OCRv6", description="主 OCR 模型（PaddleOCR ocr_version）")
    ocr_fallback_model: str = Field(default="PaddleOCR-VL-1.6", description="备用疑难 OCR 模型标识，仅记录不加载，后续切片接入")
    ocr_lang: str = Field(default="ch", description="OCR 识别语言")
    ocr_use_gpu: bool = Field(default=False, description="OCR 是否使用 GPU，默认 CPU")
    # ASR 配置（ai/asr 使用）：ASR 推理全部在 Modal 云端，本地仅提取音频并 HTTPS 调用
    modal_asr_url: str = Field(default="", description="Modal 云端 ASR 服务地址（不含路径），未配置时 ASR 不可用")
    asr_primary_model: str = Field(default="Qwen3-ASR-1.7B", description="主 ASR 模型标识")
    asr_aligner_model: str = Field(default="Qwen3-ForcedAligner-0.6B", description="时间戳对齐模型标识，仅记录不加载，云端服务内部使用")
    asr_request_timeout_seconds: float = Field(default=120.0, gt=0, description="云端 ASR 服务请求超时（秒）")

    @field_validator("modal_asr_url")
    @classmethod
    def _validate_modal_asr_url(cls, value: str) -> str:
        """空值表示未配置；生产端点必须 https，仅允许本地 http stub，禁止 URL 凭据。"""
        url = (value or "").strip()
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("MODAL_ASR_URL 禁止携带用户名或密码")
        scheme = (parsed.scheme or "").lower()
        host = (parsed.hostname or "").lower()
        if not scheme or not parsed.netloc:
            raise ValueError("MODAL_ASR_URL 必须是含 scheme 的绝对 URL")
        if scheme == "https":
            return url
        if scheme == "http" and host in _LOCAL_HTTP_HOSTS:
            return url
        raise ValueError(
            "MODAL_ASR_URL 必须使用 https（本地测试 stub 允许 http://127.0.0.1 或 http://localhost）"
        )

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

    @property
    def audio_path(self) -> Path:
        return self.storage_path / "audio"

    def ensure_storage_dirs(self) -> None:
        """启动时确保存储目录结构存在。"""
        for path in (
            self.storage_path,
            self.uploads_path,
            self.frames_path,
            self.outputs_path,
            self.audio_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
