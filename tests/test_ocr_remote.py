"""RemoteOcrProvider 与工厂的单元测试。

Provider 通过本地 HTTP stub（http.server 线程）隔离，禁止依赖真实 Modal 服务；
覆盖响应解析、错误分支（超时 / 非 2xx / 非 JSON / 缺行字段 / 结构非法）与工厂分派。
"""

import importlib.util
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from ai.ocr import factory
from ai.ocr.base import OcrProvider, OcrTextLine
from ai.ocr.remote import (
    OcrNotConfiguredError,
    OcrResponseFormatError,
    OcrServiceError,
    OcrTimeoutError,
    RemoteOcrProvider,
    parse_recognize_response,
)
from app.core.config import get_settings

VALID_BBOX = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
VALID_LINES = [
    {"text": "第一行", "bbox": VALID_BBOX, "confidence": 0.95},
    {"text": "第二行", "bbox": [[0, 20], [10, 20], [10, 30], [0, 30]], "confidence": 0.87},
]


class _FakePaddleOcrProvider(OcrProvider):
    """避免工厂 local 路径加载真实 PaddleOCR 权重。"""

    def __init__(self) -> None:
        pass

    def recognize(self, image_path: Path) -> list[OcrTextLine]:
        return []


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch):
    """每个用例还原配置缓存与工厂单例，避免相互污染。"""
    monkeypatch.setenv("OCR_PROVIDER", "local")
    monkeypatch.setenv("MODAL_OCR_URL", "http://127.0.0.1:1")
    monkeypatch.setattr("ai.ocr.paddleocr.PaddleOcrProvider", _FakePaddleOcrProvider)
    get_settings.cache_clear()
    factory._provider = None
    yield
    factory._provider = None
    get_settings.cache_clear()


class _StubHandler(BaseHTTPRequestHandler):
    """固定行为的 /recognize stub：状态码与响应体由类属性控制。"""

    status_code = 200
    body: Any = {"lines": VALID_LINES}
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
    _StubHandler.body = {"lines": VALID_LINES}
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def _make_provider(base_url: str, monkeypatch: pytest.MonkeyPatch) -> RemoteOcrProvider:
    monkeypatch.setenv("MODAL_OCR_URL", base_url)
    get_settings.cache_clear()
    return RemoteOcrProvider()


def _make_jpeg(tmp_path: Path) -> Path:
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    return path


def test_recognize_success(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正常契约：multipart 上传 JPEG，解析出文字行。"""
    provider = _make_provider(stub_server, monkeypatch)
    result = provider.recognize(_make_jpeg(tmp_path))

    assert result == [
        OcrTextLine(text="第一行", bbox=VALID_BBOX, confidence=0.95),
        OcrTextLine(
            text="第二行",
            bbox=[[0.0, 20.0], [10.0, 20.0], [10.0, 30.0], [0.0, 30.0]],
            confidence=0.87,
        ),
    ]

    request = _StubHandler.last_request
    assert request["path"] == "/recognize"
    assert request["content_type"].startswith("multipart/form-data")
    assert b"frame.jpg" in request["body"]
    assert b"PP-OCRv6" in request["body"]


def test_recognize_empty_lines(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.body = {"lines": []}
    provider = _make_provider(stub_server, monkeypatch)
    assert provider.recognize(_make_jpeg(tmp_path)) == []


def test_recognize_non_200_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.status_code = 500
    _StubHandler.body = "internal error"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(OcrServiceError, match="500"):
        provider.recognize(_make_jpeg(tmp_path))


def test_recognize_non_json_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.body = "<html>not json</html>"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(OcrResponseFormatError, match="非 JSON"):
        provider.recognize(_make_jpeg(tmp_path))


def test_recognize_unreachable_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """服务不可达时抛 OcrServiceError（端口 1 必然连接失败）。"""
    provider = _make_provider("http://127.0.0.1:1", monkeypatch)
    with pytest.raises(OcrServiceError, match="请求失败"):
        provider.recognize(_make_jpeg(tmp_path))


def test_provider_requires_modal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 MODAL_OCR_URL 时初始化明确报错。"""
    monkeypatch.setenv("MODAL_OCR_URL", "")
    get_settings.cache_clear()
    with pytest.raises(OcrNotConfiguredError, match="MODAL_OCR_URL"):
        RemoteOcrProvider()


def test_recognize_timeout_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """httpx 超时应映射为 OcrTimeoutError，而不是泛化的网络错误。"""
    provider = _make_provider("http://127.0.0.1:9", monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    provider._client.close()
    provider._client = httpx.Client(
        base_url="http://127.0.0.1:9",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    with pytest.raises(OcrTimeoutError, match="超时"):
        provider.recognize(_make_jpeg(tmp_path))
    provider._client.close()


def _payload(**overrides: Any) -> dict:
    return {"lines": VALID_LINES, **overrides}


def test_parse_response_numeric_bbox_coercion() -> None:
    result = parse_recognize_response(
        _payload(lines=[{"text": "a", "bbox": [[0, 0], [1, 1]], "confidence": 1}])
    )
    assert result[0].bbox == [[0.0, 0.0], [1.0, 1.0]]
    assert result[0].confidence == 1.0


def test_parse_response_empty_lines_allowed() -> None:
    assert parse_recognize_response(_payload(lines=[])) == []


@pytest.mark.parametrize(
    "payload, match",
    [
        (["不是对象"], "JSON 对象"),
        ({}, "缺少 lines"),
        (_payload(lines="不是列表"), "lines 字段类型非法"),
        (_payload(lines=["不是对象"]), "lines\\[0\\] 类型非法"),
        (_payload(lines=[{"text": "a", "bbox": VALID_BBOX}]), "缺少字段"),
        (_payload(lines=[{"text": 1, "bbox": VALID_BBOX, "confidence": 0.5}]), "text 字段类型非法"),
        (_payload(lines=[{"text": "a", "bbox": "坏", "confidence": 0.5}]), "bbox 字段类型非法"),
        (_payload(lines=[{"text": "a", "bbox": [[0]], "confidence": 0.5}]), "bbox 字段类型非法"),
        (
            _payload(lines=[{"text": "a", "bbox": [[0, 0, 1], [1, 1]], "confidence": 0.5}]),
            r"bbox\[0\] 非法",
        ),
        (
            _payload(lines=[{"text": "a", "bbox": [["0", 0], [1, 1]], "confidence": 0.5}]),
            r"bbox\[0\]\[0\] 字段类型非法",
        ),
        (_payload(lines=[{"text": "a", "bbox": VALID_BBOX, "confidence": "0.5"}]), "confidence 字段类型非法"),
        (_payload(lines=[{"text": "a", "bbox": VALID_BBOX, "confidence": True}]), "confidence 字段类型非法"),
        (_payload(lines=[{"text": "a", "bbox": VALID_BBOX, "confidence": float("nan")}]), "有限数值"),
        (_payload(lines=[{"text": "a", "bbox": VALID_BBOX, "confidence": float("inf")}]), "有限数值"),
    ],
)
def test_parse_response_invalid(payload: Any, match: str) -> None:
    with pytest.raises(OcrResponseFormatError, match=match):
        parse_recognize_response(payload)


def test_factory_local_returns_paddle_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "local")
    get_settings.cache_clear()
    provider = factory._create_provider()
    assert isinstance(provider, _FakePaddleOcrProvider)
    assert isinstance(provider, factory.OcrProvider)


def test_factory_modal_returns_remote_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "modal")
    monkeypatch.setenv("MODAL_OCR_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    provider = factory._create_provider()
    assert isinstance(provider, RemoteOcrProvider)
    assert isinstance(provider, factory.OcrProvider)


def test_factory_modal_without_url_falls_back_local(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "modal")
    monkeypatch.setenv("MODAL_OCR_URL", "")
    get_settings.cache_clear()
    with caplog.at_level(logging.WARNING):
        provider = factory._create_provider()
    assert isinstance(provider, _FakePaddleOcrProvider)
    assert "回退本地" in caplog.text


def test_factory_unknown_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "local")
    monkeypatch.setenv("OCR_PRIMARY_MODEL", "Unknown-OCR-1.0")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="未支持的主 OCR 模型标识"):
        factory._create_provider()


def test_factory_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PROVIDER", "modal")
    monkeypatch.setenv("MODAL_OCR_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    first = factory.get_default_provider()
    assert factory.get_default_provider() is first
    assert isinstance(first, RemoteOcrProvider)


def _load_modal_contract():
    path = Path(__file__).resolve().parents[1] / "modal" / "ocr" / "contract.py"
    spec = importlib.util.spec_from_file_location("ocr_modal_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_modal_contract_parse_ppocr_and_vl() -> None:
    contract = _load_modal_contract()
    ppocr = contract.parse_ppocr_results(
        [{"rec_texts": ["你好"], "rec_scores": [0.91], "rec_polys": [VALID_BBOX]}]
    )
    assert ppocr == [{"text": "你好", "bbox": VALID_BBOX, "confidence": 0.91}]

    vl = contract.parse_vl_results(
        [{"parsing_res_list": [{"block_content": "标题", "block_bbox": [0, 0, 10, 10]}]}]
    )
    assert vl[0]["text"] == "标题"
    assert vl[0]["bbox"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    assert vl[0]["confidence"] == 1.0

    assert contract.needs_fallback([], 0.6) is False
    assert contract.needs_fallback(ppocr, 0.6) is False
    assert contract.needs_fallback(ppocr, 0.95) is True

    class FakeVlBlock:
        def __init__(self) -> None:
            self.content = "HELLO"
            self.bbox = [1, 2, 11, 12]
            self.score = None

    vl_blocks = contract.parse_vl_results([FakeVlBlock()])
    assert vl_blocks[0]["text"] == "HELLO"
    assert vl_blocks[0]["bbox"] == [[1.0, 2.0], [11.0, 2.0], [11.0, 12.0], [1.0, 12.0]]
    assert vl_blocks[0]["confidence"] == 1.0
