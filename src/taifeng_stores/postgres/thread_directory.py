"""``ThreadDirectory`` 的 Postgres 实现。

以 taifeng ``examples/persistence/postgres_thread_directory.py`` 为起点（ADR 0001 决策 5），
补齐的差异：

- 连接池由宿主注入，本类不建连接、不读环境变量；
- ``update_metadata`` 在一个事务里「加行锁 → 合并 → 写回」，并发更新不会互相覆盖；
- 翻页多取一行来判断是否还有下一页，最后一页的 ``next_cursor`` 一定是 None；
- JSON 列经 ``text`` 中转读写，不依赖连接池上是否注册了 jsonb 编解码器；
- 底层错误归类为 ``DirectoryError``。

与内核默认实现的差异：不做孤儿检测（那是「索引与本机 JSONL 主存对账」，外部目录没有可对账
的本机文件）；游标损坏时写日志而不是发事件（事件出口协议尚未公开）。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import asyncpg
from taifeng import DirectoryError, ThreadMetadata, ThreadNotFoundError, ThreadPage

from taifeng_stores._cursor import decode_cursor, encode_cursor, validate_limit
from taifeng_stores.namespace import SqlNamespace
from taifeng_stores.postgres.migrate import ensure_schema_current

if TYPE_CHECKING:
    from asyncpg.pool import PoolConnectionProxy
    from taifeng import ThreadFilter

    Connection = asyncpg.Connection | PoolConnectionProxy

_COLUMNS = (
    "thread_id, created_at, updated_at, entry_skill_id, source, "
    "tags::text AS tags, extra::text AS extra"
)


def row_to_metadata(row: asyncpg.Record) -> ThreadMetadata:
    """数据库行 → ``ThreadMetadata``。"""
    return ThreadMetadata(
        thread_id=str(row["thread_id"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        entry_skill_id=str(row["entry_skill_id"]),
        source=str(row["source"]),
        tags=tuple(json.loads(row["tags"])),
        extra=dict(json.loads(row["extra"])),
    )


def _dump(value: Any) -> str:
    """序列化 JSON 列。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def merge_patch(old: ThreadMetadata, patch: dict[str, Any], *, now: float) -> ThreadMetadata:
    """按契约合并补丁：提到的字段整体替换，没提到的保留，``updated_at`` 刷新。"""
    return ThreadMetadata(
        thread_id=old.thread_id,
        created_at=old.created_at,
        updated_at=now,
        entry_skill_id=patch.get("entry_skill_id", old.entry_skill_id),
        source=patch.get("source", old.source),
        tags=tuple(patch.get("tags", old.tags)),
        extra=dict(patch.get("extra", old.extra)),
    )


def build_list_query(
    table: str,
    *,
    limit: int,
    cursor: tuple[float, str] | None,
    filter: ThreadFilter | None,  # noqa: A002 —— 与协议参数同名
) -> tuple[str, list[Any]]:
    """拼出列表查询。多个过滤条件是 AND 关系；多取一行用来判断有没有下一页。"""
    where: list[str] = []
    params: list[Any] = []

    def bind(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if filter is not None:
        if filter.entry_skill_id is not None:
            where.append(f"entry_skill_id = {bind(filter.entry_skill_id)}")
        if filter.source is not None:
            where.append(f"source = {bind(filter.source)}")
        if filter.created_after is not None:
            where.append(f"created_at > {bind(filter.created_after)}")
        if filter.created_before is not None:
            where.append(f"created_at < {bind(filter.created_before)}")
        if filter.tag is not None:
            where.append(f"tags @> {bind(_dump([filter.tag]))}::text::jsonb")
    if cursor is not None:
        at = bind(cursor[0])
        tid = bind(cursor[1])
        where.append(f"(updated_at < {at} OR (updated_at = {at} AND thread_id < {tid}))")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {_COLUMNS} FROM {table}{where_sql} "
        f"ORDER BY updated_at DESC, thread_id DESC LIMIT {bind(limit + 1)}"
    )
    return sql, params


class PostgresThreadDirectory:
    """线程元数据目录，可多实例共享。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace | None = None) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池。
            namespace: 表所在的 schema 与表名前缀；None 用默认值。
        """
        self._pool = pool
        self._namespace = namespace or SqlNamespace()
        self._table = self._namespace.table("thread")

    @property
    def namespace(self) -> SqlNamespace:
        """生效的命名空间。"""
        return self._namespace

    async def check_schema(self) -> None:
        """核对表结构版本；落后时抛 ``SchemaOutOfDateError``。建议宿主启动时调用一次。"""
        await ensure_schema_current(self._pool, self._namespace, "core")

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,  # noqa: A002 —— 与协议参数同名
    ) -> ThreadPage:
        """按 ``updated_at`` 倒序、过滤、游标分页列出线程。"""
        validate_limit(limit)
        sql, params = build_list_query(
            self._table, limit=limit, cursor=decode_cursor(cursor), filter=filter
        )
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
        except (asyncpg.PostgresError, OSError) as exc:
            raise DirectoryError(f"列出线程失败：{exc}") from exc
        items = [row_to_metadata(row) for row in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = encode_cursor(last.updated_at, last.thread_id)
        return ThreadPage(items=items, next_cursor=next_cursor)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        """按 id 取元数据；不存在返回 None。"""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT {_COLUMNS} FROM {self._table} WHERE thread_id = $1", thread_id
                )
        except (asyncpg.PostgresError, OSError) as exc:
            raise DirectoryError(f"读取线程 {thread_id!r} 的元数据失败：{exc}") from exc
        return row_to_metadata(row) if row is not None else None

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        """部分合并补丁并刷新 ``updated_at``；线程不存在抛 ``ThreadNotFoundError``。"""
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT {_COLUMNS} FROM {self._table} WHERE thread_id = $1 FOR UPDATE",
                    thread_id,
                )
                if row is None:
                    raise ThreadNotFoundError(thread_id)
                merged = merge_patch(row_to_metadata(row), patch, now=time.time())
                await self._write(conn, merged)
        except (asyncpg.PostgresError, OSError) as exc:
            raise DirectoryError(f"更新线程 {thread_id!r} 的元数据失败：{exc}") from exc

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        """不存在则插入，存在则整体替换。"""
        try:
            async with self._pool.acquire() as conn:
                await self._write(conn, meta)
        except (asyncpg.PostgresError, OSError) as exc:
            raise DirectoryError(f"写入线程 {meta.thread_id!r} 的元数据失败：{exc}") from exc

    async def _write(self, conn: Connection, meta: ThreadMetadata) -> None:
        """整行写入（插入或覆盖）。"""
        await conn.execute(
            f"INSERT INTO {self._table} "
            "(thread_id, created_at, updated_at, entry_skill_id, source, tags, extra) "
            "VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, $7::text::jsonb) "
            "ON CONFLICT (thread_id) DO UPDATE SET "
            "created_at = EXCLUDED.created_at, updated_at = EXCLUDED.updated_at, "
            "entry_skill_id = EXCLUDED.entry_skill_id, source = EXCLUDED.source, "
            "tags = EXCLUDED.tags, extra = EXCLUDED.extra",
            meta.thread_id,
            meta.created_at,
            meta.updated_at,
            meta.entry_skill_id,
            meta.source,
            _dump(list(meta.tags)),
            _dump(meta.extra),
        )


__all__ = ["PostgresThreadDirectory", "build_list_query", "merge_patch", "row_to_metadata"]
