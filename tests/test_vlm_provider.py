"""QwenVlmProvider、schemas 解析与工厂的单元测试。

Provider 通过本地 HTTP stub（http.server 线程）隔离，禁止依赖真实 Modal 服务；
覆盖响应解析、错误分支（超时 / 非 2xx / 501 / 非 JSON / 缺字段）与工厂分派。
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from ai.vlm import factory
from ai.vlm.base import (
    VlmEscalationUnavailableError,
    VlmNotConfiguredError,
    VlmProvider,
    VlmResponseFormatError,
    VlmReviewInput,
    VlmTimeoutError,
)
from ai.vlm.qwen import QwenVlmProvider, parse_review_response
from ai.vlm.schemas import VlmReviewResult
from app.core.config import get_settings

VALID_RESULT = {
    "risk": True,
    "category": "violence",
    "severity": "high",
    "confidence": 0.95,
    "start_ms": 500,
    "end_ms": 1000,
    "evidence": "第2帧含持刀刺击",
    "reason": "画面与字幕指向暴力",
    "suggestion": "拦截",
}


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch: pytest.MonkeyPatch):
    """每个用例还原配置缓存与工厂单例，避免相互污染。"""
    monkeypatch.setenv("MODAL_VLM_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("VLM_PROVIDER", "modal")
    monkeypatch.setenv("VLM_PRIMARY", "qwen3-vl-8b-instruct")
    monkeypatch.setenv("VLM_REQUEST_MAX_RETRIES", "0")
    get_settings.cache_clear()
    factory._provider = None
    yield
    factory._provider = None
    get_settings.cache_clear()


class _StubHandler(BaseHTTPRequestHandler):
    """固定行为的 /review stub：状态码与响应体由类属性控制。"""

    status_code = 200
    body: Any = VALID_RESULT
    last_request: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802
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
    _StubHandler.status_code = 200
    _StubHandler.body = VALID_RESULT
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def _make_provider(base_url: str, monkeypatch: pytest.MonkeyPatch) -> QwenVlmProvider:
    monkeypatch.setenv("MODAL_VLM_URL", base_url)
    get_settings.cache_clear()
    return QwenVlmProvider()


def _make_frames(tmp_path: Path, count: int = 2) -> list[Path]:
    paths = []
    for index in range(count):
        path = tmp_path / f"frame_{index:06d}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
        paths.append(path)
    return paths


def _input(tmp_path: Path, model: str | None = None) -> VlmReviewInput:
    paths = _make_frames(tmp_path, 2)
    return VlmReviewInput(
        frame_paths=paths,
        frame_timestamps_ms=[0, 500],
        ocr_text="他拿起刀刺向对方",
        asr_text="你给我去死吧",
        context="漫剧第3集",
        rules="严查暴力",
        model=model,
    )


def test_review_success(stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """正常契约：multipart 多帧 + 表单字段，解析出严格 JSON。"""
    provider = _make_provider(stub_server, monkeypatch)
    result = provider.review(_input(tmp_path))

    assert isinstance(result, VlmReviewResult)
    assert result.risk is True
    assert result.category == "violence"
    assert result.severity == "high"
    assert result.confidence == pytest.approx(0.95)
    assert result.start_ms == 500 and result.end_ms == 1000

    request = _StubHandler.last_request
    assert request["path"] == "/review"
    assert request["content_type"].startswith("multipart/form-data")
    assert b"frame_000000.jpg" in request["body"]
    assert b"frame_000001.jpg" in request["body"]
    assert b"qwen3-vl-8b-instruct" in request["body"]
    assert b"ocr_text" in request["body"]
    assert b"[0, 500]" in request["body"] or b"[0,500]" in request["body"]


def test_review_501_raises_escalation_unavailable(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.status_code = 501
    _StubHandler.body = {"detail": "escalation 未部署"}
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(VlmEscalationUnavailableError, match="501"):
        provider.review(_input(tmp_path, model="qwen3-vl-32b-instruct"))


def test_review_non_200_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ai.vlm.base import VlmServiceError

    _StubHandler.status_code = 500
    _StubHandler.body = "internal error"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(VlmServiceError, match="500"):
        provider.review(_input(tmp_path))


def test_review_400_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ai.vlm.base import VlmServiceError

    _StubHandler.status_code = 400
    _StubHandler.body = {"detail": "未知 model"}
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(VlmServiceError, match="400"):
        provider.review(_input(tmp_path, model="unknown-model"))


def test_review_non_json_raises(
    stub_server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _StubHandler.body = "<html>not json</html>"
    provider = _make_provider(stub_server, monkeypatch)
    with pytest.raises(VlmResponseFormatError, match="非 JSON"):
        provider.review(_input(tmp_path))


def test_review_unreachable_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ai.vlm.base import VlmServiceError

    provider = _make_provider("http://127.0.0.1:1", monkeypatch)
    with pytest.raises(VlmServiceError, match="请求失败"):
        provider.review(_input(tmp_path))


def test_provider_requires_modal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_VLM_URL", "")
    get_settings.cache_clear()
    with pytest.raises(VlmNotConfiguredError, match="MODAL_VLM_URL"):
        QwenVlmProvider()


def test_review_timeout_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    with pytest.raises(VlmTimeoutError, match="超时"):
        provider.review(_input(tmp_path))
    provider._client.close()


def test_review_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """瞬时 502 在重试后成功。"""
    monkeypatch.setenv("VLM_REQUEST_MAX_RETRIES", "1")
    monkeypatch.setenv("MODAL_VLM_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()
    provider = QwenVlmProvider()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=VALID_RESULT)

    provider._client.close()
    provider._client = httpx.Client(
        base_url="http://127.0.0.1:9",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    result = provider.review(_input(tmp_path))
    assert result.category == "violence"
    assert calls["n"] == 2
    provider._client.close()


def test_qwen_reexports_abstract_errors() -> None:
    from ai.vlm import base, qwen

    assert qwen.VlmServiceError is base.VlmServiceError
    assert qwen.VlmResponseFormatError is base.VlmResponseFormatError
    assert qwen.VlmTimeoutError is base.VlmTimeoutError
    assert qwen.VlmNotConfiguredError is base.VlmNotConfiguredError
    assert qwen.VlmEscalationUnavailableError is base.VlmEscalationUnavailableError


# ---- parse_review_response 严格解析 ----


def _payload(**overrides: Any) -> dict:
    data = dict(VALID_RESULT)
    data.update(overrides)
    return data


def test_parse_safe_result_normalized() -> None:
    result = parse_review_response(
        _payload(risk=False, category="porn", severity="low", start_ms=100, end_ms=200)
    )
    assert result.risk is False
    assert result.category == "none"
    assert result.severity == "none"
    assert result.start_ms == 0 and result.end_ms == 0


def test_parse_integer_confidence_allowed() -> None:
    result = parse_review_response(_payload(confidence=1))
    assert result.confidence == 1.0


@pytest.mark.parametrize(
    "payload, match",
    [
        (["不是对象"], "JSON 对象"),
        ({"risk": True}, "缺少字段"),
        (_payload(risk="yes"), "risk 字段类型非法"),
        (_payload(category=1), "category 字段类型非法"),
        (_payload(category="nudity"), "category 取值非法"),
        (_payload(severity="fatal"), "severity 取值非法"),
        (_payload(confidence="high"), "confidence 字段类型非法"),
        (_payload(confidence=True), "confidence 字段类型非法"),
        (_payload(confidence=1.5), "confidence 越界"),
        (_payload(start_ms="0"), "start_ms 必须是非负整数"),
        (_payload(start_ms=True), "start_ms 必须是非负整数"),
        (_payload(start_ms=1.5), "start_ms 必须是非负整数"),
        (_payload(start_ms=-1), "start_ms 必须是非负整数"),
        (_payload(end_ms=100, start_ms=500), "时间区间非法"),
        (_payload(evidence=1), "evidence 字段类型非法"),
        (_payload(extra_field="x"), "未知字段"),
        (_payload(risk=True, category="none", severity="high"), "不得为空值 none"),
    ],
)
def test_parse_response_invalid(payload: Any, match: str) -> None:
    with pytest.raises(VlmResponseFormatError, match=match):
        parse_review_response(payload)


def test_pydantic_model_forbids_extra() -> None:
    with pytest.raises(Exception):
        VlmReviewResult.model_validate({**VALID_RESULT, "foo": "bar"})


def test_categories_match_modal_contract() -> None:
    from ai.vlm import schemas

    path = Path(__file__).resolve().parents[1] / "modal" / "vlm" / "review_contract.py"
    spec = importlib.util.spec_from_file_location("modal_vlm_review_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert schemas.RISK_CATEGORIES == module.RISK_CATEGORIES
    assert schemas.SEVERITY_LEVELS == module.SEVERITY_LEVELS
    assert schemas.REVIEW_RESULT_FIELDS == module.REVIEW_RESULT_FIELDS


# ---- 工厂分派 ----


def test_factory_returns_qwen_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = factory._create_provider()
    assert isinstance(provider, QwenVlmProvider)
    assert isinstance(provider, VlmProvider)


def test_factory_unknown_model_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLM_PRIMARY", "Unknown-VLM-1.0")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="未支持的主 VLM 模型标识"):
        factory._create_provider()


def test_factory_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    first = factory.get_default_provider()
    assert factory.get_default_provider() is first
    assert isinstance(first, QwenVlmProvider)
