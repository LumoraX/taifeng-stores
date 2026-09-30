"""把会话消息镜像到 Postgres 的 ``IndexHook`` 实现。

``taifeng.EnginePool`` 的会话主存是节点本地的 JSONL。多实例部署时，其他节点、审计与查询
服务需要一个可共享的消息视图；本类订阅线程生命周期事件，把元数据与消息同步写进 Postgres。

这是**镜像**，不是主存：

- 内核以 fire-and-forget 方式调用钩子，写入失败不影响会话，只会让镜像缺数据
  （内核会发 ``index_hook_failed`` 事件）；
- 同一条消息重复到达时去重；
- 回放顺序按钩子被调用的先后，而不是各次写入落库的先后；
- 镜像落后或缺失时，以节点本地的 JSONL 为准。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from taifeng_stores.namespace import SqlNamespace
from taifeng_stores.postgres.messages import PositionClock, insert_items
from taifeng_stores.postgres.migrate import ensure_schema_current
from taifeng_stores.postgres.thread_directory import PostgresThreadDirectory

if TYPE_CHECKING:
    import asyncpg
    from taifeng import ResponseItem, ThreadMetadata


class PostgresMessageMirror:
    """线程与消息的 Postgres 镜像。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace | None = None) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池。
            namespace: 表所在的 schema 与表名前缀；None 用默认值。
        """
        self._pool = pool
        self._namespace = namespace or SqlNamespace()
        self._messages = self._namespace.table("message")
        self._threads = self._namespace.table("thread")
        self._directory = PostgresThreadDirectory(pool, namespace=self._namespace)
        self._clock = PositionClock()

    async def check_schema(self) -> None:
        """核对表结构版本；落后时抛 ``SchemaOutOfDateError``。"""
        await ensure_schema_current(self._pool, self._namespace, "core")

    async def on_thread_created(self, meta: ThreadMetadata) -> None:
        """登记新线程的元数据。"""
        await self._directory.upsert_metadata(meta)

    async def on_message_appended(self, thread_id: str, items: list[ResponseItem]) -> None:
        """镜像一批消息，并刷新线程活跃时间。"""
        if not items:
            return
        # 必须在第一个 await 之前取位置：内核并发触发钩子，落库先后不等于对话顺序
        position = self._clock.next()
        latest = max(item.created_at for item in items).timestamp()
        async with self._pool.acquire() as conn, conn.transaction():
            await insert_items(conn, self._messages, items, position=position)
            # 只前进不后退：乱序到达的旧批次不会把活跃时间拨回去
            await conn.execute(
                f"UPDATE {self._threads} SET updated_at = GREATEST(updated_at, $1) "
                "WHERE thread_id = $2",
                latest,
                thread_id,
            )

    async def on_metadata_updated(self, thread_id: str, patch: dict[str, Any]) -> None:
        """同步元数据补丁；线程尚未镜像时由目录抛 ``ThreadNotFoundError``。"""
        await self._directory.update_metadata(thread_id, patch)


__all__ = ["PostgresMessageMirror"]
