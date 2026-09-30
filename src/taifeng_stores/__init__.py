"""taifeng-stores —— taifeng 的存储适配层。

把 taifeng 定义的存储类协议（``MessageStore`` / ``ThreadDirectory`` / ``MemoryStore`` 等）
接到外部存储后端：Postgres、Redis、对象存储、向量库。taifeng 内核只内置 JSONL / SQLite
等本机默认实现（taifeng ADR 0008、ADR 0017 规则③），生产级后端由本包提供。

各后端在自己的子包里，装了对应 extra 才能导入：

- ``taifeng_stores.postgres``：线程目录、消息存储、消息镜像、向量记忆、表结构迁移；
- ``taifeng_stores.redis``：线程目录。
"""

from __future__ import annotations

from importlib.metadata import version

from taifeng_stores.errors import MigrationError, SchemaOutOfDateError, StoreConfigError
from taifeng_stores.namespace import SqlNamespace

__all__ = [
    "MigrationError",
    "SchemaOutOfDateError",
    "SqlNamespace",
    "StoreConfigError",
    "__version__",
]

# 版本单一来源是 pyproject.toml：从发行元数据读取，避免两处手改漂移
__version__ = version("taifeng-stores")
