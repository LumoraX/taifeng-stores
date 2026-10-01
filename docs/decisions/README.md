# 决策记录（ADR）

只增不改；要推翻旧决策，写新 ADR 并标 `Supersedes #NNNN`。

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| [0001](0001-positioning-and-boundaries.md) | 定位与边界 —— taifeng 存储类协议的外部后端，独立于内核发布 | Accepted |
| [0002](0002-journal-backend-waits-for-kernel.md) | 会话日志后端等 taifeng 先公开日志协议，不绕道私有模块 | Accepted |
| [0003](0003-signature-types-of-stable-protocols.md) | 稳定协议签名里的类型视为稳定契约，经单点模块引入 | Superseded by [0005](0005-main-store-for-engine-pool.md) |
| [0004](0004-schema-migrations-namespaces-and-replay-order.md) | 表结构迁移、命名空间与消息回放顺序 | Accepted；回放顺序由 [0005](0005-main-store-for-engine-pool.md) 补充（主存接管时不再错位，镜像照旧） |
| [0005](0005-main-store-for-engine-pool.md) | PostgresMessageStore 当 EnginePool 的会话主存 | Accepted |
