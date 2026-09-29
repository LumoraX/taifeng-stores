# ADR 0002：会话日志后端等 taifeng 先公开日志协议，不绕道私有模块

- 状态：Accepted
- 日期：2026-09-29
- 关联：taifeng ADR 0025（SessionJournal 唯一事实源）、taifeng ADR 0053（审计会话恢复与写者接管）

## 背景

taifeng ADR 0025 把 `SessionJournal` 定为会话执行历史的唯一可靠事实源，`MessageStore` 退为它的对话兼容视图。所以多实例平台真正需要的，是一个可跨机器共享的会话日志后端，外加跨机器的写者锁（配合 ADR 0053 的写者接管），而不只是 `MessageStore`。

核实 taifeng 2026-09-29 的 main：

- 唯一的日志实现 `JsonlSessionJournalCore` 在实验层（`taifeng.experimental`），只能写本机文件；
- `WriterLockAdapter`（默认实现是本机 `fcntl` 文件锁）、`ConversationProjectionStore`、`AtomicBatchMessageStore` 既不在稳定层也不在实验层。

也就是说，外部包目前没有合规途径实现一个数据库版的会话日志后端。

## 决策

1. **本仓暂不实现会话日志后端**，只实现稳定层已公开的协议（`MessageStore`、`ThreadDirectory`、`MemoryStore` 等）。
2. **不绕道 import taifeng 私有模块**。私有模块随时可能变更，而且会让本仓与 taifeng 内部实现耦合。
3. **在 taifeng 侧立项**：把会话日志后端所需的协议提升为公开 API，并配一套协议一致性测试套件，供外部实现证明自己满足语义。具体公开哪些协议，在 taifeng 立项时核定。

## 后果

- 在 taifeng 完成上述立项并发版之前，基于本仓的多实例部署只能共享线程目录与消息视图，会话日志仍是单机的。
- 一致性测试套件落地后，本仓所有后端都要接入它作为验收门禁。
