"""配置项加载与校验测试。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_frame_extraction_fps_zero_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRAME_EXTRACTION_FPS=0 应在配置加载时明确报错。"""
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_frame_extraction_fps_negative_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "-1.5")
    with pytest.raises(ValidationError):
        Settings()


def test_frame_extraction_fps_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAME_EXTRACTION_FPS", "1.5")
    assert Settings().frame_extraction_fps == 1.5


def test_modal_asr_url_empty_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "")
    assert Settings().modal_asr_url == ""


def test_modal_asr_url_https_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "https://asr.example.modal.run")
    assert Settings().modal_asr_url == "https://asr.example.modal.run"


def test_modal_asr_url_localhost_http_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "http://127.0.0.1:8000")
    assert Settings().modal_asr_url == "http://127.0.0.1:8000"
    monkeypatch.setenv("MODAL_ASR_URL", "http://localhost:9")
    assert Settings().modal_asr_url == "http://localhost:9"


def test_modal_asr_url_http_remote_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "http://asr.example.com")
    with pytest.raises(ValidationError):
        Settings()


def test_modal_asr_url_credentials_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "https://user:pass@asr.example.com")
    with pytest.raises(ValidationError):
        Settings()


def test_modal_asr_url_missing_scheme_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_ASR_URL", "asr.example.com")
    with pytest.raises(ValidationError):
        Settings()


def test_ocr_provider_default_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_PROVIDER", raising=False)
    assert Settings().ocr_provider == "local"


def test_ocr_provider_modal_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "MODAL")
    assert Settings().ocr_provider == "modal"


def test_ocr_provider_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "cloud")
    with pytest.raises(ValidationError):
        Settings()


def test_modal_ocr_url_empty_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "")
    assert Settings().modal_ocr_url == ""


def test_modal_ocr_url_https_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "https://ocr.example.modal.run")
    assert Settings().modal_ocr_url == "https://ocr.example.modal.run"


def test_modal_ocr_url_localhost_http_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "http://127.0.0.1:8000")
    assert Settings().modal_ocr_url == "http://127.0.0.1:8000"
    monkeypatch.setenv("MODAL_OCR_URL", "http://localhost:9")
    assert Settings().modal_ocr_url == "http://localhost:9"


def test_modal_ocr_url_http_remote_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "http://ocr.example.com")
    with pytest.raises(ValidationError):
        Settings()


def test_modal_ocr_url_credentials_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "https://user:pass@ocr.example.com")
    with pytest.raises(ValidationError):
        Settings()


def test_modal_ocr_url_missing_scheme_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_OCR_URL", "ocr.example.com")
    with pytest.raises(ValidationError):
        Settings()
