"""``MemoryStore`` 的 pgvector 实现：长期记忆的向量检索。

对应内核的「换页」语义（taifeng ``context/memory.py``）：turn 结束把新增内容写进长期存储，
压缩丢弃前把将被换出的内容抢救进来，下一次 turn 开始前按查询取回相关片段。

本类只负责存与取。文本怎么变成向量由宿主注入的 ``Embedder`` 决定（模型、服务、缓存都是
宿主的事），本包不绑定任何嵌入服务。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from taifeng_stores.errors import StoreConfigError
from taifeng_stores.namespace import SqlNamespace
from taifeng_stores.postgres.migrate import ensure_schema_current

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg
    from taifeng import ResponseItem

# 默认只沉淀对话正文；工具调用、记账类消息不进长期记忆
DEFAULT_KINDS: frozenset[str] = frozenset({"user_message", "assistant_message"})


@runtime_checkable
class Embedder(Protocol):
    """把文本变成向量。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与 ``texts`` 等长、顺序对应的向量列表。"""
        ...


def vector_literal(vector: Sequence[float]) -> str:
    """向量 → pgvector 的文本字面量。"""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class PgvectorMemoryStore:
    """基于 pgvector 的长期记忆。

    ``scope`` 是记忆的共享边界：同一个 ``scope`` 下的所有线程共享记忆，不同 ``scope``
    互不可见。按什么划分（用户、团队、项目）由宿主决定。
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: Embedder,
        *,
        dimensions: int,
        scope: str = "default",
        namespace: SqlNamespace | None = None,
        top_k: int = 5,
        min_similarity: float = 0.0,
        max_chars: int = 4000,
        max_item_chars: int = 8000,
        include_current_thread: bool = False,
        kinds: frozenset[str] = DEFAULT_KINDS,
    ) -> None:
        """
        Args:
            pool: 宿主创建并管理的连接池。
            embedder: 嵌入实现。
            dimensions: 向量维度，必须与迁移时指定的一致。
            scope: 记忆共享边界。
            namespace: 表所在的 schema 与表名前缀；None 用默认值。
            top_k: 每次取回的最多条数。
            min_similarity: 余弦相似度下限，低于它的不取回。
            max_chars: 取回文本的总字符上限。
            max_item_chars: 单条记忆的字符上限，超出部分不存。
            include_current_thread: 是否取回当前线程自己写入的记忆。默认不取——那些内容
                通常还在上下文里，取回只会重复。
            kinds: 哪些类型的消息进长期记忆。
        """
        if dimensions < 1:
            raise StoreConfigError(f"dimensions 必须为正数，得到 {dimensions}")
        if top_k < 1 or max_chars < 1 or max_item_chars < 1:
            raise StoreConfigError("top_k / max_chars / max_item_chars 必须为正数")
        if not scope:
            raise StoreConfigError("scope 不能为空")
        self._pool = pool
        self._embedder = embedder
        self._dimensions = dimensions
        self._scope = scope
        self._namespace = namespace or SqlNamespace()
        self._table = self._namespace.table("memory")
        self._top_k = top_k
        self._min_similarity = min_similarity
        self._max_chars = max_chars
        self._max_item_chars = max_item_chars
        self._include_current_thread = include_current_thread
        self._kinds = kinds

    async def check_schema(self) -> None:
        """核对表结构版本；落后时抛 ``SchemaOutOfDateError``。"""
        await ensure_schema_current(self._pool, self._namespace, "memory")

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        """调用嵌入实现并核对返回形状。"""
        vectors = await self._embedder.embed(texts)
        if len(vectors) != len(texts):
            raise ValueError(f"嵌入实现返回了 {len(vectors)} 个向量，期望 {len(texts)} 个")
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise ValueError(f"嵌入维度为 {len(vector)}，期望 {self._dimensions}")
        return vectors

    def _memorable(self, items: Sequence[ResponseItem]) -> list[tuple[ResponseItem, str]]:
        """挑出要进长期记忆的消息及其正文。"""
        selected: list[tuple[ResponseItem, str]] = []
        for item in items:
            if item.kind not in self._kinds:
                continue
            text = item.payload.get("text")
            if isinstance(text, str) and text.strip():
                selected.append((item, text.strip()[: self._max_item_chars]))
        return selected

    async def _store(self, items: Sequence[ResponseItem]) -> int:
        """写入一批消息，返回实际写入的条数；已存在的跳过。"""
        selected = self._memorable(items)
        if not selected:
            return 0
        vectors = await self._embed([text for _, text in selected])
        written = 0
        async with self._pool.acquire() as conn, conn.transaction():
            for (item, text), vector in zip(selected, vectors, strict=True):
                status = await conn.execute(
                    f"INSERT INTO {self._table} (scope, thread_id, item_id, content, embedding) "
                    "VALUES ($1, $2, $3, $4, $5::text::vector) "
                    "ON CONFLICT (scope, thread_id, item_id) DO NOTHING",
                    self._scope,
                    item.thread_id,
                    item.id,
                    text,
                    vector_literal(vector),
                )
                written += int(status.rsplit(" ", 1)[-1])
        return written

    async def prefetch(self, query: str, *, thread_id: str) -> str:
        """按查询取回相关记忆；没有相关内容返回空串。"""
        if not query.strip():
            return ""
        (vector,) = await self._embed([query])
        literal = vector_literal(vector)
        current = None if self._include_current_thread else thread_id
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT content, 1 - (embedding <=> $1::text::vector) AS similarity "
                f"FROM {self._table} "
                "WHERE scope = $2 AND ($3::text IS NULL OR thread_id <> $3) "
                "ORDER BY embedding <=> $1::text::vector LIMIT $4",
                literal,
                self._scope,
                current,
                self._top_k,
            )
        parts: list[str] = []
        used = 0
        for row in rows:
            if float(row["similarity"]) < self._min_similarity:
                continue
            content = str(row["content"])
            if used + len(content) > self._max_chars:
                break
            parts.append(content)
            used += len(content)
        return "\n\n".join(parts)

    async def writeback(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """turn 结束后把新增内容写入长期记忆。"""
        await self._store(items)

    async def on_pre_evict(self, items: Sequence[ResponseItem]) -> str:
        """压缩丢弃前把内容抢救进长期记忆。

        返回空串：被换出的内容已经可以按需取回，不需要再往压缩摘要里塞文本。
        """
        await self._store(items)
        return ""

    async def on_session_end(self, *, thread_id: str, items: Sequence[ResponseItem]) -> None:
        """会话结束时的最终写入；已写过的会被去重。"""
        await self._store(items)

    async def forget_thread(self, thread_id: str) -> int:
        """删除某个线程写入的全部记忆，返回删除条数（用于数据删除请求）。"""
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                f"DELETE FROM {self._table} WHERE scope = $1 AND thread_id = $2",
                self._scope,
                thread_id,
            )
        return int(status.rsplit(" ", 1)[-1])


__all__ = ["DEFAULT_KINDS", "Embedder", "PgvectorMemoryStore", "vector_literal"]
