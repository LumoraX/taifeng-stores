"""``ThreadDirectory`` 的 Redis 实现。

以 taifeng ``examples/persistence/redis_thread_directory.py`` 为起点（ADR 0001 决策 5），
把其中的待办补全。数据布局：

- ``<前缀>:thread:<thread_id>``：哈希，存元数据各字段；
- ``<前缀>:threads``：有序集合，分值是 ``updated_at``，成员是 ``thread_id``，用来排序翻页。

适用范围：线程数量不大、过滤条件简单的场景。Redis 里没有二级索引，带过滤条件的列表是
「按时间倒序扫描、逐条比对」，代价随线程总数增长；需要按标签、按入口 skill 高效过滤时
用 Postgres 后端。
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from redis.exceptions import RedisError
from taifeng import DirectoryError, ThreadMetadata, ThreadNotFoundError, ThreadPage

from taifeng_stores._cursor import decode_cursor, encode_cursor, validate_limit
from taifeng_stores.namespace import validate_key_prefix

if TYPE_CHECKING:
    from collections.abc import Mapping

    from redis.asyncio import Redis
    from taifeng import ThreadFilter

_SCAN_BATCH = 200

# KEYS[1] 线程哈希，KEYS[2] 排序索引；ARGV[1] 线程 id，ARGV[2] 新的 updated_at，
# 其后成对出现「字段名, 字段值」。线程不存在返回 0，不做任何修改。
_UPDATE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
for i = 3, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
redis.call('HSET', KEYS[1], 'updated_at', ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
return 1
"""


def _text(value: Any) -> str:
    """客户端是否开启 ``decode_responses`` 由宿主决定，两种返回形态都要能处理。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _dump(value: Any) -> str:
    """序列化 JSON 字段。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def to_hash(meta: ThreadMetadata) -> dict[str, str]:
    """``ThreadMetadata`` → 哈希字段。浮点数用 ``repr`` 保证读回来逐位相等。"""
    return {
        "thread_id": meta.thread_id,
        "created_at": repr(meta.created_at),
        "updated_at": repr(meta.updated_at),
        "entry_skill_id": meta.entry_skill_id,
        "source": meta.source,
        "tags": _dump(list(meta.tags)),
        "extra": _dump(meta.extra),
    }


def _hash_mapping(meta: ThreadMetadata) -> Any:
    """``hset`` 的 ``mapping`` 参数。

    redis-py 把键类型标成了不变的联合类型，``dict[str, str]`` 无法直接通过类型检查，
    这里集中做一次类型擦除。
    """
    return to_hash(meta)


def from_hash(raw: Mapping[Any, Any]) -> ThreadMetadata:
    """哈希字段 → ``ThreadMetadata``。"""
    fields = {_text(key): _text(value) for key, value in raw.items()}
    return ThreadMetadata(
        thread_id=fields["thread_id"],
        created_at=float(fields["created_at"]),
        updated_at=float(fields["updated_at"]),
        entry_skill_id=fields["entry_skill_id"],
        source=fields["source"],
        tags=tuple(json.loads(fields["tags"])),
        extra=dict(json.loads(fields["extra"])),
    )


def patch_fields(patch: Mapping[str, Any]) -> dict[str, str]:
    """补丁 → 要改写的哈希字段。提到的字段整体替换；不认识的键忽略（与内核默认实现一致）。"""
    fields: dict[str, str] = {}
    if "entry_skill_id" in patch:
        fields["entry_skill_id"] = str(patch["entry_skill_id"])
    if "source" in patch:
        fields["source"] = str(patch["source"])
    if "tags" in patch:
        fields["tags"] = _dump(list(patch["tags"]))
    if "extra" in patch:
        fields["extra"] = _dump(dict(patch["extra"]))
    return fields


def matches(meta: ThreadMetadata, filter: ThreadFilter | None) -> bool:  # noqa: A002
    """过滤条件是 AND 关系；时间窗两端都是开区间。"""
    if filter is None:
        return True
    checks = (
        filter.entry_skill_id is None or meta.entry_skill_id == filter.entry_skill_id,
        filter.source is None or meta.source == filter.source,
        filter.created_after is None or meta.created_at > filter.created_after,
        filter.created_before is None or meta.created_at < filter.created_before,
        filter.tag is None or filter.tag in meta.tags,
    )
    return all(checks)


def _after_cursor(meta: ThreadMetadata, cursor: tuple[float, str] | None) -> bool:
    """该线程是否排在游标之后（尚未被上一页返回）。"""
    if cursor is None:
        return True
    updated_at, thread_id = cursor
    if meta.updated_at != updated_at:
        return meta.updated_at < updated_at
    return meta.thread_id < thread_id


class RedisThreadDirectory:
    """线程元数据目录，可多实例共享。"""

    def __init__(self, client: Redis, *, prefix: str = "taifeng") -> None:
        """
        Args:
            client: 宿主创建并管理的异步 Redis 客户端。
            prefix: 键前缀，用来隔离不同的数据集。
        """
        self._redis = client
        self._prefix = validate_key_prefix(prefix)

    def _thread_key(self, thread_id: str) -> str:
        """某个线程的哈希键。"""
        return f"{self._prefix}:thread:{thread_id}"

    @property
    def _index_key(self) -> str:
        """排序用的有序集合键。"""
        return f"{self._prefix}:threads"

    async def _load_many(self, thread_ids: list[str]) -> list[ThreadMetadata]:
        """批量读取元数据；索引里有、哈希却不在的（已被外部删除）跳过。"""
        if not thread_ids:
            return []
        pipeline = self._redis.pipeline(transaction=False)
        for thread_id in thread_ids:
            pipeline.hgetall(self._thread_key(thread_id))
        results = cast("list[Mapping[Any, Any]]", await pipeline.execute())
        return [from_hash(raw) for raw in results if raw]

    async def _scan(
        self,
        *,
        limit: int,
        cursor: tuple[float, str] | None,
        filter: ThreadFilter | None,  # noqa: A002
    ) -> list[ThreadMetadata]:
        """按时间倒序扫描，直到凑够 ``limit + 1`` 条或扫完。"""
        upper: float | str = cursor[0] if cursor is not None else "+inf"
        collected: list[ThreadMetadata] = []
        offset = 0
        while len(collected) <= limit:
            members = cast(
                "list[Any]",
                await self._redis.zrevrangebyscore(
                    self._index_key, upper, "-inf", start=offset, num=_SCAN_BATCH
                ),
            )
            if not members:
                break
            offset += len(members)
            for meta in await self._load_many([_text(member) for member in members]):
                if _after_cursor(meta, cursor) and matches(meta, filter):
                    collected.append(meta)
        # 哈希里的 updated_at 才是权威值；按它重排一次，消除扫描期间并发更新造成的错位
        collected.sort(key=lambda meta: (meta.updated_at, meta.thread_id), reverse=True)
        return collected[: limit + 1]

    async def list_threads(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        filter: ThreadFilter | None = None,  # noqa: A002 —— 与协议参数同名
    ) -> ThreadPage:
        """按 ``updated_at`` 倒序、过滤、游标分页列出线程。"""
        validate_limit(limit)
        try:
            found = await self._scan(limit=limit, cursor=decode_cursor(cursor), filter=filter)
        except (RedisError, OSError) as exc:
            raise DirectoryError(f"列出线程失败：{exc}") from exc
        items = found[:limit]
        next_cursor: str | None = None
        if len(found) > limit:
            last = items[-1]
            next_cursor = encode_cursor(last.updated_at, last.thread_id)
        return ThreadPage(items=items, next_cursor=next_cursor)

    async def get_metadata(self, thread_id: str) -> ThreadMetadata | None:
        """按 id 取元数据；不存在返回 None。"""
        try:
            raw = cast(
                "Mapping[Any, Any]", await self._redis.hgetall(self._thread_key(thread_id))
            )
        except (RedisError, OSError) as exc:
            raise DirectoryError(f"读取线程 {thread_id!r} 的元数据失败：{exc}") from exc
        return from_hash(raw) if raw else None

    async def upsert_metadata(self, meta: ThreadMetadata) -> None:
        """不存在则插入，存在则整体替换。哈希与索引在同一个事务里写。"""
        try:
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.delete(self._thread_key(meta.thread_id))
            pipeline.hset(self._thread_key(meta.thread_id), mapping=_hash_mapping(meta))
            pipeline.zadd(self._index_key, {meta.thread_id: meta.updated_at})
            await pipeline.execute()
        except (RedisError, OSError) as exc:
            raise DirectoryError(f"写入线程 {meta.thread_id!r} 的元数据失败：{exc}") from exc

    async def update_metadata(self, thread_id: str, patch: dict[str, Any]) -> None:
        """部分合并补丁并刷新 ``updated_at``；线程不存在抛 ``ThreadNotFoundError``。

        「判断存在 → 改字段 → 更新排序」由一段服务端脚本原子完成：并发补丁逐个生效，
        不会互相覆盖，也不需要客户端重试。
        """
        now = repr(time.time())
        arguments = [thread_id, now]
        for field, value in patch_fields(patch).items():
            arguments.extend((field, value))
        try:
            updated = await self._redis.eval(
                _UPDATE_SCRIPT, 2, self._thread_key(thread_id), self._index_key, *arguments
            )
        except (RedisError, OSError) as exc:
            raise DirectoryError(f"更新线程 {thread_id!r} 的元数据失败：{exc}") from exc
        if not updated:
            raise ThreadNotFoundError(thread_id)

    async def delete_thread(self, thread_id: str) -> bool:
        """删除线程的元数据与索引项；返回是否确实删掉了东西。"""
        try:
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.delete(self._thread_key(thread_id))
            pipeline.zrem(self._index_key, thread_id)
            deleted, _ = await pipeline.execute()
        except (RedisError, OSError) as exc:
            raise DirectoryError(f"删除线程 {thread_id!r} 失败：{exc}") from exc
        return bool(deleted)


__all__ = ["RedisThreadDirectory", "from_hash", "matches", "patch_fields", "to_hash"]
