# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。

## 现状

第一版：Postgres 后端（线程目录、消息存储、消息镜像、向量记忆、表结构迁移）与 Redis 后端（线程目录）可用。对象存储未开始；会话日志后端仍在等 taifeng 公开协议。

## 模块

| 模块 | 内容 |
| --- | --- |
| `taifeng_stores.namespace` | `SqlNamespace`（schema 加表名前缀）与键前缀校验 |
| `taifeng_stores.postgres.migrate` | 迁移执行与版本核对；脚本在 `postgres/migrations/<组件>/` |
| `taifeng_stores.postgres.cli` | 命令行 `taifeng-stores-migrate` |
| `taifeng_stores.postgres.thread_directory` | `PostgresThreadDirectory` |
| `taifeng_stores.postgres.message_store` | `PostgresMessageStore` |
| `taifeng_stores.postgres.message_mirror` | `PostgresMessageMirror`（`IndexHook`） |
| `taifeng_stores.postgres.memory_store` | `PgvectorMemoryStore` 与 `Embedder` 协议 |
| `taifeng_stores.redis.thread_directory` | `RedisThreadDirectory` |
| `taifeng_stores._kernel_types` | 稳定协议签名类型的单点引入（ADR 0003） |

## 两种接法

taifeng 的 `EnginePool.create` 目前固定使用节点本地的 JSONL 作会话主存，不接受外部 `MessageStore`。所以按宿主怎么承载会话，有两种接法：

```
A. 经 EnginePool 承载会话（常见）            B. 直接构造 AgentEngine

   EnginePool.create(                          AgentEngine(
     storage_dir=本机目录,        ← 主存          store=PostgresMessageStore,  ← 主存
     thread_directory=                          ...)
       PostgresThreadDirectory,   ← 共享目录
     index_hook=
       PostgresMessageMirror,     ← 共享视图
     memory_store=
       PgvectorMemoryStore)       ← 长期记忆
```

接法 A 里 Postgres 中的消息是镜像：供其他节点、审计与查询服务读取，写入失败不影响会话，与本机 JSONL 不一致时以后者为准。接法 B 里 Postgres 是主存，但只能配 Chat 协议的模型客户端（见「已知限制」）。

## 数据布局

Postgres（`core` 组件）：

| 表 | 内容 |
| --- | --- |
| `<前缀>thread` | 线程元数据。按 `(updated_at, thread_id)` 倒序翻页；标签列有 GIN 索引 |
| `<前缀>message` | 消息。回放顺序 `(position, seq)`；`(thread_id, item_id)` 唯一，重复写入去重 |
| `<前缀>schema_migrations` | 迁移记录：组件、版本、脚本摘要 |

Postgres（`memory` 组件）：`<前缀>memory`，含 `scope`、来源线程与消息、正文、向量；余弦距离的 HNSW 索引。

Redis：`<前缀>:thread:<id>` 哈希存元数据，`<前缀>:threads` 有序集合按 `updated_at` 排序。元数据更新由服务端脚本原子完成。

## 契约与验证

- 线程目录的契约用例在 `tests/contract/`，同一套用例对 Postgres、Redis 和内核自带的 `SqliteThreadDirectory` 执行。内核实现是参照：它通过，说明用例没有偏离内核语义。
- 消息存储、消息镜像、向量记忆各有一条把实现挂到内核引擎上真跑对话的用例。
- 所有存储行为对真实后端验证：设置 `TAIFENG_STORES_TEST_PG_DSN`、`TAIFENG_STORES_TEST_REDIS_URL` 使用已有服务，否则用 Docker 临时拉起；每个用例独占一个 schema 或键前缀，结束时清理。

## 组件状态

| 组件 | extra | 覆盖协议 | 状态 |
| --- | --- | --- | --- |
| Postgres 线程目录 | `postgres` | `ThreadDirectory` | 可用 |
| Postgres 消息存储 | `postgres` | `MessageStore` | 可用（仅 Chat 协议） |
| Postgres 消息镜像 | `postgres` | `IndexHook` | 可用 |
| pgvector 向量记忆 | `pgvector` | `MemoryStore` | 可用 |
| Redis 线程目录 | `redis` | `ThreadDirectory` | 可用 |
| 对象存储 | `s3` | 大体积内容外置 | 未开始（taifeng 尚无对应协议） |
| 会话日志后端 | — | `SessionJournal` 相关 | 阻塞：等 taifeng 公开协议（ADR 0002） |

## 已知限制

- `PostgresMessageStore` 不能配 Responses 协议的模型客户端：那需要原子批量写协议，taifeng 尚未公开。
- 与内核默认的线程目录相比：不做孤儿检测；游标损坏时写日志而不是发事件（事件出口协议未公开）。
- 会话日志仍是节点本地的，跨节点接管会话需要共享卷。
- Redis 目录带过滤条件的列表是顺序扫描。
- 向量维度在迁移时定死。

## 依赖的 taifeng 协议

| 协议或类型 | 在 taifeng 稳定层 |
| --- | --- |
| `MessageStore`、`MessageWriter`、`ThreadDirectory`、`IndexHook`、`MemoryStore` | 是 |
| `ThreadMetadata`、`ThreadFilter`、`ThreadPage`、`ResponseItem`、`DirectoryError`、`ThreadNotFoundError` | 是 |
| `ThreadInfo` | 否；是 `MessageStore.list_threads` 的返回类型，经单点模块引入（ADR 0003） |
| `JsonlSessionJournalCore` | 否，在实验层 |
| `WriterLockAdapter`、`ConversationProjectionStore`、`AtomicBatchMessageStore` | 否，未公开 |

taifeng 自 2026.9.30.1 起带 `py.typed`，本仓对 taifeng 协议的使用受 mypy strict 检查。
