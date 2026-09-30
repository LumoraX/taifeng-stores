"""``RedisThreadDirectory`` 特有的行为（通用契约见 ``tests/contract``）。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import redis.asyncio as redis_asyncio
from taifeng import DirectoryError, ThreadFilter, ThreadMetadata

from taifeng_stores import StoreConfigError
from taifeng_stores.redis import RedisThreadDirectory

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from tests.backends import Backend


def _meta(thread_id: str, updated_at: float = 100.0, **kwargs: object) -> ThreadMetadata:
    """构造元数据。"""
    return ThreadMetadata(
        thread_id=thread_id,
        created_at=updated_at,
        updated_at=updated_at,
        entry_skill_id="entry",
        **kwargs,  # type: ignore[arg-type]
    )


def test_invalid_prefix_rejected(redis_scope: tuple[Redis, str]) -> None:
    """键前缀受字符集约束。"""
    client, _ = redis_scope
    with pytest.raises(StoreConfigError):
        RedisThreadDirectory(client, prefix="bad prefix*")


async def test_prefixes_isolate_data(redis_scope: tuple[Redis, str]) -> None:
    """不同前缀的数据互不可见。"""
    client, prefix = redis_scope
    first = RedisThreadDirectory(client, prefix=f"{prefix}:a")
    second = RedisThreadDirectory(client, prefix=f"{prefix}:b")
    await first.upsert_metadata(_meta("t1"))
    assert await second.get_metadata("t1") is None
    assert (await second.list_threads()).items == []


async def test_works_with_decoding_client(redis_backend: Backend, redis_scope: tuple[Redis, str]) -> None:
    """宿主的客户端开了 ``decode_responses`` 也能用，且与不开的客户端读到同样的数据。"""
    raw_client, prefix = redis_scope
    decoding: Redis = redis_asyncio.from_url(redis_backend.url, decode_responses=True)
    try:
        value = _meta("t1", tags=("中文", "b"), extra={"k": "值"})
        await RedisThreadDirectory(decoding, prefix=prefix).upsert_metadata(value)
        assert await RedisThreadDirectory(raw_client, prefix=prefix).get_metadata("t1") == value
        await RedisThreadDirectory(raw_client, prefix=prefix).update_metadata("t1", {"source": "s"})
        page = await RedisThreadDirectory(decoding, prefix=prefix).list_threads(
            filter=ThreadFilter(tag="中文")
        )
        assert [item.source for item in page.items] == ["s"]
    finally:
        await decoding.aclose()


async def test_upsert_drops_stale_fields(redis_scope: tuple[Redis, str]) -> None:
    """整体替换：哈希里以前残留的多余字段不会留下来。"""
    client, prefix = redis_scope
    directory = RedisThreadDirectory(client, prefix=prefix)
    await client.hset(f"{prefix}:thread:t1", mapping={"legacy_field": "x"})
    await directory.upsert_metadata(_meta("t1"))
    fields = await client.hkeys(f"{prefix}:thread:t1")
    assert b"legacy_field" not in fields


async def test_index_entry_without_hash_is_skipped(redis_scope: tuple[Redis, str]) -> None:
    """索引里有、哈希却被外部删掉的线程，列表时跳过而不是报错。"""
    client, prefix = redis_scope
    directory = RedisThreadDirectory(client, prefix=prefix)
    await directory.upsert_metadata(_meta("t1", updated_at=1.0))
    await directory.upsert_metadata(_meta("t2", updated_at=2.0))
    await client.delete(f"{prefix}:thread:t2")
    page = await directory.list_threads()
    assert [item.thread_id for item in page.items] == ["t1"]


async def test_delete_thread(redis_scope: tuple[Redis, str]) -> None:
    """删除线程同时清掉哈希与索引项。"""
    client, prefix = redis_scope
    directory = RedisThreadDirectory(client, prefix=prefix)
    await directory.upsert_metadata(_meta("t1"))
    assert await directory.delete_thread("t1") is True
    assert await directory.delete_thread("t1") is False
    assert await directory.get_metadata("t1") is None
    assert (await directory.list_threads()).items == []
    assert await client.zcard(f"{prefix}:threads") == 0


async def test_scan_spans_multiple_batches(redis_scope: tuple[Redis, str]) -> None:
    """命中的线程稀疏分布时，扫描会跨多个批次继续找。"""
    client, prefix = redis_scope
    directory = RedisThreadDirectory(client, prefix=prefix)
    for index in range(450):
        tags = ("rare",) if index % 150 == 0 else ()
        await directory.upsert_metadata(_meta(f"t{index:03d}", updated_at=float(index), tags=tags))
    page = await directory.list_threads(limit=10, filter=ThreadFilter(tag="rare"))
    assert [item.thread_id for item in page.items] == ["t300", "t150", "t000"]
    assert page.next_cursor is None


async def test_unreachable_server_raises_directory_error() -> None:
    """连不上服务时归类为 ``DirectoryError``，原始异常挂在 ``__cause__`` 上。"""
    client: Redis = redis_asyncio.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.5, retry=None
    )
    directory = RedisThreadDirectory(client, prefix="x")
    try:
        for call in (
            directory.get_metadata("t1"),
            directory.list_threads(),
            directory.upsert_metadata(_meta("t1")),
            directory.update_metadata("t1", {}),
        ):
            with pytest.raises(DirectoryError) as caught:
                await call
            assert caught.value.__cause__ is not None
    finally:
        await client.aclose()
