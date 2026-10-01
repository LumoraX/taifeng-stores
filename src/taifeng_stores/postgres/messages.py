"""消息行的读写：``PostgresMessageStore`` 与 ``PostgresMessageMirror`` 共用。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from taifeng import ResponseItem

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    import asyncpg
    from asyncpg.pool import PoolConnectionProxy

    Connection = asyncpg.Connection | PoolConnectionProxy

_INSERT_COLUMNS = "(position, thread_id, item_id, kind, payload, metadata, created_at)"


class PositionClock:
    """给每次写入调用发一个位置序号；回放按 ``(position, seq)`` 排序。

    取值不小于「调用进入时刻的纳秒时间戳」，并且同一个时钟发出的序号严格递增。

    - 同进程内靠「调用进入顺序」：必须在写入方法的第一个 ``await`` 之前同步调用。内核按
      消息顺序依次发起调用，但各次调用的数据库写入是并发完成的，只有进入顺序可靠。
    - 跨节点接管时靠库里已有的最大位置续排：接管必先回放，回放时用 ``observe`` 记下该线程
      已有的最大位置，之后对它的写入一定排在这之后，节点之间的时钟偏差不再打乱顺序。
    - 镜像（``PostgresMessageMirror``）不经回放、不调 ``observe``，仍按墙钟排序。

    下限按线程记，免得一个时钟偏快的节点写过的线程把本进程其余线程的位置一并抬高。
    各线程的下限存在一个容量有限（``capacity``）、按插入顺序淘汰的字典里。淘汰不影响
    正确性：被淘汰线程的下限并入全局下限，之后发出的任何位置都大于它；何况被淘汰的
    线程再被接管时一定会先被回放，重新观察。
    """

    def __init__(
        self, now_ns: Callable[[], int] = time.time_ns, *, capacity: int = 100_000
    ) -> None:
        """
        Args:
            now_ns: 墙钟，返回纳秒时间戳；测试时可注入。从 0 起，首次取值即跳到当前时刻。
            capacity: 最多记住多少个线程的下限，必须为正数。
        """
        if capacity < 1:
            raise ValueError(f"capacity must be positive; got {capacity}")
        self._now_ns = now_ns
        self._capacity = capacity
        # 本时钟发出过的最大位置，兼作全局下限（被淘汰线程的下限并入这里）
        self._last = 0
        # 线程 id → 该线程已知的最大位置；按插入顺序排列，最早的在前
        self._floors: dict[str, int] = {}

    def observe(self, thread_id: str, position: int) -> None:
        """记下某线程库里已有的最大位置：之后对它的写入一定排在这之后。"""
        if position > self._floors.get(thread_id, -1):
            self._remember(thread_id, position)

    def next(self, thread_ids: Iterable[str] = ()) -> int:
        """下一个位置序号：不小于墙钟，严格大于上一次取值与这些线程已有的最大位置。

        取到的序号同时记为这些线程的新下限。镜像不带线程调用，行为与只按墙钟取值一致。
        """
        ids = tuple(thread_ids)
        floor = max((self._floors.get(tid, -1) for tid in ids), default=-1)
        position = max(self._now_ns(), self._last + 1, floor + 1)
        self._last = position
        for tid in ids:
            self._remember(tid, position)
        return position

    def _remember(self, thread_id: str, position: int) -> None:
        """把线程的下限设为 ``position`` 并标为最新；超出容量时淘汰最早的线程。"""
        # 先删再插：已有的键也挪到最新
        self._floors.pop(thread_id, None)
        self._floors[thread_id] = position
        while len(self._floors) > self._capacity:
            oldest = next(iter(self._floors))
            # 被淘汰的下限并入全局下限，之后取的位置都大于它
            self._last = max(self._last, self._floors.pop(oldest))


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


async def select_items(conn: Connection, table: str, thread_id: str) -> list[ResponseItem]:
    """按写入顺序取回一个线程的全部消息。"""
    items, _ = await select_items_with_max_position(conn, table, thread_id)
    return items


__all__ = [
    "PositionClock",
    "insert_items",
    "item_to_row",
    "row_to_item",
    "select_items",
    "select_items_with_max_position",
]
