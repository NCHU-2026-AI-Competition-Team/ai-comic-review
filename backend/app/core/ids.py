"""video_id 规范化：统一 UUID 字符串，供 API 与服务层共用。"""

import uuid


class InvalidVideoIdError(ValueError):
    """video_id 不是合法 UUID，拒绝进入路径拼接。"""


def normalize_video_id(video_id: str) -> str:
    """把 video_id 规范为带连字符的小写 UUID 字符串；非法格式抛 InvalidVideoIdError。"""
    try:
        return str(uuid.UUID(str(video_id)))
    except (ValueError, AttributeError, TypeError):
        raise InvalidVideoIdError("video_id 格式非法") from None
