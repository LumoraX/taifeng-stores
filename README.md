# taifeng-stores

[taifeng](https://github.com/LumoraX/taifeng) 的存储适配层：把 taifeng 定义的存储类协议接到 Postgres、Redis、对象存储与向量库。

> **状态**：工程骨架。包可安装、CI 门禁就位，尚无可用后端。

## 定位

taifeng 内核只定义存储协议，并内置 JSONL / SQLite 等单机默认实现；生产级、可多实例共享的后端由本仓提供。

| 由谁负责 | 内容 |
| --- | --- |
| **taifeng**（内核） | 协议定义与单机默认实现 |
| **taifeng-stores**（本仓） | 外部存储后端实现 |
| **上层平台** | 决定用哪个后端、按租户划分库 / schema / 前缀 |

## 规划中的后端

| extra | 后端 | 覆盖的协议 |
| --- | --- | --- |
| `postgres` | PostgreSQL（SQLAlchemy 2.0 async / asyncpg） | `MessageStore`、`ThreadDirectory` |
| `redis` | Redis | `ThreadDirectory` |
| `s3` | S3 兼容对象存储 | 大体积内容外置 |
| `pgvector` | pgvector | `MemoryStore` |

会话日志（taifeng `SessionJournal`）后端要等 taifeng 先公开相关协议，见 [ADR 0002](docs/decisions/0002-journal-backend-waits-for-kernel.md)。

## 开发

```bash
uv sync --extra dev
uv run pytest
uv run ruff check --select F,S108,I,TC src tests
uv run mypy src/
```

## 许可

Apache-2.0
