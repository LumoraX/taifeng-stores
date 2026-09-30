# taifeng-stores

[taifeng](https://github.com/LumoraX/taifeng) 的存储适配层：把 taifeng 定义的存储类协议接到 Postgres、Redis、对象存储与向量库。

> **状态**：第一版。Postgres（线程目录、消息存储、消息镜像、向量记忆）与 Redis（线程目录）可用；对象存储未开始，会话日志后端等 taifeng 公开协议。

## 定位

taifeng 内核只定义存储协议，并内置 JSONL / SQLite 等单机默认实现；生产级、可多实例共享的后端由本仓提供。

| 由谁负责 | 内容 |
| --- | --- |
| **taifeng**（内核） | 协议定义与单机默认实现 |
| **taifeng-stores**（本仓） | 外部存储后端实现 |
| **上层平台** | 决定用哪个后端、按租户划分库 / schema / 前缀 |

## 用法

先迁移表结构（本包不在运行时隐式建表）：

```bash
taifeng-stores-migrate --dsn postgresql://... --schema app --prefix tf_ \
    --component core --component memory --embedding-dimensions 1536
```

再把后端注入 taifeng：

```python
import asyncpg
import taifeng
from taifeng_stores import SqlNamespace
from taifeng_stores.postgres import (
    PgvectorMemoryStore,
    PostgresMessageMirror,
    PostgresThreadDirectory,
)

pool = await asyncpg.create_pool(dsn)          # 连接池由宿主创建和关闭
namespace = SqlNamespace(schema="app", prefix="tf_")

directory = PostgresThreadDirectory(pool, namespace=namespace)
await directory.check_schema()                 # 表结构落后时明确报错

engines = await taifeng.EnginePool.create(
    skills_dir="/data/skills",
    storage_dir="/data/threads",               # 会话主存仍是本机 JSONL
    model_client=model_client,
    thread_directory=directory,                # 线程目录多实例共享
    index_hook=PostgresMessageMirror(pool, namespace=namespace),   # 消息镜像进库
    memory_store=PgvectorMemoryStore(          # 长期记忆
        pool, embedder, dimensions=1536, scope="team-a", namespace=namespace
    ),
)
```

`embedder` 是宿主提供的嵌入实现（一个 `async def embed(texts) -> list[list[float]]` 方法），本包不绑定任何嵌入服务。

## 后端

| extra | 后端 | 覆盖的协议 | 状态 |
| --- | --- | --- | --- |
| `postgres` | PostgreSQL（asyncpg） | `ThreadDirectory`、`MessageStore`、`IndexHook` | 可用 |
| `pgvector` | pgvector | `MemoryStore` | 可用 |
| `redis` | Redis | `ThreadDirectory` | 可用 |
| `s3` | S3 兼容对象存储 | 大体积内容外置 | 未开始 |

会话日志（taifeng `SessionJournal`）后端要等 taifeng 先公开相关协议，见 [ADR 0002](docs/decisions/0002-journal-backend-waits-for-kernel.md)。设计与已知限制见 [docs/architecture/](docs/architecture/README.md)。

## 开发

```bash
uv sync --extra dev
uv run pytest
uv run ruff check --select F,S108,I,TC src tests
uv run mypy src/
```

测试对真实后端执行：设置 `TAIFENG_STORES_TEST_PG_DSN`（须装有 pgvector 扩展）与 `TAIFENG_STORES_TEST_REDIS_URL` 使用已有服务，否则用 Docker 临时拉起 `pgvector/pgvector:pg16` 与 `redis:7-alpine`；两者都没有时相关用例跳过。测试只在自己新建的 schema 与键前缀里读写。

## 许可

Apache-2.0
