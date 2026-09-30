"""Postgres 后端（extra：``postgres``；向量记忆另需 ``pgvector`` 扩展）。"""

from __future__ import annotations

from taifeng_stores.postgres.memory_store import Embedder, PgvectorMemoryStore
from taifeng_stores.postgres.message_mirror import PostgresMessageMirror
from taifeng_stores.postgres.message_store import PostgresMessageStore
from taifeng_stores.postgres.migrate import (
    apply_migrations,
    current_version,
    ensure_schema_current,
    latest_version,
)
from taifeng_stores.postgres.thread_directory import PostgresThreadDirectory

__all__ = [
    "Embedder",
    "PgvectorMemoryStore",
    "PostgresMessageMirror",
    "PostgresMessageStore",
    "PostgresThreadDirectory",
    "apply_migrations",
    "current_version",
    "ensure_schema_current",
    "latest_version",
]
