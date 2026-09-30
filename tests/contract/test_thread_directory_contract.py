"""``ThreadDirectory`` 契约测试：同一套用例对所有后端执行。

用例依据 taifeng ``docs/architecture/capabilities/thread-directory.md``。内核自带的
``SqliteThreadDirectory`` 也作为一个后端参与——它是参照实现，用来证明这套用例本身
没有偏离内核语义。
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any

import pytest
from taifeng import (
    SqliteThreadDirectory,
    ThreadDirectory,
    ThreadFilter,
    ThreadMetadata,
    ThreadNotFoundError,
)

from taifeng_stores.postgres import PostgresThreadDirectory
from taifeng_stores.redis import RedisThreadDirectory
from tests.conftest import migrate_all, open_pool, open_redis, scratch_namespace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

BACKENDS = ["postgres", "redis", "sqlite-reference"]


def meta(
    thread_id: str,
    *,
    updated_at: float = 100.0,
    created_at: float | None = None,
    entry_skill_id: str = "entry",
    source: str = "user",
    tags: tuple[str, ...] = (),
    extra: dict[str, Any] | None = None,
) -> ThreadMetadata:
    """构造元数据。"""
    return ThreadMetadata(
        thread_id=thread_id,
        created_at=updated_at if created_at is None else created_at,
        updated_at=updated_at,
        entry_skill_id=entry_skill_id,
        source=source,
        tags=tags,
        extra=extra or {},
    )


class SqliteReference:
    """内核默认实现的薄包装。

    默认实现会把「索引里有、本机 JSONL 主存里没有」的线程当成孤儿过滤掉，所以写入元数据时
    顺手放一个空的主存文件；其余方法原样转发。
    """

    def __init__(self, inner: SqliteThreadDirectory, threads_dir: Path) -> None:
        """包装内核实现。"""
        self._inner = inner
        self._threads_dir = threads_dir

    async def upsert_metadata(self, value: ThreadMetadata) -> None:
        """写入元数据并补上主存文件。"""
        (self._threads_dir / f"{value.thread_id}.jsonl").touch()
        await self._inner.upsert_metadata(value)

    def __getattr__(self, name: str) -> Any:
        """其余方法转发给内核实现。"""
        return getattr(self._inner, name)


@pytest.fixture(params=BACKENDS)
async def directory(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    """按参数给出一个空的目录后端。"""
    if request.param == "postgres":
        backend = request.getfixturevalue("postgres_backend")
        async with open_pool(backend.url) as pool, scratch_namespace(pool) as namespace:
            await migrate_all(pool, namespace)
            yield PostgresThreadDirectory(pool, namespace=namespace)
    elif request.param == "redis":
        backend = request.getfixturevalue("redis_backend")
        async with open_redis(backend.url) as (client, prefix):
            yield RedisThreadDirectory(client, prefix=prefix)
    else:
        inner = SqliteThreadDirectory(tmp_path / "index.db", threads_dir=tmp_path)
        try:
            yield SqliteReference(inner, tmp_path)
        finally:
            await inner.close()


def is_reference(directory: Any) -> bool:
    """是否是内核参照实现。"""
    return isinstance(directory, SqliteReference)


async def collect_all(directory: Any, *, limit: int, **kwargs: Any) -> list[list[str]]:
    """一页页翻到底，返回每页的线程 id。"""
    pages: list[list[str]] = []
    cursor: str | None = None
    for _ in range(1000):
        page = await directory.list_threads(limit=limit, cursor=cursor, **kwargs)
        if page.items:
            pages.append([item.thread_id for item in page.items])
        cursor = page.next_cursor
        if cursor is None:
            return pages
    raise AssertionError("翻页没有终止")


async def test_implements_protocol(directory: Any) -> None:
    """实现满足 ``ThreadDirectory`` 协议。"""
    target = directory._inner if is_reference(directory) else directory  # noqa: SLF001
    assert isinstance(target, ThreadDirectory)


async def test_get_missing_returns_none(directory: Any) -> None:
    """不存在的线程返回 None。"""
    assert await directory.get_metadata("nope") is None


async def test_upsert_then_get_roundtrip(directory: Any) -> None:
    """写入后原样读回：浮点时间戳逐位相等，标签保序，extra 支持嵌套与非 ASCII。"""
    value = meta(
        "t1",
        updated_at=1759190400.123456,
        created_at=1759190399.000001,
        source="subskill:parent",
        tags=("b", "a", "中文"),
        extra={"cwd": "/work", "nested": {"k": [1, 2.5, None, True]}, "备注": "引号 ' \" 反斜杠 \\"},
    )
    await directory.upsert_metadata(value)
    assert await directory.get_metadata("t1") == value


async def test_upsert_replaces_whole_record(directory: Any) -> None:
    """已存在时整体替换，旧字段不残留。"""
    await directory.upsert_metadata(meta("t1", tags=("old",), extra={"a": 1, "b": 2}))
    replacement = meta("t1", updated_at=200.0, entry_skill_id="other", extra={"c": 3})
    await directory.upsert_metadata(replacement)
    assert await directory.get_metadata("t1") == replacement


async def test_update_merges_patch_and_refreshes_updated_at(directory: Any) -> None:
    """补丁提到的字段整体替换，没提到的保留，``updated_at`` 刷新为当前时间。"""
    await directory.upsert_metadata(
        meta("t1", updated_at=100.0, tags=("keep",), extra={"a": 1, "b": 2})
    )
    await directory.update_metadata("t1", {"extra": {"a": 99}})
    updated = await directory.get_metadata("t1")
    assert updated is not None
    assert updated.extra == {"a": 99}
    assert updated.tags == ("keep",)
    assert updated.created_at == 100.0
    assert updated.entry_skill_id == "entry"
    assert updated.updated_at > 100.0


async def test_update_tags_and_source(directory: Any) -> None:
    """标签与来源也可以经补丁替换。"""
    await directory.upsert_metadata(meta("t1", tags=("a",)))
    await directory.update_metadata("t1", {"tags": ["x", "y"], "source": "system"})
    updated = await directory.get_metadata("t1")
    assert updated is not None
    assert updated.tags == ("x", "y")
    assert updated.source == "system"


async def test_update_missing_raises_not_found(directory: Any) -> None:
    """更新不存在的线程抛 ``ThreadNotFoundError``。"""
    with pytest.raises(ThreadNotFoundError):
        await directory.update_metadata("not-exist", {})


async def test_update_moves_thread_to_front(directory: Any) -> None:
    """被更新的线程排到列表最前。"""
    for index in range(3):
        await directory.upsert_metadata(meta(f"t{index}", updated_at=100.0 + index))
    await directory.update_metadata("t0", {"source": "system"})
    page = await directory.list_threads(limit=10)
    assert [item.thread_id for item in page.items] == ["t0", "t2", "t1"]


async def test_list_empty(directory: Any) -> None:
    """空目录列出空页。"""
    page = await directory.list_threads()
    assert page.items == []
    assert page.next_cursor is None


async def test_list_orders_by_updated_at_desc(directory: Any) -> None:
    """按 ``updated_at`` 倒序；相同时按 ``thread_id`` 倒序。"""
    await directory.upsert_metadata(meta("a", updated_at=100.0))
    await directory.upsert_metadata(meta("c", updated_at=300.0))
    await directory.upsert_metadata(meta("b", updated_at=200.0))
    await directory.upsert_metadata(meta("tie-1", updated_at=50.0))
    await directory.upsert_metadata(meta("tie-2", updated_at=50.0))
    page = await directory.list_threads(limit=10)
    assert [item.thread_id for item in page.items] == ["c", "b", "a", "tie-2", "tie-1"]


async def test_pagination_covers_everything_once(directory: Any) -> None:
    """100 个线程每页 10 个：无重复、无遗漏、顺序正确。"""
    for index in range(100):
        await directory.upsert_metadata(meta(f"t{index:03d}", updated_at=1000.0 + index))
    pages = await collect_all(directory, limit=10)
    flat = [thread_id for page in pages for thread_id in page]
    assert flat == [f"t{index:03d}" for index in range(99, -1, -1)]
    assert all(len(page) <= 10 for page in pages)


async def test_last_page_has_no_cursor(directory: Any) -> None:
    """刚好翻完时最后一页的游标为 None，不会多出一个空页。"""
    if is_reference(directory):
        pytest.skip("内核默认实现在结果恰好填满一页时会多给一个游标（其下一页为空）")
    for index in range(20):
        await directory.upsert_metadata(meta(f"t{index:02d}", updated_at=1000.0 + index))
    first = await directory.list_threads(limit=10)
    assert first.next_cursor is not None
    second = await directory.list_threads(limit=10, cursor=first.next_cursor)
    assert len(second.items) == 10
    assert second.next_cursor is None


async def test_pagination_with_identical_timestamps(directory: Any) -> None:
    """时间戳全部相同时靠 ``thread_id`` 定序，翻页仍然不重不漏。"""
    for index in range(25):
        await directory.upsert_metadata(meta(f"t{index:02d}", updated_at=500.0))
    pages = await collect_all(directory, limit=7)
    flat = [thread_id for page in pages for thread_id in page]
    assert flat == [f"t{index:02d}" for index in range(24, -1, -1)]


async def test_corrupt_cursor_restarts_from_first_page(directory: Any) -> None:
    """游标损坏不抛异常，从首页重新开始。"""
    for index in range(3):
        await directory.upsert_metadata(meta(f"t{index}", updated_at=100.0 + index))
    for broken in ("%%%not-base64%%%", "bm90LWpzb24=", "e30="):
        page = await directory.list_threads(limit=2, cursor=broken)
        assert [item.thread_id for item in page.items] == ["t2", "t1"]


@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_limit_out_of_range_raises(directory: Any, limit: int) -> None:
    """``limit`` 越界抛 ``ValueError``。"""
    with pytest.raises(ValueError, match="limit"):
        await directory.list_threads(limit=limit)


@pytest.mark.parametrize("limit", [1, 1000])
async def test_limit_bounds_accepted(directory: Any, limit: int) -> None:
    """边界值 1 与 1000 合法。"""
    await directory.upsert_metadata(meta("t1"))
    page = await directory.list_threads(limit=limit)
    assert [item.thread_id for item in page.items] == ["t1"]


async def _seed_for_filters(directory: Any) -> None:
    """过滤用例的公共数据。"""
    await directory.upsert_metadata(
        meta("a", updated_at=10.0, created_at=10.0, entry_skill_id="x", tags=("production",))
    )
    await directory.upsert_metadata(
        meta("b", updated_at=20.0, created_at=20.0, entry_skill_id="x", tags=("staging",))
    )
    await directory.upsert_metadata(
        meta("c", updated_at=30.0, created_at=30.0, entry_skill_id="y", tags=("production", "x"))
    )
    await directory.upsert_metadata(
        meta("d", updated_at=40.0, created_at=40.0, entry_skill_id="x", source="subskill:foo")
    )


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        (ThreadFilter(), ["d", "c", "b", "a"]),
        (ThreadFilter(entry_skill_id="x"), ["d", "b", "a"]),
        (ThreadFilter(tag="production"), ["c", "a"]),
        (ThreadFilter(tag="produc"), []),
        (ThreadFilter(source="subskill:foo"), ["d"]),
        (ThreadFilter(entry_skill_id="x", tag="production"), ["a"]),
        (ThreadFilter(created_after=10.0, created_before=40.0), ["c", "b"]),
        (ThreadFilter(created_after=10.0, created_before=40.0, source="user"), ["c", "b"]),
        (ThreadFilter(entry_skill_id="y", source="subskill:foo"), []),
    ],
)
async def test_filters_use_and_semantics(
    directory: Any, criteria: ThreadFilter, expected: list[str]
) -> None:
    """多个过滤条件是 AND 关系；时间窗两端是开区间；标签是整项匹配。"""
    await _seed_for_filters(directory)
    page = await directory.list_threads(limit=10, filter=criteria)
    assert [item.thread_id for item in page.items] == expected


async def test_filter_combined_with_pagination(directory: Any) -> None:
    """过滤与翻页叠加：只在命中的线程里翻页。"""
    for index in range(30):
        skill = "even" if index % 2 == 0 else "odd"
        await directory.upsert_metadata(
            meta(f"t{index:02d}", updated_at=100.0 + index, entry_skill_id=skill)
        )
    pages = await collect_all(directory, limit=4, filter=ThreadFilter(entry_skill_id="even"))
    flat = [thread_id for page in pages for thread_id in page]
    assert flat == [f"t{index:02d}" for index in range(28, -1, -2)]


async def test_hostile_strings_are_data_not_syntax(directory: Any) -> None:
    """带引号、分号、通配符的取值只是数据。"""
    hostile = "x'; DROP TABLE thread; --"
    await directory.upsert_metadata(
        meta("t1", entry_skill_id=hostile, source=hostile, tags=(hostile, "*"))
    )
    await directory.upsert_metadata(meta("t2"))
    page = await directory.list_threads(filter=ThreadFilter(entry_skill_id=hostile, tag=hostile))
    assert [item.thread_id for item in page.items] == ["t1"]
    page = await directory.list_threads(filter=ThreadFilter(tag="%"))
    assert page.items == []
    assert await directory.get_metadata("t2") is not None


async def test_concurrent_upserts(directory: Any) -> None:
    """并发写入互不丢失。"""
    await asyncio.gather(
        *(directory.upsert_metadata(meta(f"t{index:03d}", updated_at=float(index))) for index in range(60))
    )
    pages = await collect_all(directory, limit=25)
    assert sum(len(page) for page in pages) == 60


async def test_concurrent_updates_keep_record_consistent(directory: Any) -> None:
    """并发补丁之后记录仍然完整：每个字段的取值都来自某一次完整的补丁。"""
    await directory.upsert_metadata(meta("t1", tags=("init",), extra={"n": -1}))
    await asyncio.gather(
        *(
            directory.update_metadata("t1", {"extra": {"n": index}, "tags": [f"tag-{index}"]})
            for index in range(20)
        )
    )
    final = await directory.get_metadata("t1")
    assert final is not None
    assert final.extra["n"] in range(20)
    assert final.tags == (f"tag-{final.extra['n']}",)
    assert dataclasses.replace(final, extra={}, tags=(), updated_at=0.0) == meta(
        "t1", updated_at=0.0, created_at=100.0
    )
