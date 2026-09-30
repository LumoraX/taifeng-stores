"""表结构迁移：按组件、按版本号顺序执行随包分发的 SQL 脚本。

红线 6：表结构变更必须有迁移脚本，存储实现不在运行时隐式建表改表。宿主在部署流程里调
``apply_migrations``（或命令行 ``taifeng-stores-migrate``），存储实现只在需要时用
``ensure_schema_current`` 核对版本。

组件互相独立、各记各的版本：

- ``core``：线程目录与消息表；
- ``memory``：长期记忆表，依赖 pgvector 扩展，按需启用。

并发安全：迁移在一个事务里执行，事务开头取咨询锁；多个实例同时启动时只有一个真正执行，
其余的等锁释放后发现已是最新。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import TYPE_CHECKING

import asyncpg

from taifeng_stores.errors import MigrationError, SchemaOutOfDateError, StoreConfigError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg.pool import PoolConnectionProxy

    from taifeng_stores.namespace import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

COMPONENTS: tuple[str, ...] = ("core", "memory")

_SCRIPT_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_MAX_DIMENSIONS = 16000


@dataclass(frozen=True)
class Migration:
    """一个迁移脚本。"""

    component: str
    version: int
    name: str
    sql_template: str

    @property
    def checksum(self) -> str:
        """脚本模板的摘要，用来发现「已执行过的脚本事后被改动」。"""
        return hashlib.sha256(self.sql_template.encode("utf-8")).hexdigest()


def load_migrations(component: str) -> list[Migration]:
    """读取某个组件的全部迁移脚本，按版本号升序。"""
    if component not in COMPONENTS:
        raise StoreConfigError(f"未知的迁移组件：{component!r}（可选：{', '.join(COMPONENTS)}）")
    directory = resources.files("taifeng_stores.postgres").joinpath("migrations", component)
    migrations: list[Migration] = []
    for entry in directory.iterdir():
        matched = _SCRIPT_NAME.match(entry.name)
        if matched is None:
            continue
        migrations.append(
            Migration(
                component=component,
                version=int(matched.group(1)),
                name=matched.group(2),
                sql_template=entry.read_text("utf-8"),
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"组件 {component!r} 的迁移版本号不连续：{versions}")
    return migrations


def latest_version(component: str) -> int:
    """代码要求的表结构版本。"""
    migrations = load_migrations(component)
    return migrations[-1].version if migrations else 0


def render(
    migration: Migration, namespace: SqlNamespace, *, embedding_dimensions: int | None
) -> str:
    """把命名空间与参数代入脚本模板。"""
    values = {"schema": namespace.schema, "prefix": namespace.prefix}
    if "${dimensions}" in migration.sql_template:
        if embedding_dimensions is None:
            raise StoreConfigError(f"组件 {migration.component!r} 需要指定 embedding_dimensions")
        if not 1 <= embedding_dimensions <= _MAX_DIMENSIONS:
            raise StoreConfigError(f"embedding_dimensions 超出范围：{embedding_dimensions}")
        values["dimensions"] = str(embedding_dimensions)
    try:
        return Template(migration.sql_template).substitute(values)
    except KeyError as exc:
        raise MigrationError(
            f"迁移脚本 {migration.component}/{migration.version:04d} 含未知占位符 {exc}"
        ) from exc


def _ledger_table(namespace: SqlNamespace) -> str:
    """迁移记录表。"""
    return namespace.table("schema_migrations")


def _lock_key(namespace: SqlNamespace) -> int:
    """按命名空间派生咨询锁的键（有符号 64 位）。"""
    digest = hashlib.sha256(f"{namespace.schema}.{namespace.prefix}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def _ensure_ledger(conn: Connection, namespace: SqlNamespace) -> None:
    """建迁移记录表（迁移工具自己的簿记，不属于任何组件）。"""
    await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{namespace.schema}"')
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_ledger_table(namespace)} ("
        "component TEXT NOT NULL, "
        "version INTEGER NOT NULL, "
        "name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "PRIMARY KEY (component, version))"
    )


async def _applied(
    conn: Connection, namespace: SqlNamespace, component: str
) -> dict[int, str]:
    """已执行的迁移：版本号 → 摘要。"""
    rows = await conn.fetch(
        f"SELECT version, checksum FROM {_ledger_table(namespace)} WHERE component = $1",
        component,
    )
    return {int(row["version"]): str(row["checksum"]) for row in rows}


async def _apply_component(
    conn: Connection,
    namespace: SqlNamespace,
    component: str,
    *,
    embedding_dimensions: int | None,
) -> list[Migration]:
    """在当前事务里把一个组件迁移到最新。"""
    applied = await _applied(conn, namespace, component)
    executed: list[Migration] = []
    for migration in load_migrations(component):
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != migration.checksum:
                raise MigrationError(
                    f"迁移 {component}/{migration.version:04d} 已执行，但脚本内容与执行时不同；"
                    "已发布的迁移不得修改，请新增迁移脚本"
                )
            continue
        sql = render(migration, namespace, embedding_dimensions=embedding_dimensions)
        try:
            await conn.execute(sql)
        except asyncpg.PostgresError as exc:
            raise MigrationError(
                f"迁移 {component}/{migration.version:04d}_{migration.name} 执行失败：{exc}"
            ) from exc
        await conn.execute(
            f"INSERT INTO {_ledger_table(namespace)} (component, version, name, checksum) "
            "VALUES ($1, $2, $3, $4)",
            component,
            migration.version,
            migration.name,
            migration.checksum,
        )
        executed.append(migration)
    return executed


async def apply_migrations(
    pool: asyncpg.Pool,
    namespace: SqlNamespace,
    *,
    components: Sequence[str] = ("core",),
    embedding_dimensions: int | None = None,
) -> list[Migration]:
    """把指定组件迁移到最新版本，返回本次实际执行的迁移。

    整个过程在一个事务里：任何一步失败全部回滚，不会留下迁到一半的表结构。
    """
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _lock_key(namespace))
        await _ensure_ledger(conn, namespace)
        executed: list[Migration] = []
        for component in components:
            executed.extend(
                await _apply_component(
                    conn, namespace, component, embedding_dimensions=embedding_dimensions
                )
            )
        return executed


async def current_version(pool: asyncpg.Pool, namespace: SqlNamespace, component: str) -> int:
    """数据库里某个组件的当前版本；从未迁移过返回 0。"""
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", _ledger_table(namespace)
        )
        if not exists:
            return 0
        version = await conn.fetchval(
            f"SELECT max(version) FROM {_ledger_table(namespace)} WHERE component = $1",
            component,
        )
    return int(version) if version is not None else 0


async def ensure_schema_current(
    pool: asyncpg.Pool, namespace: SqlNamespace, component: str
) -> None:
    """核对表结构版本，落后则抛 ``SchemaOutOfDateError``。只读，不改库。"""
    required = latest_version(component)
    current = await current_version(pool, namespace, component)
    if current < required:
        raise SchemaOutOfDateError(component, current, required)


__all__ = [
    "COMPONENTS",
    "Migration",
    "apply_migrations",
    "current_version",
    "ensure_schema_current",
    "latest_version",
    "load_migrations",
    "render",
]
