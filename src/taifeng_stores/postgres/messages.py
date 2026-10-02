"""消息行的读写：``PostgresMessageStore`` 与 ``PostgresMessageMirror`` 共用。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from taifeng import ResponseItem

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import asyncpg
    from asyncpg.pool import PoolConnectionProxy

    Connection = asyncpg.Connection | PoolConnectionProxy

_INSERT_COLUMNS = "(position, thread_id, item_id, kind, payload, metadata, created_at)"


class PositionClock:
    """给每次写入调用发一个位置序号；回放按 ``(position, seq)`` 排序。

    取值不小于「调用进入时刻的纳秒时间戳」，并且大于本时钟的全局下限；每发一个序号，
    下限就推到这个序号，所以同一个时钟发出的序号严格递增。

    - 同进程内靠「调用进入顺序」：必须在写入方法的第一个 ``await`` 之前同步调用。内核按
      消息顺序依次发起调用，但各次调用的数据库写入是并发完成的，只有进入顺序可靠。
    - 跨节点接管时靠回放续排：接管必先回放，回放看到的最大位置经 ``observe`` 抬高本时钟
      （即本存储实例）的全局下限，之后所有写入都排在它之后，节点之间的时钟偏差不再打乱
      顺序。效果类似 Lamport 时钟：position 只用于排序，不当时间用，被别的节点抬高无害。
    - 镜像（``PostgresMessageMirror``）从不 ``observe``，行为与只按墙钟取值一致。
    """

    def __init__(self, now_ns: Callable[[], int] = time.time_ns) -> None:
        """
        Args:
            now_ns: 墙钟，返回纳秒时间戳；测试时可注入。从 0 起，首次取值即跳到当前时刻。
        """
        self._now_ns = now_ns
        # 全局下限：本时钟发出过的、以及回放时观察到的最大位置
        self._last = 0

    def observe(self, position: int) -> None:
        """记下回放时看到的已有位置：之后发出的序号一定大于它；更小的值不会拉低下限。"""
        self._last = max(self._last, position)

    def next(self) -> int:
        """下一个位置序号：不小于墙钟，且严格大于全局下限。"""
        self._last = max(self._now_ns(), self._last + 1)
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


async def select_items_with_max_position(
    conn: Connection, table: str, thread_id: str
) -> tuple[list[ResponseItem], int | None]:
    """按写入顺序取回一个线程的全部消息，连同其中最大的位置；线程没有消息时位置为 None。

    结果按 ``position`` 升序，最大位置就是最后一行的位置，一次查询即可取到。
    """
    rows = await conn.fetch(
        f"SELECT position, thread_id, item_id, kind, payload::text AS payload, "
        f"metadata::text AS metadata, created_at FROM {table} "
        "WHERE thread_id = $1 ORDER BY position, seq",
        thread_id,
    )
    max_position: int | None = rows[-1]["position"] if rows else None
    return [row_to_item(row) for row in rows], max_position


__all__ = [
    "PositionClock",
    "insert_items",
    "item_to_row",
    "row_to_item",
    "select_items_with_max_position",
]
