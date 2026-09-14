"""QwenAsrProvider 与工厂的单元测试。

Provider 通过本地 HTTP stub（http.server 线程）隔离，禁止依赖真实 Modal 服务；
覆盖响应解析、错误分支（非 200 / 非 JSON / 结构非法）与工厂分派。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from ai.asr import factory
from ai.asr.base import AsrResult, AsrSegment
from ai.asr.qwen_asr import (
    AsrResponseFormatError,
    AsrServiceError,
    QwenAsrProvider,
    parse_transcribe_response,
)
from app.core.config import get_settings

VALID_SEGMENTS = [
    {"text": "第一句", "start_ms": 0, "end_ms": 1200, "confidence": 0.95},
    {"text": "第二句", "start_ms": 1500, "end_ms": 3000, "confidence": 0.87},
]


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch):
    """每个用例还原配置缓存与工厂单例，避免相互污染。"""
    monkeypatch.setenv("MODAL_ASR_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    factory._provider = None
    yield
    factory._provider = None
    get_settings.cache_clear()


class _StubHandler(BaseHTTPRequestHandler):
    """固定行为的 /transcribe stub：状态码与响应体由类属性控制。"""

    status_code = 200
    body: Any = {"segments": VALID_SEGMENTS, "language": "zh"}
    last_request: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802（http.server 约定的方法名）
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length)
        type(self).last_request = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": payload,
        }
        if isinstance(self.body, (dict, list)):
            raw = json.dumps(self.body).encode("utf-8")
            content_type = "application/json"
        else:
            raw = str(self.body).encode("utf-8")
            content_type = "text/plain"
        self.send_response(self.status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def stub_server() -> Iterator[str]:
    """启动本地 HTTP stub，返回 base_url；用例可改写 _StubHandler 类属性控制行为。"""
    _StubHandler.status_code = 200
    _StubHandler.body = {"segments": VALID_SEGMENTS, "language": "zh"}
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def _make_provider(base_url: str, monkeypatch: pytest.MonkeyPatch) -> QwenAsrProvider:
    monkeypatch.setenv("MODAL_ASR_URL", base_url)
    get_settings.cache_clear()
    return QwenAsrProvider()


def _make_wav(tmp_path: Path) -> Path:
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    return wav


def test_transcribe_success(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正常契约：multipart 上传 wav，解析出分段与语言。"""
    provider = _make_provider(stub_server, monkeypatch)
    result = provider.transcribe(_make_wav(tmp_path))

    assert isinstance(result, AsrResult)
    assert result.language == "zh"
    assert result.segments == [
        AsrSegment(text="第一句", start_ms=0, end_ms=1200, confidence=0.95),
        AsrSegment(text="第二句", start_ms=1500, end_ms=3000, confidence=0.87),
    ]

    request = _StubHandler.last_request
    assert request["path"] == "/transcribe"
    assert request["content_type"].startswith("multipart/form-data")
    assert b"audio.wav" in request["body"]
    assert b"Qwen3-ASR-1.7B" in request["body"]


def test_transcribe_non_200_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.status_code = 500
    _StubHandler.body = "internal error"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(AsrServiceError, match="500"):
        provider.transcribe(_make_wav(tmp_path))


def test_transcribe_non_json_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.body = "<html>not json</html>"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(AsrResponseFormatError, match="非 JSON"):
        provider.transcribe(_make_wav(tmp_path))


def test_transcribe_unreachable_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """服务不可达时抛 AsrServiceError（端口 1 必然连接失败）。"""
    provider = _make_provider("http://127.0.0.1:1", monkeypatch)
    with pytest.raises(AsrServiceError, match="请求失败"):
        provider.transcribe(_make_wav(tmp_path))


def test_provider_requires_modal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 MODAL_ASR_URL 时初始化明确报错。"""
    monkeypatch.setenv("MODAL_ASR_URL", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="MODAL_ASR_URL"):
        QwenAsrProvider()


# ---- parse_transcribe_response 严格解析 ----


def _payload(**overrides: Any) -> dict:
    return {"segments": VALID_SEGMENTS, "language": "zh", **overrides}


def test_parse_response_numeric_coercion() -> None:
    """浮点毫秒时间戳四舍五入为 int。"""
    result = parse_transcribe_response(
        _payload(segments=[{"text": "a", "start_ms": 0.4, "end_ms": 1200.6, "confidence": 1}])
    )
    assert result.segments[0].start_ms == 0
    assert result.segments[0].end_ms == 1201
    assert result.segments[0].confidence == 1.0


def test_parse_response_empty_segments_allowed() -> None:
    result = parse_transcribe_response(_payload(segments=[]))
    assert result.segments == []
    assert result.language == "zh"


@pytest.mark.parametrize(
    "payload, match",
    [
        (["不是对象"], "JSON 对象"),
        ({"language": "zh"}, "缺少 segments"),
        ({"segments": []}, "缺少 language"),
        (_payload(segments="不是列表"), "segments 字段类型非法"),
        (_payload(segments=["不是对象"]), "segments\\[0\\] 类型非法"),
        (_payload(segments=[{"text": "a", "start_ms": 0, "end_ms": 1}]), "缺少字段"),
        (_payload(segments=[{"text": 1, "start_ms": 0, "end_ms": 1, "confidence": 0.5}]), "text 字段类型非法"),
        (_payload(segments=[{"text": "a", "start_ms": "0", "end_ms": 1, "confidence": 0.5}]), "start_ms 字段类型非法"),
        (_payload(segments=[{"text": "a", "start_ms": -1, "end_ms": 1, "confidence": 0.5}]), "时间区间非法"),
        (_payload(segments=[{"text": "a", "start_ms": 5, "end_ms": 1, "confidence": 0.5}]), "时间区间非法"),
        (_payload(language=123), "language 字段类型非法"),
    ],
)
def test_parse_response_invalid(payload: Any, match: str) -> None:
    with pytest.raises(AsrResponseFormatError, match=match):
        parse_transcribe_response(payload)


# ---- 工厂分派 ----


def test_factory_returns_qwen_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = factory._create_provider()
    assert isinstance(provider, QwenAsrProvider)
    assert isinstance(provider, factory.AsrProvider)


def test_factory_unknown_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_PRIMARY_MODEL", "Unknown-ASR-1.0")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="未支持的主 ASR 模型标识"):
        factory._create_provider()


def test_factory_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    first = factory.get_default_provider()
    assert factory.get_default_provider() is first
