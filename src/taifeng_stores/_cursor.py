"""线程列表的分页游标：``(updated_at, thread_id)`` 的不透明编码。

格式与 taifeng 默认的 ``SqliteThreadDirectory`` 一致（urlsafe base64 包一层 JSON），
宿主在不同后端之间切换时已发出去的游标仍可解析。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

logger = logging.getLogger(__name__)


def encode_cursor(updated_at: float, thread_id: str) -> str:
    """编码游标。"""
    payload = json.dumps({"updated_at": updated_at, "thread_id": thread_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[float, str] | None:
    """解码游标；None 或损坏的游标都返回 None（调用方从头开始列）。

    契约要求损坏的游标不抛异常、从首页重新开始。内核默认实现此时还会发
    ``directory_cursor_reset`` 事件，但事件出口协议尚未公开，这里改为写日志。
    """
    if cursor is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        return float(payload["updated_at"]), str(payload["thread_id"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        logger.warning("线程列表游标无法解析，从首页重新开始：%r", exc)
        return None


def validate_limit(limit: int) -> None:
    """``limit`` 必须落在 ``[1, 1000]``。"""
    if limit < 1 or limit > 1000:
        raise ValueError(f"limit must be in [1, 1000]; got {limit}")


__all__ = ["decode_cursor", "encode_cursor", "validate_limit"]
