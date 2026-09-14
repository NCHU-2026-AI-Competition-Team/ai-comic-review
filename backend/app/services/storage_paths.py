"""存储路径安全拼接：生成路径必须落在指定 storage 子目录内。"""

from pathlib import Path


class UnsafePathError(ValueError):
    """解析后的路径逃出了允许的存储根目录。"""


def resolve_in_dir(base: Path, *parts: str) -> Path:
    """在 base 下拼接路径并 resolve，若结果不在 base 内则抛 UnsafePathError。"""
    root = base.resolve()
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root):
        raise UnsafePathError(f"路径越界: {candidate} 不在 {root} 内")
    return candidate
