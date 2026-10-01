"""``MessageStore`` 的 Postgres 实现。

会话消息与线程元数据落在同一个库里：建线程、追加消息、刷新活跃时间都在事务内完成。

适用范围（重要）：

- 可以直接交给 ``taifeng.AgentEngine(store=...)``，配 Chat 协议的模型客户端；
- **不能**配 Responses 协议的模型客户端：那需要原子批量写协议，taifeng 尚未公开（ADR 0002）；
- ``taifeng.EnginePool.create`` 目前固定使用本机 JSONL 主存，不接受外部 ``MessageStore``。
  经 ``EnginePool`` 承载会话的宿主，用 ``PostgresThreadDirectory`` 加 ``PostgresMessageMirror``
  获得可共享的目录与消息视图。
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from taifeng import ThreadInfo, ThreadMetadata

from taifeng_stores.namespace import SqlNamespace
from taifeng_stores.postgres.messages import (
    PositionClock,
    insert_items,
    select_items_with_max_position,
)
from taifeng_stores.postgres.migrate import ensure_schema_current
from taifeng_stores.postgres.thread_directory import PostgresThreadDirectory, row_to_metadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import asyncpg
    from taifeng import ResponseItem

# 与内核默认实现一致的兜底值
_DEFAULT_ENTRY_SKILL = "general"
_DEFAULT_SOURCE = "user"


def generate_thread_id() -> str:
    """生成线程 id，格式与内核默认实现一致。"""
    return f"thr_{secrets.token_hex(8)}"


def to_thread_info(meta: ThreadMetadata) -> ThreadInfo:
    """``ThreadMetadata`` → 旧式 ``ThreadInfo``。"""
    return ThreadInfo(
        thread_id=meta.thread_id,
        cwd=meta.extra.get("cwd"),
        created_at=datetime.fromtimestamp(meta.created_at, tz=UTC),
        last_activity_at=datetime.fromtimestamp(meta.updated_at, tz=UTC),
        item_count=0,
        entry_skill_id=meta.entry_skill_id,
        source=meta.source,
    )


class PostgresMessageStore:
    """会话消息存储，可多实例共享。

    同一线程同一时刻只能有一个实例写入（由宿主保证，例如租约）。实例接管线程时先
    ``load_thread`` 回放再追加：回放记下库里已有的最大位置，之后的写入排在它后面，
    两个实例的时钟偏差不会打乱回放顺序。
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        namespace: SqlNamespace | None = None,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池。``close`` 不会关闭它。
            namespace: 表所在的 schema 与表名前缀；None 用默认值。
            now_ns: 取位置序号用的墙钟（纳秒时间戳），只为测试注入时钟偏差；
                None 用 ``time.time_ns``。
        """
        self._pool = pool
        self._namespace = namespace or SqlNamespace()
        self._messages = self._namespace.table("message")
        self._threads = self._namespace.table("thread")
        self._directory = PostgresThreadDirectory(pool, namespace=self._namespace)
        self._clock = PositionClock(time.time_ns if now_ns is None else now_ns)

    @property
    def directory(self) -> PostgresThreadDirectory:
        """与本存储同库、同命名空间的线程目录。"""
        return self._directory

    async def check_schema(self) -> None:
        """核对表结构版本；落后时抛 ``SchemaOutOfDateError``。"""
        await ensure_schema_current(self._pool, self._namespace, "core")

    async def create_thread(
        self,
        *,
        cwd: str | None = None,
        entry_skill_id: str | None = None,
        source: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """创建线程并登记元数据，返回线程 id。"""
        merged: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
        if extra:
            merged.update(extra)
        now = time.time()
        meta = ThreadMetadata(
            thread_id=generate_thread_id(),
            created_at=now,
            updated_at=now,
            entry_skill_id=entry_skill_id or _DEFAULT_ENTRY_SKILL,
            source=source or _DEFAULT_SOURCE,
            tags=(),
            extra=merged,
        )
        await self._directory.upsert_metadata(meta)
        return meta.thread_id

    async def append(self, item: ResponseItem) -> None:
        """追加单条消息。"""
        await self.append_batch([item])

    async def append_batch(self, items: list[ResponseItem]) -> None:
        """批量追加：整批在一个事务里，要么全部可见，要么全部不可见。"""
        if not items:
            return
        # 必须在第一个 await 之前取位置：并发追加按「调用进入顺序」回放；
        # 带上涉及的线程，位置才会排在回放时观察到的已有消息之后
        thread_ids = sorted({item.thread_id for item in items})
        position = self._clock.next(thread_ids)
        async with self._pool.acquire() as conn, conn.transaction():
            await insert_items(conn, self._messages, items, position=position)
            # 刷新活跃时间，列表才会把最近有消息的线程排在前面
            await conn.execute(
                f"UPDATE {self._threads} SET updated_at = $1 WHERE thread_id = ANY($2::text[])",
                time.time(),
                thread_ids,
            )

    async def load_thread(self, thread_id: str) -> AsyncIterator[ResponseItem]:
        """按写入顺序回放线程的全部消息；线程不存在时得到空序列。

        同时记下库里已有的最大位置：本实例之后对该线程的写入一定排在这些消息之后。
        """
        async with self._pool.acquire() as conn:
            items, max_position = await select_items_with_max_position(
                conn, self._messages, thread_id
            )
        if max_position is not None:
            self._clock.observe(thread_id, max_position)

        async def _replay() -> AsyncIterator[ResponseItem]:
            for item in items:
                yield item

        return _replay()

    async def list_threads(self, *, cwd: str | None = None, limit: int = 50) -> list[ThreadInfo]:
        """列出最近活跃的线程，可按 ``cwd`` 过滤。"""
        if limit < 1:
            raise ValueError(f"limit must be positive; got {limit}")
        if cwd is None:
            page = await self._directory.list_threads(limit=min(limit, 1000))
            return [to_thread_info(meta) for meta in page.items]
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT thread_id, created_at, updated_at, entry_skill_id, source, "
                f"tags::text AS tags, extra::text AS extra FROM {self._threads} "
                "WHERE extra @> $1::text::jsonb "
                "ORDER BY updated_at DESC, thread_id DESC LIMIT $2",
                json.dumps({"cwd": cwd}),
                limit,
            )
        return [to_thread_info(row_to_metadata(row)) for row in rows]

    async def select_resume_path(self, cwd: str) -> str | None:
        """取该 ``cwd`` 下最近活跃的线程 id；没有返回 None。"""
        threads = await self.list_threads(cwd=cwd, limit=1)
        return threads[0].thread_id if threads else None

    async def close(self) -> None:
        """连接池由宿主管理，这里不关闭它。"""
        return


__all__ = ["PostgresMessageStore", "generate_thread_id", "to_thread_info"]
