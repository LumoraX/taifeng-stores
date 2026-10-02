# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。
>
> 相关：[接口参考](reference.md)

## 现状

第一版：Postgres 后端（线程目录、消息存储、消息镜像、向量记忆、表结构迁移）与 Redis 后端（线程目录）可用，消息存储可当 `EnginePool` 的会话主存。对象存储未开始；会话日志后端仍在等 taifeng 公开协议。

## 模块

| 模块 | 内容 |
| --- | --- |
| `taifeng_stores.namespace` | `SqlNamespace`（schema 加表名前缀）与键前缀校验 |
| `taifeng_stores.postgres.migrate` | 迁移执行与版本核对；脚本在 `postgres/migrations/<组件>/` |
| `taifeng_stores.postgres.cli` | 命令行 `taifeng-stores-migrate` |
| `taifeng_stores.postgres.thread_directory` | `PostgresThreadDirectory` |
| `taifeng_stores.postgres.message_store` | `PostgresMessageStore` |
| `taifeng_stores.postgres.message_mirror` | `PostgresMessageMirror`（`IndexHook`） |
| `taifeng_stores.postgres.messages` | 消息存储与镜像共用的消息行读写、位置时钟 `PositionClock` |
| `taifeng_stores.postgres.memory_store` | `PgvectorMemoryStore` 与 `Embedder` 协议 |
| `taifeng_stores.redis.thread_directory` | `RedisThreadDirectory` |

内核类型一律从 `taifeng` 顶层引入（ADR 0005 决策 1）。

## 两种接法

按会话主存放在哪里，经 `EnginePool` 承载会话有两种接法：

```
A. 会话主存进库（多实例共享会话）              B. 主存留在本机，消息镜像进库

   store = PostgresMessageStore(...)
   EnginePool.create(                            EnginePool.create(
     message_store=store,          ← 主存          storage_dir=本机目录,         ← 主存
     thread_directory=                             thread_directory=
       store.directory,            ← 共享目录        PostgresThreadDirectory,    ← 共享目录
     memory_store=                                 index_hook=
       PgvectorMemoryStore)        ← 长期记忆        PostgresMessageMirror,      ← 共享视图
                                                   memory_store=
                                                     PgvectorMemoryStore)        ← 长期记忆
```

| | A. 会话主存 | B. 镜像 |
| --- | --- | --- |
| Postgres 里的消息是 | 会话本身，引擎回放的就是它 | 视图，供其他节点、审计与查询服务读取 |
| 写入失败 | 就是会话自身的写入失败，异常交给内核 | 不影响会话，镜像缺数据（内核发 `index_hook_failed`） |
| 不一致时 | 只有一份，无所谓不一致 | 以本机 JSONL 为准 |
| 跨节点接管 | 任一实例按 `resume_thread_id` 从库里续接，接管方的时钟慢也不乱序；同一线程同一时刻只能有一个实例写入，由宿主保证 | 要共享卷；接管点前后的少量镜像消息可能错位 |
| 模型客户端 | 只能是 Chat 协议 | 不限 |

**同一命名空间只选一种。** 两者写同一张消息表：既当主存又挂镜像，同一条消息会被两条路径各写一次，回放顺序就不再只由主存决定。

接法 A 的接管顺序：接管方先 `load_thread` 回放，回放看到的最大位置抬高本实例位置时钟的全局下限，之后的写入都排在已有消息之后（ADR 0005 决策 3）。经 `EnginePool` 用 `resume_thread_id` 续接时内核会自动先回放（taifeng ADR 0111）；但 `get_or_create` 命中缓存时不重新回放，所以失去租约的一方必须 `release` 这个会话的引擎。镜像不回放，没有这一步，所以接管点错位只在接法 B 出现。

此外，`PostgresMessageStore` 也可以直接交给 `AgentEngine(store=...)`，不经 `EnginePool`。

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
- 消息存储、消息镜像、向量记忆各有把实现挂到内核引擎上真跑对话的用例。消息存储有两条：直接构造 `AgentEngine` 跑两轮；经 `EnginePool` 当主存，第一个池跑一轮后关闭，第二个池以另一个存储实例（时钟停在 1970）续接同一线程，并断言上一轮的问答进了模型请求、续接后的写入排在已有消息之后。另有一条钉住「配 Responses 协议的客户端建池即报错」。模型客户端用内核稳定层的 `SimClient`，或测试辅助里的脚本化客户端。
- 接管时的回放顺序有专门用例：接管方的时钟停在 1970 年，回放后写入的消息仍排在已有消息之后。
- 所有存储行为对真实后端验证：设置 `TAIFENG_STORES_TEST_PG_DSN`、`TAIFENG_STORES_TEST_REDIS_URL` 使用已有服务，否则用 Docker 临时拉起；每个用例独占一个 schema 或键前缀，结束时清理。

## 组件状态

| 组件 | extra | 覆盖协议 | 状态 |
| --- | --- | --- | --- |
| Postgres 线程目录 | `postgres` | `ThreadDirectory` | 可用 |
| Postgres 消息存储 | `postgres` | `MessageStore` | 可用，可当 `EnginePool` 主存（仅 Chat 协议） |
| Postgres 消息镜像 | `postgres` | `IndexHook` | 可用 |
| pgvector 向量记忆 | `pgvector` | `MemoryStore` | 可用 |
| Redis 线程目录 | `redis` | `ThreadDirectory` | 可用 |
| 对象存储 | `s3` | 大体积内容外置 | 未开始（taifeng 尚无对应协议） |
| 会话日志后端 | — | `SessionJournal` 相关 | 阻塞：等 taifeng 公开协议（ADR 0002） |

## 已知限制

- `PostgresMessageStore` 不能配 Responses 协议的模型客户端：那要求 store 实现 `AtomicBatchMessageStore`。协议已在 taifeng 稳定层，本实现暂不做（ADR 0005 决策 4），内核在建池时就会报错。
- `PostgresMessageStore.list_threads` 返回的 `item_count` 恒为 0。
- 单写者前提由宿主保证：同一线程同一时刻只能有一个实例写入。租约过期不等于在途写入已落库，宿主要保证续期失败的一方立即停止写入；本包不提供跨实例的写者锁。
- 与内核默认的线程目录相比：不做孤儿检测；游标损坏时写日志而不是发事件（事件出口协议未公开）。
- 审计模式（`audit=`）不接受外部 store；要审计的会话、以及会话日志，仍是节点本地的，跨节点接管需要共享卷（ADR 0002）。
- 镜像在跨节点接管点前后可能有少量消息错位（ADR 0004）。
- Redis 目录带过滤条件的列表是顺序扫描。
- 向量维度在迁移时定死。

## 依赖的 taifeng 协议

| 协议或类型 | 在 taifeng 稳定层 |
| --- | --- |
| `MessageStore`、`MessageWriter`、`ThreadDirectory`、`IndexHook`、`MemoryStore` | 是 |
| `ThreadMetadata`、`ThreadFilter`、`ThreadPage`、`ResponseItem`、`DirectoryError`、`ThreadNotFoundError` | 是 |
| `ThreadInfo`（`MessageStore.list_threads` 的返回类型） | 是，自 2026.10.1.10（taifeng ADR 0110） |
| `EnginePool.create(message_store=)` | 是，自 2026.10.1.10（taifeng ADR 0111） |
| `AtomicBatchMessageStore`、`BatchAppendAck` | 是，自 2026.10.1.10；本仓暂不实现（ADR 0005） |
| `SimClient`、`SimTurn`、`SimExpect`（只在测试里用） | 是，自 2026.10.1.10 |
| `JsonlSessionJournalCore`、`WriterLockAdapter` | 否，在实验层 |
| `ConversationProjectionStore` | 否，未公开 |

本仓要求 taifeng ≥ 2026.10.1.10。taifeng 自 2026.9.30.1 起带 `py.typed`，本仓对 taifeng 协议的使用受 mypy strict 检查。
