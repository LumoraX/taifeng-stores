"""``PostgresMessageStore``：协议行为，以及交给内核引擎、引擎池真跑对话。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import taifeng
from taifeng import MessageStore, ResponseItem, assistant_message, function_call, user_message

from taifeng_stores import SchemaOutOfDateError
from taifeng_stores.postgres import PostgresMessageStore
from taifeng_stores.postgres.messages import PositionClock
from tests.support import ScriptedModelClient, run_turn, write_skills

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg

    from taifeng_stores import SqlNamespace


@pytest.fixture
def store(pg_pool: asyncpg.Pool, namespace: SqlNamespace) -> PostgresMessageStore:
    """已迁移好的消息存储。"""
    return PostgresMessageStore(pg_pool, namespace=namespace)


async def _load(store: PostgresMessageStore, thread_id: str) -> list[ResponseItem]:
    """回放线程的全部消息。"""
    return [item async for item in await store.load_thread(thread_id)]


def test_implements_protocol(store: PostgresMessageStore) -> None:
    """满足 ``MessageStore`` 协议。"""
    assert isinstance(store, MessageStore)


async def test_check_schema_on_unmigrated_database(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """表结构没迁移时核对会明确报错。"""
    with pytest.raises(SchemaOutOfDateError):
        await PostgresMessageStore(pg_pool, namespace=empty_namespace).check_schema()


async def test_create_thread_registers_metadata(store: PostgresMessageStore) -> None:
    """建线程的同时登记元数据：cwd 与调用方的 extra 合并，缺省值与内核一致。"""
    thread_id = await store.create_thread(
        cwd="/work", entry_skill_id="helper", extra={"parent_thread_id": "thr_x"}
    )
    assert thread_id.startswith("thr_")
    meta = await store.directory.get_metadata(thread_id)
    assert meta is not None
    assert meta.entry_skill_id == "helper"
    assert meta.source == "user"
    assert meta.extra == {"cwd": "/work", "parent_thread_id": "thr_x"}

    bare = await store.directory.get_metadata(await store.create_thread())
    assert bare is not None
    assert (bare.entry_skill_id, bare.source, bare.extra) == ("general", "user", {})


async def test_thread_ids_are_unique(store: PostgresMessageStore) -> None:
    """并发建线程得到互不相同的 id。"""
    ids = await asyncio.gather(*(store.create_thread() for _ in range(40)))
    assert len(set(ids)) == 40


async def test_append_and_replay_preserve_order_and_content(store: PostgresMessageStore) -> None:
    """按写入顺序回放，各字段原样保留（含非 ASCII、嵌套结构、时间戳）。"""
    thread_id = await store.create_thread(entry_skill_id="helper")
    stamp = datetime(2026, 9, 30, 1, 2, 3, 456789, tzinfo=UTC)
    items = [
        user_message("你好，'引号' 与 \"双引号\"", thread_id=thread_id),
        function_call("c1", "lookup", '{"q": "x"}', thread_id=thread_id),
        ResponseItem(
            kind="function_call_output",
            thread_id=thread_id,
            payload={"call_id": "c1", "output": "结果", "is_error": False, "nested": {"a": [1, None]}},
            created_at=stamp,
            metadata={"trace": "t-1"},
        ),
        assistant_message("完成", thread_id=thread_id, model="m"),
    ]
    await store.append(items[0])
    await store.append_batch(items[1:])
    assert await _load(store, thread_id) == items


async def test_large_batch_keeps_order(store: PostgresMessageStore) -> None:
    """大批量写入顺序不乱。"""
    thread_id = await store.create_thread()
    items = [user_message(f"m{index}", thread_id=thread_id) for index in range(300)]
    await store.append_batch(items)
    assert [item.payload["text"] for item in await _load(store, thread_id)] == [
        f"m{index}" for index in range(300)
    ]


async def test_empty_batch_is_noop(store: PostgresMessageStore) -> None:
    """空批次不做任何事。"""
    thread_id = await store.create_thread()
    await store.append_batch([])
    assert await _load(store, thread_id) == []


async def test_unknown_thread_replays_empty(store: PostgresMessageStore) -> None:
    """不存在的线程回放为空，不抛异常（与内核默认实现一致）。"""
    assert await _load(store, "thr_missing") == []


async def test_threads_do_not_leak_into_each_other(store: PostgresMessageStore) -> None:
    """一个批次里混着多个线程的消息，回放时各归各。"""
    first = await store.create_thread()
    second = await store.create_thread()
    await store.append_batch(
        [
            user_message("a1", thread_id=first),
            user_message("b1", thread_id=second),
            user_message("a2", thread_id=first),
        ]
    )
    assert [item.payload["text"] for item in await _load(store, first)] == ["a1", "a2"]
    assert [item.payload["text"] for item in await _load(store, second)] == ["b1"]


async def test_duplicate_item_is_ignored(store: PostgresMessageStore) -> None:
    """同一条消息重复写入只保留一条。"""
    thread_id = await store.create_thread()
    item = user_message("once", thread_id=thread_id)
    await store.append(item)
    await store.append(item)
    assert await _load(store, thread_id) == [item]


async def test_batch_is_atomic(store: PostgresMessageStore, pg_pool: asyncpg.Pool) -> None:
    """批次中途失败时整批不可见。"""
    thread_id = await store.create_thread()
    good = user_message("good", thread_id=thread_id)
    bad = ResponseItem(kind="user_message", thread_id=thread_id, payload={"text": "bad \x00 nul"})
    with pytest.raises(Exception, match="(?i)unicode|0x00|null"):
        await store.append_batch([good, bad])
    assert await _load(store, thread_id) == []


async def test_concurrent_appends_all_land(store: PostgresMessageStore) -> None:
    """并发追加不丢消息。"""
    thread_id = await store.create_thread()
    await asyncio.gather(
        *(store.append(user_message(f"m{index}", thread_id=thread_id)) for index in range(50))
    )
    texts = {item.payload["text"] for item in await _load(store, thread_id)}
    assert texts == {f"m{index}" for index in range(50)}


async def test_takeover_with_a_slower_clock_keeps_order(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace
) -> None:
    """接管方的时钟比原持有方慢：回放后再写的消息仍排在已有消息之后。"""
    ahead = PostgresMessageStore(pg_pool, namespace=namespace)
    thread_id = await ahead.create_thread(entry_skill_id="helper")
    await ahead.append(user_message("接管之前", thread_id=thread_id))

    # 接管方的时钟停在 1970 年：不做处理的话，它写的消息会排到最前面
    behind = PostgresMessageStore(pg_pool, namespace=namespace, now_ns=lambda: 1)
    replayed = [item async for item in await behind.load_thread(thread_id)]
    assert [item.payload["text"] for item in replayed] == ["接管之前"]
    await behind.append(user_message("接管之后", thread_id=thread_id))

    texts = [item.payload["text"] for item in await _load(behind, thread_id)]
    assert texts == ["接管之前", "接管之后"]


async def test_takeover_with_a_slower_clock_keeps_call_order(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace
) -> None:
    """时钟停住的接管方连续追加两次：都排在已有消息之后，彼此按调用顺序。"""
    ahead = PostgresMessageStore(pg_pool, namespace=namespace)
    thread_id = await ahead.create_thread()
    await ahead.append(user_message("接管之前", thread_id=thread_id))

    behind = PostgresMessageStore(pg_pool, namespace=namespace, now_ns=lambda: 1)
    assert [item.payload["text"] for item in await _load(behind, thread_id)] == ["接管之前"]
    await behind.append(user_message("第一条", thread_id=thread_id))
    await behind.append_batch([user_message("第二条", thread_id=thread_id)])

    texts = [item.payload["text"] for item in await _load(ahead, thread_id)]
    assert texts == ["接管之前", "第一条", "第二条"]


def test_position_clock_continues_after_observed_position() -> None:
    """观察到已有位置后，取的位置严格大于它，并且继续严格递增。"""
    clock = PositionClock(lambda: 1)
    clock.observe(1000)
    assert clock.next() == 1001
    assert clock.next() == 1002


def test_position_clock_smaller_observation_keeps_floor() -> None:
    """更小的观察值不会把下限往回拉：既不低于观察过的，也不低于发出过的。"""
    clock = PositionClock(lambda: 1)
    clock.observe(1000)
    clock.observe(5)
    assert clock.next() == 1001
    clock.observe(10)
    assert clock.next() == 1002


def test_position_clock_without_observation_follows_wall_clock() -> None:
    """从不观察（镜像的用法）：不小于墙钟，且严格递增。"""
    ticks = iter([100, 50, 300])
    clock = PositionClock(lambda: next(ticks))
    assert [clock.next(), clock.next(), clock.next()] == [100, 101, 300]


async def test_append_refreshes_activity_order(store: PostgresMessageStore) -> None:
    """有新消息的线程排到列表最前。"""
    old = await store.create_thread(cwd="/w")
    await asyncio.sleep(0.01)
    new = await store.create_thread(cwd="/w")
    assert [info.thread_id for info in await store.list_threads()] == [new, old]
    await asyncio.sleep(0.01)
    await store.append(user_message("bump", thread_id=old))
    threads = await store.list_threads()
    assert [info.thread_id for info in threads] == [old, new]
    assert threads[0].last_activity_at > threads[0].created_at


async def test_list_and_resume_filter_by_cwd(store: PostgresMessageStore) -> None:
    """按 cwd 过滤；续接选该 cwd 下最近活跃的线程。"""
    in_a = await store.create_thread(cwd="/a", entry_skill_id="helper", source="system")
    await asyncio.sleep(0.01)
    in_b = await store.create_thread(cwd="/b")
    await asyncio.sleep(0.01)
    in_a_later = await store.create_thread(cwd="/a")

    threads = await store.list_threads(cwd="/a")
    assert [info.thread_id for info in threads] == [in_a_later, in_a]
    assert threads[1].cwd == "/a"
    assert (threads[1].entry_skill_id, threads[1].source) == ("helper", "system")
    assert [info.thread_id for info in await store.list_threads(cwd="/b")] == [in_b]
    assert await store.list_threads(cwd="/a", limit=1) == threads[:1]

    assert await store.select_resume_path("/a") == in_a_later
    assert await store.select_resume_path("/nowhere") is None


async def test_list_rejects_bad_limit(store: PostgresMessageStore) -> None:
    """limit 必须为正数。"""
    with pytest.raises(ValueError, match="limit"):
        await store.list_threads(limit=0)


async def test_close_leaves_injected_pool_usable(
    store: PostgresMessageStore, pg_pool: asyncpg.Pool
) -> None:
    """连接池归宿主管：关闭存储之后连接池仍可用。"""
    await store.close()
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1") == 1


async def test_namespaces_isolate_data(pg_pool: asyncpg.Pool, namespace: SqlNamespace) -> None:
    """不同命名空间的数据互不可见。"""
    from taifeng_stores import SqlNamespace as Namespace
    from taifeng_stores.postgres import apply_migrations

    other = Namespace(schema=namespace.schema, prefix="other_")
    await apply_migrations(pg_pool, other)
    first = PostgresMessageStore(pg_pool, namespace=namespace)
    second = PostgresMessageStore(pg_pool, namespace=other)
    thread_id = await first.create_thread()
    await first.append(user_message("only-in-first", thread_id=thread_id))
    assert await second.list_threads() == []
    assert await _load(second, thread_id) == []


async def test_engine_runs_a_turn_on_postgres(
    store: PostgresMessageStore, tmp_path: Path
) -> None:
    """交给内核引擎真跑两轮对话：消息落库；换一个引擎从库里续接，上一轮的内容进了提示。"""
    registry = await taifeng.FilesystemSkillRegistry.load(write_skills(tmp_path))
    entry = registry.get("helper")
    assert entry is not None
    thread_id = await store.create_thread(entry_skill_id=entry.id)
    client = ScriptedModelClient(["第一轮的回答", "第二轮的回答"])

    async def converse(text: str, history: list[ResponseItem] | None) -> list[taifeng.EventMsg]:
        engine = taifeng.AgentEngine(
            entry_skill=entry,
            skill_snapshot=registry.snapshot(),
            tool_runtime=taifeng.ToolCallRuntime(taifeng.ToolRegistry()),
            model_client=client,
            store=store,
            thread_id=thread_id,
            initial_history=history,
        )
        cancel = taifeng.CancellationToken()
        runner = asyncio.ensure_future(engine.run(cancel))
        try:
            return await run_turn(engine, text)
        finally:
            await engine.shutdown()
            await asyncio.wait_for(runner, timeout=10)

    events = await converse("第一个问题", None)
    assert events[-1].msg.kind == "turn_completed"
    persisted = await _load(store, thread_id)
    assert [(item.kind, item.payload.get("text")) for item in persisted] == [
        ("user_message", "第一个问题"),
        ("assistant_message", "第一轮的回答"),
    ]

    events = await converse("第二个问题", persisted)
    assert events[-1].msg.kind == "turn_completed"
    assert [item.payload.get("text") for item in await _load(store, thread_id)] == [
        "第一个问题",
        "第一轮的回答",
        "第二个问题",
        "第二轮的回答",
    ]
    second_prompt = str(client.requests[-1].model_dump())
    assert "第一轮的回答" in second_prompt


def _sim_client(turns: list[taifeng.SimTurn], *, protocol: str) -> taifeng.SimClient:
    """内核稳定层的模拟模型，显式声明走哪种协议（只有文本输入）。"""
    return taifeng.SimClient(
        turns=turns,
        capabilities=taifeng.ModelCapabilities(
            input_modalities=frozenset({"text"}), provider="sim", protocol=protocol
        ),
    )


async def test_engine_pool_uses_it_as_the_main_store(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, tmp_path: Path
) -> None:
    """EnginePool 以它为主存：第一个池跑一轮后关闭；第二个池（另一个存储实例）续接同一线程。"""
    skills = write_skills(tmp_path)

    async def pool_with(
        store: PostgresMessageStore, client: taifeng.SimClient
    ) -> taifeng.EnginePool:
        """以 ``store`` 为主存、它的目录为线程目录建池。"""
        return await taifeng.EnginePool.create(
            skills_dir=skills,
            model_client=client,
            message_store=store,
            thread_directory=store.directory,
            compressors=[],
        )

    first = await pool_with(
        PostgresMessageStore(pg_pool, namespace=namespace),
        _sim_client([taifeng.SimTurn(text="第一轮")], protocol="chat"),
    )
    engine = await first.get_or_create(session_id="s1", entry_skill_id="helper")
    thread_id = engine.thread_id
    assert (await run_turn(engine, "你好"))[-1].msg.kind == "turn_completed"
    await first.close()

    # 第二个池的模型请求里必须带着上一轮的问答：续接的历史真的进了提示
    expect = taifeng.SimExpect(must_contain=("你好", "第一轮"))
    second_client = _sim_client([taifeng.SimTurn(text="第二轮", expect=expect)], protocol="chat")
    second_store = PostgresMessageStore(pg_pool, namespace=namespace)
    second = await pool_with(second_store, second_client)
    engine = await second.get_or_create(
        session_id="s1", entry_skill_id="helper", resume_thread_id=thread_id
    )
    assert engine.thread_id == thread_id
    assert (await run_turn(engine, "还在吗"))[-1].msg.kind == "turn_completed"
    await second.close()
    assert second_client.ledger.violations == []

    texts = [item.payload.get("text") for item in await _load(second_store, thread_id)]
    assert texts == ["你好", "第一轮", "还在吗", "第二轮"]
    meta = await second_store.directory.get_metadata(thread_id)
    assert meta is not None
    assert (meta.entry_skill_id, meta.source) == ("helper", "session:s1")


async def test_engine_pool_rejects_it_with_a_responses_client(
    pg_pool: asyncpg.Pool, namespace: SqlNamespace, tmp_path: Path
) -> None:
    """只配 Chat：模型客户端走 Responses 协议时要原子批量写，本实现没有，建池即报错。"""
    store = PostgresMessageStore(pg_pool, namespace=namespace)
    with pytest.raises(taifeng.LLMError, match="AtomicBatchMessageStore"):
        await taifeng.EnginePool.create(
            skills_dir=write_skills(tmp_path),
            model_client=_sim_client([taifeng.SimTurn(text="不会被采样")], protocol="responses"),
            message_store=store,
            thread_directory=store.directory,
            compressors=[],
        )
