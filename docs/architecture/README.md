# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。

## 现状

工程骨架：包可安装、CI 门禁就位，尚无后端实现。

## 组件状态

| 组件 | extra | 覆盖协议 | 状态 |
| --- | --- | --- | --- |
| Postgres 后端 | `postgres` | `MessageStore`、`ThreadDirectory` | 未开始（起点：taifeng `examples/persistence/postgres_thread_directory.py`） |
| Redis 后端 | `redis` | `ThreadDirectory` | 未开始（起点：taifeng `examples/persistence/redis_thread_directory.py`） |
| 对象存储 | `s3` | 大体积内容外置 | 未开始 |
| 向量记忆 | `pgvector` | `MemoryStore` | 未开始 |
| 会话日志后端 | — | `SessionJournal` 相关 | 阻塞：等 taifeng 公开协议（ADR 0002） |

## 依赖的 taifeng 协议

| 协议 | 在 taifeng 稳定层 |
| --- | --- |
| `MessageStore`、`MessageWriter`、`ThreadDirectory`、`IndexHook`、`MemoryStore` | 是（PyPI 2026.9.28.16 已含） |
| `JsonlSessionJournalCore` | 否，在实验层 |
| `WriterLockAdapter`、`ConversationProjectionStore`、`AtomicBatchMessageStore` | 否，未公开 |

另：taifeng 目前没有 `py.typed` 标记，本仓代码一旦 import taifeng，mypy strict 会报 `import-untyped`。需要 taifeng 补上标记后发版。
