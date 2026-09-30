"""``PostgresMessageMirror``：钩子行为，以及挂在内核 ``EnginePool`` 上真跑。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import taifeng
from taifeng import IndexHook, ResponseItem, ThreadMetadata, ThreadNotFoundError, user_message

from taifeng_stores.postgres import (
    PostgresMessageMirror,
    PostgresMessageStore,
    PostgresThreadDirectory,
)
from tests.support import ScriptedModelClient, run_turn, write_skills

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg

    from taifeng_stores import SqlNamespace


@pytest.fixture
def mirror(pg_pool: asyncpg.Pool, namespace: SqlNamespace) -> PostgresMessageMirror:
    """已迁移好的镜像。"""
    return PostgresMessageMirror(pg_pool, namespace=namespace)


@pytest.fixture
def reader(pg_pool: asyncpg.Pool, namespace: SqlNamespace) -> PostgresMessageStore:
    """从同一个命名空间读镜像内容。"""
    return PostgresMessageStore(pg_pool, namespace=namespace)


def _meta(thread_id: str, updated_at: float = 100.0) -> ThreadMetadata:
    """构造元数据。"""
    return ThreadMetadata(
        thread_id=thread_id, created_at=100.0, updated_at=updated_at, entry_skill_id="helper"
    )


async def _texts(reader: PostgresMessageStore, thread_id: str) -> list[str]:
    """镜像里某线程的消息正文。"""
    return [item.payload["text"] async for item in await reader.load_thread(thread_id)]


def test_implements_protocol(mirror: PostgresMessageMirror) -> None:
    """满足 ``IndexHook`` 协议。"""
    assert isinstance(mirror, IndexHook)


async def test_thread_and_messages_are_mirrored(
    mirror: PostgresMessageMirror, reader: PostgresMessageStore
) -> None:
    """线程元数据与消息都进镜像。"""
    await mirror.on_thread_created(_meta("thr_1"))
    await mirror.on_message_appended(
        "thr_1", [user_message("a", thread_id="thr_1"), user_message("b", thread_id="thr_1")]
    )
    assert await reader.directory.get_metadata("thr_1") is not None
    assert await _texts(reader, "thr_1") == ["a", "b"]


async def test_redelivery_is_deduplicated(
    mirror: PostgresMessageMirror, reader: PostgresMessageStore
) -> None:
    """同一批消息重复投递不产生重复行。"""
    await mirror.on_thread_created(_meta("thr_1"))
    batch = [user_message("a", thread_id="thr_1"), user_message("b", thread_id="thr_1")]
    await mirror.on_message_appended("thr_1", batch)
    await mirror.on_message_appended("thr_1", batch)
    assert await _texts(reader, "thr_1") == ["a", "b"]


async def test_activity_time_only_moves_forward(
    mirror: PostgresMessageMirror, reader: PostgresMessageStore
) -> None:
    """活跃时间取消息时间，乱序到达的旧批次不会把它拨回去。"""
    await mirror.on_thread_created(_meta("thr_1", updated_at=100.0))
    newer = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    older = newer - timedelta(hours=1)
    await mirror.on_message_appended(
        "thr_1", [ResponseItem(kind="user_message", thread_id="thr_1", created_at=newer)]
    )
    await mirror.on_message_appended(
        "thr_1", [ResponseItem(kind="user_message", thread_id="thr_1", created_at=older)]
    )
    meta = await reader.directory.get_metadata("thr_1")
    assert meta is not None
    assert meta.updated_at == newer.timestamp()


async def test_messages_before_thread_registration_are_kept(
    mirror: PostgresMessageMirror, reader: PostgresMessageStore
) -> None:
    """钩子是并发触发的：消息先于线程登记到达时也要留住。"""
    await mirror.on_message_appended("thr_late", [user_message("early", thread_id="thr_late")])
    await mirror.on_thread_created(_meta("thr_late"))
    assert await _texts(reader, "thr_late") == ["early"]


async def test_metadata_patch_is_mirrored(
    mirror: PostgresMessageMirror, reader: PostgresMessageStore
) -> None:
    """元数据补丁同步到镜像；线程尚未镜像时明确报错而不是悄悄丢掉。"""
    await mirror.on_thread_created(_meta("thr_1"))
    await mirror.on_metadata_updated("thr_1", {"tags": ["vip"]})
    meta = await reader.directory.get_metadata("thr_1")
    assert meta is not None
    assert meta.tags == ("vip",)
    with pytest.raises(ThreadNotFoundError):
        await mirror.on_metadata_updated("thr_unknown", {"tags": ["x"]})


async def test_empty_batch_is_noop(mirror: PostgresMessageMirror) -> None:
    """空批次不做任何事。"""
    await mirror.on_message_appended("thr_1", [])


async def test_engine_pool_with_shared_directory_and_mirror(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, tmp_path: Path
) -> None:
    """挂在内核 ``EnginePool`` 上：主存仍是本机 JSONL，目录与消息同步进 Postgres。"""
    directory = PostgresThreadDirectory(pg_pool, namespace=namespace)
    pool = await taifeng.EnginePool.create(
        skills_dir=write_skills(tmp_path),
        storage_dir=tmp_path / "threads",
        model_client=ScriptedModelClient(["镜像里也应该有这句"]),
        compressors=[],
        thread_directory=directory,
        index_hook=PostgresMessageMirror(pg_pool, namespace=namespace),
    )
    try:
        engine = await pool.get_or_create(session_id="s1", entry_skill_id="helper")
        events = await run_turn(engine, "你好")
        assert events[-1].msg.kind == "turn_completed"
        thread_id = engine.thread_id
    finally:
        await pool.close()

    reader = PostgresMessageStore(pg_pool, namespace=namespace)

    async def mirrored() -> list[tuple[str, object]]:
        return [
            (item.kind, item.payload.get("text"))
            async for item in await reader.load_thread(thread_id)
        ]

    # 钩子是 fire-and-forget 的，pool.close 会等它们收尾；这里再留一点余量
    for _ in range(50):
        if len(await mirrored()) >= 2:
            break
        await asyncio.sleep(0.1)
    assert await mirrored() == [
        ("user_message", "你好"),
        ("assistant_message", "镜像里也应该有这句"),
    ]
    page = await directory.list_threads()
    assert [meta.thread_id for meta in page.items] == [thread_id]
    assert page.items[0].entry_skill_id == "helper"
