# ADR 0001：定位与边界 —— taifeng 存储类协议的外部后端，独立于内核发布

- 状态：Accepted
- 日期：2026-09-29
- 关联：taifeng ADR 0008（Store 协议解耦）、taifeng ADR 0017（规则③）、taifeng ADR 0025（SessionJournal 唯一事实源）

## 背景

taifeng 按 ADR 0008 把存储抽成协议，按 ADR 0017 规则③只在内核里放单机默认实现（JSONL、SQLite），禁止内置任何具体后端。多实例部署需要的 Postgres、Redis 等后端只能在内核外实现。

taifeng 仓库的 `examples/persistence/` 已经有 Postgres、Redis 版 `ThreadDirectory` 骨架，但示例是「复制后自行改造」的形态，没有测试、不发布，各宿主会各自复制、各自演化。

codex 的做法可作参照：`thread-store` 只定义 `ThreadStore` trait 与本地 / 内存实现，README 明说「其他存储实现可以放在本仓库之外」。

## 决策

1. **只实现 taifeng 已公开的存储类协议**，未公开的协议不实现（见 ADR 0002）。
2. **只依赖 taifeng 稳定层**（`taifeng.__all__`），不 import 实验层与私有模块。
3. **零平台概念**：不认识租户；按租户划分库、schema、表前缀的参数由上层在构造时传入。
4. **每个后端一个 optional extra**，核心包除 taifeng 外没有运行时依赖。
5. **以 taifeng `examples/persistence/` 为起点**，补齐测试后作为正式实现发布。

## 后果

- 任何 taifeng 宿主都可以单独引入本包，不必再复制示例。
- taifeng 协议变更会直接影响本仓；冒烟测试钉住所依赖协议在 taifeng 稳定层的可见性。
