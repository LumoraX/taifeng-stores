"""命令行：``taifeng-stores-migrate``，在部署流程里执行表结构迁移。"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from taifeng_stores.errors import MigrationError, StoreConfigError
from taifeng_stores.namespace import SqlNamespace
from taifeng_stores.postgres.migrate import (
    COMPONENTS,
    apply_migrations,
    current_version,
    latest_version,
)


def build_parser() -> argparse.ArgumentParser:
    """命令行参数定义。"""
    parser = argparse.ArgumentParser(
        prog="taifeng-stores-migrate",
        description="执行 taifeng-stores 的 Postgres 表结构迁移",
    )
    parser.add_argument("--dsn", required=True, help="数据库连接串（postgresql://...）")
    parser.add_argument("--schema", default="public", help="schema 名（默认 public）")
    parser.add_argument("--prefix", default="taifeng_", help="表名前缀（默认 taifeng_）")
    parser.add_argument(
        "--component",
        action="append",
        choices=COMPONENTS,
        help="要迁移的组件，可重复指定（默认只迁移 core）",
    )
    parser.add_argument(
        "--embedding-dimensions", type=int, default=None, help="memory 组件的向量维度"
    )
    parser.add_argument("--status", action="store_true", help="只显示版本，不执行迁移")
    return parser


async def _run(args: argparse.Namespace) -> int:
    """连接数据库并执行。"""
    namespace = SqlNamespace(schema=args.schema, prefix=args.prefix)
    components: list[str] = args.component or ["core"]
    pool = await asyncpg.create_pool(args.dsn, min_size=1, max_size=1)
    try:
        if args.status:
            for component in components:
                current = await current_version(pool, namespace, component)
                print(f"{component}: 当前 {current}，要求 {latest_version(component)}")
            return 0
        executed = await apply_migrations(
            pool,
            namespace,
            components=components,
            embedding_dimensions=args.embedding_dimensions,
        )
    finally:
        await pool.close()
    if not executed:
        print("表结构已是最新")
    for migration in executed:
        print(f"已执行 {migration.component}/{migration.version:04d}_{migration.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (MigrationError, StoreConfigError, asyncpg.PostgresError, OSError) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
