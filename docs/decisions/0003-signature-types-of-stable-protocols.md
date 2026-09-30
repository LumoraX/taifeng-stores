# ADR 0003：稳定协议签名里的类型视为稳定契约，经单点模块引入

- 状态：Accepted
- 日期：2026-09-30
- 关联：ADR 0001（决策 2）、ADR 0002；taifeng ADR 0066（公共 API 分层）

## 背景

红线 1 要求只 import `taifeng.__all__` 里的名字。实现 `MessageStore` 时核实到：协议方法 `list_threads` 的返回类型是 `ThreadInfo`，而 `ThreadInfo` 不在 taifeng `2026.9.30.1` 的顶层 `__all__` 里，只能从 `taifeng.conversation` 拿到。

也就是说，`MessageStore` 已经公开，但只用稳定层无法把它实现完整。这和 ADR 0002 处理的情况不同：那里是整个协议都没公开，这里是协议公开了、它签名里的一个数据类型漏了导出。

## 决策

1. **出现在稳定协议方法签名里的类型，视为稳定契约的一部分。** 协议公开即意味着承诺了它的输入输出形状。
2. **这类类型集中在 `taifeng_stores/_kernel_types.py` 单点引入**，其他模块只从这里 import。该模块只允许引入数据类型，不得引入任何实现。
3. **每个经此引入的名字由冒烟测试钉住**：存在、关键字段齐全。同一条测试还断言该名字尚未进入 taifeng 顶层；一旦 taifeng 补上导出，测试变红，提醒把引入方式改回顶层并删除条目。
4. **同时在 taifeng 侧立项补导出**（登记在 xuanpu `docs/architecture/kernel-seam-backlog.md` 第 3 项）。

当前经此引入的只有 `ThreadInfo`。

## 否决的方案

- **不实现 `list_threads`**：协议就不完整，`isinstance(store, MessageStore)` 虽然通过，调用时才炸。
- **自己定义一个同形状的类型返回**：调用方按 `ThreadInfo` 写的类型标注会对不上，而且内核以后给 `ThreadInfo` 加字段时这里会悄悄落后。
- **放开红线 1，允许按需 import 子模块**：口子一开就收不住，ADR 0002 的约束也会跟着失效。

## 后果

- 本仓对 taifeng 的依赖面多了一个有明确边界、有测试守护的例外。
- taifeng 调整 `ThreadInfo` 的字段会先让冒烟测试变红。
