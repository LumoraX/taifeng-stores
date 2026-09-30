"""表结构迁移。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from taifeng_stores import MigrationError, SchemaOutOfDateError, StoreConfigError
from taifeng_stores.postgres import (
    PostgresThreadDirectory,
    apply_migrations,
    current_version,
    ensure_schema_current,
    latest_version,
)
from taifeng_stores.postgres import migrate as migrate_module
from taifeng_stores.postgres.cli import main as cli_main

if TYPE_CHECKING:
    import asyncpg

    from taifeng_stores import SqlNamespace
    from tests.backends import Backend


async def _tables(pool: asyncpg.Pool, namespace: SqlNamespace) -> set[str]:
    """命名空间里现有的表。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = $1",
            namespace.schema,
        )
    return {str(row["table_name"]) for row in rows}


def test_bundled_migrations_are_well_formed() -> None:
    """随包分发的迁移：版本号从 1 起连续，脚本非空。"""
    for component in migrate_module.COMPONENTS:
        migrations = migrate_module.load_migrations(component)
        assert [item.version for item in migrations] == list(range(1, len(migrations) + 1))
        assert all(item.sql_template.strip() for item in migrations)
        assert latest_version(component) == len(migrations)


def test_unknown_component_rejected() -> None:
    """未知组件直接拒绝。"""
    with pytest.raises(StoreConfigError):
        migrate_module.load_migrations("nope")


async def test_fresh_database_reports_version_zero(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """从未迁移过：版本为 0，核对时报落后。"""
    assert await current_version(pg_pool, empty_namespace, "core") == 0
    with pytest.raises(SchemaOutOfDateError) as caught:
        await ensure_schema_current(pg_pool, empty_namespace, "core")
    assert (caught.value.current, caught.value.required) == (0, latest_version("core"))


async def test_stores_do_not_create_tables_implicitly(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """存储实现不会在运行时隐式建表：构造与核对之后库里仍然没有任何表。"""
    directory = PostgresThreadDirectory(pg_pool, namespace=empty_namespace)
    with pytest.raises(SchemaOutOfDateError):
        await directory.check_schema()
    assert await _tables(pg_pool, empty_namespace) == set()


async def test_apply_creates_tables_and_is_idempotent(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """首次执行建表并记账；再次执行什么都不做。"""
    executed = await apply_migrations(pg_pool, empty_namespace)
    assert [(item.component, item.version) for item in executed] == [("core", 1)]
    assert await _tables(pg_pool, empty_namespace) == {
        "tf_schema_migrations",
        "tf_thread",
        "tf_message",
    }
    assert await apply_migrations(pg_pool, empty_namespace) == []
    await ensure_schema_current(pg_pool, empty_namespace, "core")


async def test_components_are_versioned_independently(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """只迁移 core 时 memory 仍是 0；之后可以单独补上。"""
    await apply_migrations(pg_pool, empty_namespace)
    assert await current_version(pg_pool, empty_namespace, "memory") == 0
    async with pg_pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await apply_migrations(
        pg_pool, empty_namespace, components=("memory",), embedding_dimensions=4
    )
    assert await current_version(pg_pool, empty_namespace, "memory") == latest_version("memory")
    assert "tf_memory" in await _tables(pg_pool, empty_namespace)


async def test_memory_requires_dimensions(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """迁移 memory 必须给向量维度，且维度要在合理范围内；失败时不留下半截结构。"""
    with pytest.raises(StoreConfigError):
        await apply_migrations(pg_pool, empty_namespace, components=("core", "memory"))
    with pytest.raises(StoreConfigError):
        await apply_migrations(
            pg_pool, empty_namespace, components=("memory",), embedding_dimensions=0
        )
    assert await _tables(pg_pool, empty_namespace) == set()


async def test_failed_migration_rolls_back_everything(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """某一步失败时整个事务回滚，包括同一次执行里已经成功的前几步。"""
    real = migrate_module.load_migrations

    def with_broken_tail(component: str) -> list[migrate_module.Migration]:
        migrations = real(component)
        broken = migrate_module.Migration(
            component=component,
            version=len(migrations) + 1,
            name="broken",
            sql_template='CREATE TABLE "${schema}"."${prefix}x" (id INT); SELECT nope();',
        )
        return [*migrations, broken]

    monkeypatch.setattr(migrate_module, "load_migrations", with_broken_tail)
    with pytest.raises(MigrationError, match="broken"):
        await apply_migrations(pg_pool, empty_namespace)
    assert await _tables(pg_pool, empty_namespace) == set()


async def test_modified_applied_migration_detected(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已执行过的迁移脚本事后被改动：拒绝继续，提示新增迁移。"""
    await apply_migrations(pg_pool, empty_namespace)
    real = migrate_module.load_migrations

    def tampered(component: str) -> list[migrate_module.Migration]:
        return [
            migrate_module.Migration(
                component=item.component,
                version=item.version,
                name=item.name,
                sql_template=item.sql_template + "\n-- 事后改动",
            )
            for item in real(component)
        ]

    monkeypatch.setattr(migrate_module, "load_migrations", tampered)
    with pytest.raises(MigrationError, match="不得修改"):
        await apply_migrations(pg_pool, empty_namespace)


async def test_concurrent_migrators_apply_once(
    pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace
) -> None:
    """多个实例同时迁移：只有一个真正执行，其余发现已是最新。"""
    results = await asyncio.gather(
        *(apply_migrations(pg_pool, empty_namespace) for _ in range(6))
    )
    assert sorted(len(result) for result in results) == [0, 0, 0, 0, 0, 1]


async def test_namespaces_are_isolated(pg_pool: asyncpg.Pool, empty_namespace: SqlNamespace) -> None:
    """同一个 schema 里不同前缀互不影响。"""
    from taifeng_stores import SqlNamespace as Namespace

    other = Namespace(schema=empty_namespace.schema, prefix="other_")
    await apply_migrations(pg_pool, empty_namespace)
    assert await current_version(pg_pool, other, "core") == 0
    await apply_migrations(pg_pool, other)
    assert {"tf_thread", "other_thread"} <= await _tables(pg_pool, empty_namespace)


async def test_cli_status_and_apply(
    postgres_backend: Backend,
    pg_pool: asyncpg.Pool,
    empty_namespace: SqlNamespace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令行：先看状态，再执行，再执行一次提示已是最新。"""
    base = [
        "--dsn", postgres_backend.url,
        "--schema", empty_namespace.schema,
        "--prefix", empty_namespace.prefix,
    ]  # fmt: skip
    assert await asyncio.to_thread(cli_main, [*base, "--status"]) == 0
    assert "core: 当前 0，要求 1" in capsys.readouterr().out
    assert await asyncio.to_thread(cli_main, base) == 0
    assert "已执行 core/0001_threads_and_messages" in capsys.readouterr().out
    assert await asyncio.to_thread(cli_main, base) == 0
    assert "已是最新" in capsys.readouterr().out
    await ensure_schema_current(pg_pool, empty_namespace, "core")


async def test_cli_reports_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """命令行：参数不合法或连不上库时返回非零并说明原因。"""
    code = await asyncio.to_thread(
        cli_main, ["--dsn", "postgresql://u:p@127.0.0.1:1/none", "--schema", "Bad-Schema"]
    )
    assert code == 1
    assert "迁移失败" in capsys.readouterr().err
