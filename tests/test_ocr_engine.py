"""PaddleOcrProvider 引擎集成测试。

用 Pillow 生成带清晰文字的测试图片，验证真实 PP-OCRv6 模型
能识别出关键字。首次运行需下载模型权重（外网），耗时较长属正常；
paddleocr 未安装时自动跳过。
"""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("paddleocr") is None,
    reason="paddleocr 未安装，跳过引擎集成测试",
)

from ai.ocr.paddleocr import PaddleOcrProvider  # noqa: E402


@pytest.fixture(scope="module")
def text_image(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """生成白底黑字测试图（大号字体保证检出率）。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 200), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 64)
    except OSError:
        font = ImageFont.load_default(size=64)
    draw.text((30, 60), "HELLO 2026", fill=(0, 0, 0), font=font)
    path = tmp_path_factory.mktemp("ocr") / "text.png"
    img.save(path)
    return path


@pytest.fixture(scope="module")
def provider() -> PaddleOcrProvider:
    """模块级共享引擎实例：模型初始化开销大，避免每个用例重复加载。"""
    return PaddleOcrProvider()


def test_recognize_text_image(provider: PaddleOcrProvider, text_image: Path) -> None:
    """带清晰文字的图片应识别出关键字，且置信度在合法区间。"""
    lines = provider.recognize(text_image)
    assert lines, "未识别出任何文字行"
    texts = " ".join(line.text.upper() for line in lines)
    assert "HELLO" in texts
    for line in lines:
        assert 0.0 <= line.confidence <= 1.0
        assert len(line.bbox) >= 2


def test_recognize_blank_image_returns_empty(provider: PaddleOcrProvider, tmp_path: Path) -> None:
    """纯空白图片应返回空列表而非报错。"""
    from PIL import Image

    blank = tmp_path / "blank.png"
    Image.new("RGB", (320, 120), color=(255, 255, 255)).save(blank)
    assert provider.recognize(blank) == []
