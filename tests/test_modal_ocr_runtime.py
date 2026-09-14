"""modal/ocr 运行时辅助与服务配置单测。

不导入 Modal SDK、不访问云端；runtime.py 按文件路径加载。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_OCR_DIR = Path(__file__).resolve().parents[1] / "modal" / "ocr"
_RUNTIME_PATH = _OCR_DIR / "runtime.py"
_SERVE_PATH = _OCR_DIR / "serve.py"
_CONFIG_PATH = _OCR_DIR / "config.yaml"
JPEG_MAGIC = b"\xff\xd8\xff"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("ocr_modal_runtime", _RUNTIME_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_config_preloads_vl_and_extends_timeouts() -> None:
    cfg = _load_config()
    serving = cfg["serving"]
    assert serving["preload_fallback"] is True
    assert serving["request_timeout"] >= 600
    assert serving["startup_timeout"] >= 1800
    assert "min_containers" not in serving
    assert cfg["models"]["fallback"] == "PaddleOCR-VL-1.6"
    assert cfg["models"]["primary"] == "PP-OCRv6"


def test_serve_source_preloads_vl_in_enter() -> None:
    source = _SERVE_PATH.read_text(encoding="utf-8")
    assert "min_containers" not in source
    assert "self.fallback = self._load_fallback()" in source
    assert "warmup_engine(self.fallback" in source
    assert "@modal.enter()" in source
    setup_at = source.index("def setup(self)")
    load_at = source.index("self.fallback = self._load_fallback()")
    require_at = source.index("def _require_fallback")
    assert setup_at < load_at < require_at
    assert "拒绝在请求路径中懒加载" in source
    assert "timeout=REQUEST_TIMEOUT" in source
    assert "startup_timeout=STARTUP_TIMEOUT" in source
    assert 'CUDA_MODULE_LOADING": "EAGER"' in source


def test_vl_engine_kwargs_require_v16_native() -> None:
    required = runtime.vl_engine_kwargs(include_optional=False)
    assert required == {
        "pipeline_version": "v1.6",
        "vl_rec_backend": "native",
        "device": "gpu",
    }
    full = runtime.vl_engine_kwargs(include_optional=True)
    assert full["pipeline_version"] == "v1.6"
    assert full["vl_rec_backend"] == "native"
    assert full["use_chart_recognition"] is False
    assert full["use_doc_orientation_classify"] is False
    assert full["use_doc_unwarping"] is False


def test_create_vl_engine_passes_required_and_optional() -> None:
    captured: dict = {}

    class Fake:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    engine = runtime.create_vl_engine(Fake)
    assert isinstance(engine, Fake)
    assert captured["pipeline_version"] == "v1.6"
    assert captured["vl_rec_backend"] == "native"
    assert captured["use_chart_recognition"] is False


def test_create_vl_engine_rejects_missing_pipeline_version() -> None:
    class Fake:
        def __init__(self, **kwargs) -> None:
            raise TypeError("got an unexpected keyword argument 'pipeline_version'")

    with pytest.raises(RuntimeError, match="拒绝静默回退到默认 PaddleOCR-VL-1.5"):
        runtime.create_vl_engine(Fake)


def test_create_vl_engine_retries_without_optional_kwargs() -> None:
    class Fake:
        def __init__(self, **kwargs) -> None:
            if "use_chart_recognition" in kwargs:
                raise TypeError("got an unexpected keyword argument 'use_chart_recognition'")
            self.kwargs = kwargs

    engine = runtime.create_vl_engine(Fake)
    assert engine.kwargs["pipeline_version"] == "v1.6"
    assert "use_chart_recognition" not in engine.kwargs


def test_dummy_jpeg_bytes_is_jpeg_with_payload() -> None:
    data = runtime.dummy_jpeg_bytes(text="HELLO 2026")
    assert data.startswith(JPEG_MAGIC)
    assert len(data) > 100


def test_predict_image_calls_engine_and_deletes_temp() -> None:
    seen: list[str] = []

    class Eng:
        def predict(self, path: str):
            seen.append(path)
            assert Path(path).exists()
            assert Path(path).read_bytes().startswith(JPEG_MAGIC)
            return ["ok"]

    result = runtime.predict_image(Eng(), runtime.dummy_jpeg_bytes())
    assert result == ["ok"]
    assert seen
    assert not Path(seen[0]).exists()


def test_warmup_engine_invokes_predict() -> None:
    calls = []

    class Eng:
        def predict(self, path: str):
            calls.append(path)
            return []

    runtime.warmup_engine(Eng(), runtime.dummy_jpeg_bytes(text="WARM"))
    assert len(calls) == 1
    assert not Path(calls[0]).exists()


def test_parse_predict_uses_parser() -> None:
    class Eng:
        def predict(self, path: str):
            return SimpleNamespace(raw="engine")

    lines = runtime.parse_predict(
        Eng(),
        runtime.dummy_jpeg_bytes(),
        parser=lambda raw: [{"text": "parsed", "raw": raw.raw}],
    )
    assert lines == [{"text": "parsed", "raw": "engine"}]
