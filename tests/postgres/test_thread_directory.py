"""``PostgresThreadDirectory`` 特有的行为（通用契约见 ``tests/contract``）。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import asyncpg
import pytest
from taifeng import DirectoryError, ThreadFilter, ThreadMetadata

from taifeng_stores.postgres import PostgresThreadDirectory
from taifeng_stores.postgres.thread_directory import build_list_query

if TYPE_CHECKING:
    from taifeng_stores import SqlNamespace
    from tests.backends import Backend


def _meta(thread_id: str) -> ThreadMetadata:
    """构造元数据。"""
    return ThreadMetadata(
        thread_id=thread_id,
        created_at=1.0,
        updated_at=1.0,
        entry_skill_id="entry",
        tags=("a",),
        extra={"k": "v"},
    )


def test_list_query_binds_every_value() -> None:
    """过滤值与游标全部走参数绑定，SQL 文本里不出现取值。"""
    sql, params = build_list_query(
        '"s"."thread"',
        limit=10,
        cursor=(5.0, "thr_x"),
        filter=ThreadFilter(entry_skill_id="x'y", tag="t", source="s", created_after=1.0),
    )
    assert "x'y" not in sql
    assert "thr_x" not in sql
    assert params == ["x'y", "s", 1.0, '["t"]', 5.0, "thr_x", 11]
    assert sql.count("$") == 8


async def test_unmigrated_database_raises_directory_error(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """表不存在时归类为 ``DirectoryError``，原始异常挂在 ``__cause__`` 上。"""
    directory = PostgresThreadDirectory(pg_pool, namespace=empty_namespace)
    for call in (
        directory.get_metadata("t1"),
        directory.list_threads(),
        directory.upsert_metadata(_meta("t1")),
        directory.update_metadata("t1", {}),
    ):
        with pytest.raises(DirectoryError) as caught:
            await call
        assert isinstance(caught.value.__cause__, asyncpg.PostgresError)


async def test_works_when_host_pool_registers_json_codec(
    postgres_backend: Backend, namespace: SqlNamespace
) -> None:
    """宿主的连接池注册了 jsonb 编解码器时读写结果不变。"""

    async def register(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    pool = await asyncpg.create_pool(postgres_backend.url, min_size=1, max_size=2, init=register)
    try:
        directory = PostgresThreadDirectory(pool, namespace=namespace)
        await directory.upsert_metadata(_meta("t1"))
        assert await directory.get_metadata("t1") == _meta("t1")
        page = await directory.list_threads(filter=ThreadFilter(tag="a"))
        assert [item.thread_id for item in page.items] == ["t1"]
    finally:
        await pool.close()
