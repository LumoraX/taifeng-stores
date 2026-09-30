"""测试公共夹具：真实的 Postgres / Redis，每个用例独占一个命名空间。"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import asyncpg
import pytest
import redis.asyncio as redis_asyncio

from taifeng_stores import SqlNamespace
from taifeng_stores.postgres import apply_migrations
from tests.backends import Backend, start_postgres, start_redis

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from redis.asyncio import Redis

EMBEDDING_DIMENSIONS = 8


@pytest.fixture(scope="session")
def postgres_backend() -> Iterator[Backend]:
    """整个测试会话共用的 Postgres 服务。"""
    backend = start_postgres()
    if backend is None:
        pytest.skip("没有可用的 Postgres：未设置 TAIFENG_STORES_TEST_PG_DSN，也没有 Docker")
    try:
        yield backend
    finally:
        backend.stop()


@pytest.fixture(scope="session")
def redis_backend() -> Iterator[Backend]:
    """整个测试会话共用的 Redis 服务。"""
    backend = start_redis()
    if backend is None:
        pytest.skip("没有可用的 Redis：未设置 TAIFENG_STORES_TEST_REDIS_URL，也没有 Docker")
    try:
        yield backend
    finally:
        backend.stop()


@asynccontextmanager
async def open_pool(url: str) -> AsyncIterator[asyncpg.Pool]:
    """连接池。每个用例新建，与用例的事件循环同生命周期。"""
    pool = await asyncpg.create_pool(url, min_size=1, max_size=8)
    try:
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def scratch_namespace(pool: asyncpg.Pool) -> AsyncIterator[SqlNamespace]:
    """尚未迁移的独占 schema；退出时整个删掉。"""
    namespace = SqlNamespace(schema="t_" + secrets.token_hex(6), prefix="tf_")
    try:
        yield namespace
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace.schema}" CASCADE')


async def migrate_all(pool: asyncpg.Pool, namespace: SqlNamespace) -> None:
    """把命名空间迁移到最新（含向量记忆表）。"""
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await apply_migrations(
        pool,
        namespace,
        components=("core", "memory"),
        embedding_dimensions=EMBEDDING_DIMENSIONS,
    )


@asynccontextmanager
async def open_redis(url: str) -> AsyncIterator[tuple[Redis, str]]:
    """Redis 客户端与独占的键前缀；退出时删掉该前缀下的所有键。"""
    client: Redis = redis_asyncio.from_url(url)
    prefix = "t-" + secrets.token_hex(6)
    try:
        yield client, prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.fixture
async def pg_pool(postgres_backend: Backend) -> AsyncIterator[asyncpg.Pool]:
    """连接池。"""
    async with open_pool(postgres_backend.url) as pool:
        yield pool


@pytest.fixture
async def empty_namespace(pg_pool: asyncpg.Pool) -> AsyncIterator[SqlNamespace]:
    """尚未迁移的独占 schema。"""
    async with scratch_namespace(pg_pool) as namespace:
        yield namespace


@pytest.fixture
async def namespace(pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace) -> SqlNamespace:
    """已迁移到最新的独占 schema（含向量记忆表）。"""
    await migrate_all(pg_pool, empty_namespace)
    return empty_namespace


@pytest.fixture
async def redis_scope(redis_backend: Backend) -> AsyncIterator[tuple[Redis, str]]:
    """Redis 客户端与独占的键前缀。"""
    async with open_redis(redis_backend.url) as scope:
        yield scope
