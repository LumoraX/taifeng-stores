"""消息行的读写：``PostgresMessageStore`` 与 ``PostgresMessageMirror`` 共用。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from taifeng import ResponseItem

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg
    from asyncpg.pool import PoolConnectionProxy

    Connection = asyncpg.Connection | PoolConnectionProxy

_INSERT_COLUMNS = "(position, thread_id, item_id, kind, payload, metadata, created_at)"


class PositionClock:
    """给每次写入调用发一个严格递增的位置序号。

    取值是「调用进入时刻的纳秒时间戳」，并保证在同一个进程里严格递增。必须在写入方法的
    第一个 ``await`` 之前同步调用：内核按消息顺序依次发起调用，但各次调用的数据库写入
    是并发完成的，只有进入顺序可靠。

    跨进程（另一个节点接管会话）时靠系统时钟排序；节点之间时钟偏差大于两次写入的间隔时，
    接管点前后的少量消息可能错位——镜像本来就只是视图，以节点本地主存为准。
    """

    def __init__(self) -> None:
        """从 0 起，首次取值即跳到当前时刻。"""
        self._last = 0

    def next(self) -> int:
        """下一个位置序号。"""
        self._last = max(time.time_ns(), self._last + 1)
        return self._last


def _dump(value: Any) -> str:
    """序列化 JSON 列。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def item_to_row(item: ResponseItem, position: int) -> tuple[int, str, str, str, str, str, Any]:
    """``ResponseItem`` → 插入参数。"""
    return (
        position,
        item.thread_id,
        item.id,
        item.kind,
        _dump(item.payload),
        _dump(item.metadata),
        item.created_at,
    )


def row_to_item(row: asyncpg.Record) -> ResponseItem:
    """数据库行 → ``ResponseItem``。"""
    return ResponseItem(
        kind=row["kind"],
        id=str(row["item_id"]),
        thread_id=str(row["thread_id"]),
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
        metadata=json.loads(row["metadata"]),
    )


async def insert_items(
    conn: Connection, table: str, items: Sequence[ResponseItem], *, position: int
) -> None:
    """按给定顺序写入一批消息；同一 ``(thread_id, item_id)`` 重复写入时保留先到的。

    逐条执行而不是 ``executemany``：批内顺序靠自增的 ``seq``，必须与传入顺序一致。
    """
    statement = (
        f"INSERT INTO {table} {_INSERT_COLUMNS} "
        "VALUES ($1, $2, $3, $4, $5::text::jsonb, $6::text::jsonb, $7) "
        "ON CONFLICT (thread_id, item_id) DO NOTHING"
    )
    for item in items:
        await conn.execute(statement, *item_to_row(item, position))


async def select_items(conn: Connection, table: str, thread_id: str) -> list[ResponseItem]:
    """按写入顺序取回一个线程的全部消息。"""
    rows = await conn.fetch(
        f"SELECT thread_id, item_id, kind, payload::text AS payload, "
        f"metadata::text AS metadata, created_at FROM {table} "
        "WHERE thread_id = $1 ORDER BY position, seq",
        thread_id,
    )
    return [row_to_item(row) for row in rows]


__all__ = ["PositionClock", "insert_items", "item_to_row", "row_to_item", "select_items"]
