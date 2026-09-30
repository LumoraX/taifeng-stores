"""``PgvectorMemoryStore``：长期记忆的写入与检索。

嵌入实现用一个确定性的词袋哈希——它不是被测对象，只是给向量检索提供可预期的输入；
存储与检索都打在真实的 pgvector 上。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import TYPE_CHECKING

import pytest
import taifeng
from taifeng import MemoryStore, assistant_message, function_call, user_message

from taifeng_stores import SchemaOutOfDateError, StoreConfigError
from taifeng_stores.postgres import PgvectorMemoryStore, apply_migrations
from taifeng_stores.postgres.memory_store import vector_literal
from tests.conftest import EMBEDDING_DIMENSIONS
from tests.support import ScriptedModelClient, run_turn, write_skills

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import asyncpg

    from taifeng_stores import SqlNamespace


class BagOfWordsEmbedder:
    """按词哈希到固定维度再归一化：共享的词越多，余弦相似度越高。"""

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        """``dimensions`` 为输出维度。"""
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """逐条计算向量。"""
        self.calls.append(list(texts))
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for word in text.lower().split():
            digest = hashlib.sha256(word.encode()).digest()
            vector[digest[0] % self._dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@pytest.fixture
def embedder() -> BagOfWordsEmbedder:
    """确定性的嵌入实现。"""
    return BagOfWordsEmbedder()


def make_store(
    pool: asyncpg.Pool,
    namespace: SqlNamespace,
    embedder: BagOfWordsEmbedder,
    **overrides: object,
) -> PgvectorMemoryStore:
    """构造记忆存储。"""
    options: dict[str, object] = {"dimensions": EMBEDDING_DIMENSIONS}
    options.update(overrides)
    return PgvectorMemoryStore(pool, embedder, namespace=namespace, **options)  # type: ignore[arg-type]


async def _count(pool: asyncpg.Pool, namespace: SqlNamespace) -> int:
    """记忆表里的行数。"""
    async with pool.acquire() as conn:
        return int(await conn.fetchval(f"SELECT count(*) FROM {namespace.table('memory')}"))


def test_implements_protocol(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """满足 ``MemoryStore`` 协议。"""
    assert isinstance(make_store(pg_pool, namespace, embedder), MemoryStore)


def test_vector_literal() -> None:
    """向量字面量的格式。"""
    assert vector_literal([1, 0.5, -2.25]) == "[1.0,0.5,-2.25]"


@pytest.mark.parametrize(
    "overrides",
    [{"dimensions": 0}, {"top_k": 0}, {"max_chars": 0}, {"max_item_chars": 0}, {"scope": ""}],
)
def test_invalid_options_rejected(
    pg_pool: asyncpg.Pool,
    namespace: SqlNamespace,
    embedder: BagOfWordsEmbedder,
    overrides: dict[str, object],
) -> None:
    """不合法的构造参数直接拒绝。"""
    with pytest.raises(StoreConfigError):
        make_store(pg_pool, namespace, embedder, **overrides)


async def test_check_schema_without_memory_component(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """只迁移了 core、没迁移 memory 时核对会明确报错。"""
    await apply_migrations(pg_pool, empty_namespace)
    with pytest.raises(SchemaOutOfDateError) as caught:
        await make_store(pg_pool, empty_namespace, embedder).check_schema()
    assert caught.value.component == "memory"


async def test_writeback_then_prefetch_from_another_thread(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """一个线程写入的内容，另一个线程按相关性取回；最相关的排最前。"""
    store = make_store(pg_pool, namespace, embedder, top_k=2)
    await store.writeback(
        thread_id="thr_a",
        items=[
            user_message("invoice approval needs finance manager signature", thread_id="thr_a"),
            assistant_message("weather today is sunny and warm", thread_id="thr_a", model="m"),
            user_message("deploy window is friday night", thread_id="thr_a"),
        ],
    )
    found = await store.prefetch("who signs invoice approval", thread_id="thr_b")
    parts = found.split("\n\n")
    assert parts[0] == "invoice approval needs finance manager signature"
    assert len(parts) <= 2


async def test_only_conversation_text_is_memorized(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """工具调用、空白正文不进长期记忆。"""
    store = make_store(pg_pool, namespace, embedder)
    await store.writeback(
        thread_id="thr_a",
        items=[
            function_call("c1", "lookup", "{}", thread_id="thr_a"),
            user_message("   ", thread_id="thr_a"),
            user_message("keep this one", thread_id="thr_a"),
        ],
    )
    assert await _count(pg_pool, namespace) == 1
    assert embedder.calls == [["keep this one"]]


async def test_nothing_to_store_skips_embedding(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """没有可沉淀的内容时不调用嵌入实现（那通常是计费的外部调用）。"""
    store = make_store(pg_pool, namespace, embedder)
    await store.writeback(thread_id="thr_a", items=[])
    await store.writeback(
        thread_id="thr_a", items=[function_call("c1", "lookup", "{}", thread_id="thr_a")]
    )
    assert await store.prefetch("   ", thread_id="thr_b") == ""
    assert embedder.calls == []


async def test_same_item_is_stored_once(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """同一条消息经 writeback、换出抢救、会话结束多次到达，只存一份。"""
    store = make_store(pg_pool, namespace, embedder)
    item = user_message("remember me once", thread_id="thr_a")
    await store.writeback(thread_id="thr_a", items=[item])
    assert await store.on_pre_evict([item]) == ""
    await store.on_session_end(thread_id="thr_a", items=[item])
    assert await _count(pg_pool, namespace) == 1


async def test_current_thread_excluded_by_default(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """默认不取回当前线程自己写入的内容；显式开启后才取回。"""
    item = user_message("alpha beta gamma", thread_id="thr_a")
    store = make_store(pg_pool, namespace, embedder)
    await store.writeback(thread_id="thr_a", items=[item])
    assert await store.prefetch("alpha beta gamma", thread_id="thr_a") == ""
    inclusive = make_store(pg_pool, namespace, embedder, include_current_thread=True)
    assert await inclusive.prefetch("alpha beta gamma", thread_id="thr_a") == "alpha beta gamma"


async def test_scopes_are_isolated(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """不同 scope 的记忆互不可见。"""
    team_a = make_store(pg_pool, namespace, embedder, scope="team-a")
    team_b = make_store(pg_pool, namespace, embedder, scope="team-b")
    await team_a.writeback(
        thread_id="thr_a", items=[user_message("secret plan alpha", thread_id="thr_a")]
    )
    assert await team_b.prefetch("secret plan alpha", thread_id="thr_x") == ""
    assert await team_a.prefetch("secret plan alpha", thread_id="thr_x") == "secret plan alpha"


async def test_similarity_floor_and_character_budget(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """相似度下限过滤掉不相关的；总字符预算截住过长的取回。"""
    items = [
        user_message("alpha beta gamma delta", thread_id="thr_a"),
        user_message("alpha beta gamma", thread_id="thr_a"),
        user_message("zzz yyy xxx www", thread_id="thr_a"),
    ]
    await make_store(pg_pool, namespace, embedder).writeback(thread_id="thr_a", items=items)

    strict = make_store(pg_pool, namespace, embedder, min_similarity=0.99)
    assert await strict.prefetch("alpha beta gamma", thread_id="thr_b") == "alpha beta gamma"

    tight = make_store(pg_pool, namespace, embedder, max_chars=20)
    assert await tight.prefetch("alpha beta gamma", thread_id="thr_b") == "alpha beta gamma"


async def test_long_item_is_truncated(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """单条内容超过上限时只存前面一段。"""
    store = make_store(pg_pool, namespace, embedder, max_item_chars=10, max_chars=100)
    await store.writeback(
        thread_id="thr_a", items=[user_message("abcdefghij-overflow", thread_id="thr_a")]
    )
    assert await store.prefetch("abcdefghij", thread_id="thr_b") == "abcdefghij"


async def test_dimension_mismatch_is_an_error(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace
) -> None:
    """嵌入实现返回的维度不对：报错并且什么都不写。"""
    store = make_store(pg_pool, namespace, BagOfWordsEmbedder(dimensions=3))
    with pytest.raises(ValueError, match="维度"):
        await store.writeback(
            thread_id="thr_a", items=[user_message("hello", thread_id="thr_a")]
        )
    assert await _count(pg_pool, namespace) == 0


async def test_forget_thread(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder
) -> None:
    """按线程删除记忆，只影响本 scope 的该线程。"""
    store = make_store(pg_pool, namespace, embedder)
    await store.writeback(
        thread_id="thr_a",
        items=[user_message("one", thread_id="thr_a"), user_message("two", thread_id="thr_a")],
    )
    await store.writeback(
        thread_id="thr_b", items=[user_message("three", thread_id="thr_b")]
    )
    assert await store.forget_thread("thr_a") == 2
    assert await store.forget_thread("thr_a") == 0
    assert await _count(pg_pool, namespace) == 1


async def test_engine_pages_memory_in_and_out(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, embedder: BagOfWordsEmbedder, tmp_path: Path
) -> None:
    """挂在内核上：第一个会话里模型给出的结论，第二个会话提问时被取回并进入提示。

    内核在 turn 结束时写回的是本轮产出（模型的回复），用户原话要到会话结束或被压缩换出时
    才交给记忆存储——所以这里用模型回复来验证跨会话取回，用会话结束后的行数验证用户原话。
    """
    store = make_store(pg_pool, namespace, embedder)
    fact = "invoice approval needs finance manager signature"
    client = ScriptedModelClient([fact, "the finance manager"])
    pool = await taifeng.EnginePool.create(
        skills_dir=write_skills(tmp_path),
        storage_dir=tmp_path / "threads",
        model_client=client,
        compressors=[],
        memory_store=store,
    )
    try:
        first = await pool.get_or_create(session_id="s1", entry_skill_id="helper")
        await run_turn(first, "what is the rule for invoices")
        # 写回是 turn 结束后的后台动作，等它落库再开第二个会话
        for _ in range(100):
            if await _count(pg_pool, namespace) >= 1:
                break
            await asyncio.sleep(0.05)
        second = await pool.get_or_create(session_id="s2", entry_skill_id="helper")
        events = await run_turn(second, "who signs invoice approval")
        assert events[-1].msg.kind == "turn_completed"
    finally:
        await pool.close()

    injected = [
        message["content"]
        for message in client.requests[-1].model_dump()["messages"]
        if message["role"] == "system"
    ]
    assert injected == [f"<retrieved_memory>\n{fact}\n</retrieved_memory>"]
    # 会话结束时两个会话的用户原话也进了长期记忆：2 条回复 + 2 条提问
    assert await _count(pg_pool, namespace) == 4
