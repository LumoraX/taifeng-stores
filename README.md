# taifeng-stores

[taifeng](https://github.com/LumoraX/taifeng) 的存储适配层：把 taifeng 定义的存储类协议接到 Postgres、Redis、对象存储与向量库。

> **状态**：第一版。Postgres（线程目录、消息存储、消息镜像、向量记忆）与 Redis（线程目录）可用，消息存储可当 `EnginePool` 的会话主存；对象存储未开始，会话日志后端等 taifeng 公开协议。

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

再把后端注入 taifeng。多实例共享会话时，把 Postgres 当会话主存：

```python
import asyncpg
import taifeng
from taifeng_stores import SqlNamespace
from taifeng_stores.postgres import PgvectorMemoryStore, PostgresMessageStore

pool = await asyncpg.create_pool(dsn)          # 连接池由宿主创建和关闭
namespace = SqlNamespace(schema="app", prefix="tf_")

store = PostgresMessageStore(pool, namespace=namespace)
await store.check_schema()                     # 表结构落后时明确报错

engines = await taifeng.EnginePool.create(
    skills_dir="/data/skills",
    model_client=model_client,                 # 只能是 Chat 协议的客户端
    message_store=store,                       # 会话主存进库；不给 storage_dir
    thread_directory=store.directory,          # 同库、同命名空间的线程目录
    memory_store=PgvectorMemoryStore(          # 长期记忆
        pool, embedder, dimensions=1536, scope="team-a", namespace=namespace
    ),
)

# 任一实例都能从库里续接线程
engine = await engines.get_or_create(
    session_id="s1", entry_skill_id="helper", resume_thread_id=thread_id
)
```

- 同一线程同一时刻只能有一个实例写入，由宿主保证：原持有方的写入全部结束后，接管方才能回放并写入。
- 模型客户端走 Responses 协议时，内核在建池时就报错：本实现没有原子批量写（[ADR 0005](docs/decisions/0005-main-store-for-engine-pool.md)）。
- 池关闭时会调 `store.close()`，它不关连接池。

`embedder` 是宿主提供的嵌入实现（一个 `async def embed(texts) -> list[list[float]]` 方法），本包不绑定任何嵌入服务。

### 只把消息镜像进库

会话主存留在本机 JSONL，只需要多实例共享的线程目录与消息视图时：

```python
from taifeng_stores.postgres import PostgresMessageMirror, PostgresThreadDirectory

directory = PostgresThreadDirectory(pool, namespace=namespace)
await directory.check_schema()

engines = await taifeng.EnginePool.create(
    skills_dir="/data/skills",
    storage_dir="/data/threads",               # 会话主存是本机 JSONL
    model_client=model_client,
    thread_directory=directory,                # 线程目录多实例共享
    index_hook=PostgresMessageMirror(pool, namespace=namespace),   # 消息镜像进库
)
```

镜像写入失败不影响会话，与本机 JSONL 不一致时以后者为准。同一命名空间不要既把 `PostgresMessageStore` 当主存、又挂镜像：两者写同一张表。

### Redis 线程目录

线程数量不大、只想要一个多实例共享的线程目录时，可以不用 Postgres：

```python
from redis.asyncio import Redis
from taifeng_stores.redis import RedisThreadDirectory

client = Redis.from_url("redis://localhost:6379/0")      # 客户端由宿主创建和关闭
directory = RedisThreadDirectory(client, prefix="app")   # 不需要迁移

engines = await taifeng.EnginePool.create(..., thread_directory=directory)
```

### 在代码里迁移

宿主自己管理发布流程时，可以不用命令行，直接调迁移函数：

```python
from taifeng_stores.postgres import (
    apply_migrations,
    current_version,
    ensure_schema_current,
    latest_version,
)

# 迁到最新；整个过程在一个事务里并持有咨询锁，多个实例同时启动也只会执行一次
await apply_migrations(pool, namespace, components=("core", "memory"), embedding_dimensions=1536)

await current_version(pool, namespace, "core")   # 数据库里的版本，从未迁移过为 0
latest_version("core")                           # 代码要求的版本
await ensure_schema_current(pool, namespace, "core")   # 只读核对，落后则抛 SchemaOutOfDateError
```

### 异常

| 异常 | 何时抛出 |
| --- | --- |
| `StoreConfigError`（`ValueError`） | 构造参数不合法：命名空间含非法字符、维度不是正整数、未知的迁移组件 |
| `SchemaOutOfDateError`（`RuntimeError`） | 表结构落后于代码要求；带 `component`、`current`、`required`。先运行迁移 |
| `MigrationError`（`RuntimeError`） | 迁移脚本执行失败，或已执行的迁移与脚本内容对不上 |

## 后端

| extra | 后端 | 覆盖的协议 | 状态 |
| --- | --- | --- | --- |
| `postgres` | PostgreSQL（asyncpg） | `ThreadDirectory`、`MessageStore`、`IndexHook` | 可用 |
| `pgvector` | pgvector | `MemoryStore` | 可用 |
| `redis` | Redis | `ThreadDirectory` | 可用 |
| `s3` | S3 兼容对象存储 | 大体积内容外置 | 未开始 |

会话日志（taifeng `SessionJournal`）后端要等 taifeng 先公开相关协议，见 [ADR 0002](docs/decisions/0002-journal-backend-waits-for-kernel.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [接口参考](docs/architecture/reference.md) | 命名空间规则、迁移命令的全部选项、每个后端的构造参数与行为、嵌入协议、异常 |
| [架构总览](docs/architecture/README.md) | 两种接法、数据布局、契约测试、已知限制 |
| [决策记录](docs/decisions/README.md) | 为什么这么定 |

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
