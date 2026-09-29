# AGENTS.md

给所有 AI agent 的工程协作约定。

## 项目身份

**taifeng-stores** 是 [taifeng](https://github.com/LumoraX/taifeng) 的存储适配层：实现 taifeng 的存储类协议（`MessageStore`、`ThreadDirectory`、`MemoryStore` 等），把数据落到 Postgres、Redis、对象存储、向量库。

它不是 agent 框架，不含业务概念，也不是某个平台的私有组件——任何 taifeng 宿主都可以单独引入。

## 红线

| # | 红线 | 说明 |
| --- | --- | --- |
| 1 | **只依赖 taifeng 稳定层** | 只 import `taifeng.__all__` 中的名字；不碰 `taifeng.experimental` 与私有模块 |
| 2 | **语义以 taifeng 协议为准** | 协议文档写明的顺序、原子性、幂等语义必须逐条满足；拿不准就回 taifeng 查契约，不自行放宽 |
| 3 | **零平台概念** | 不出现租户、业务包等概念；划分库 / schema / 前缀的参数由上层在构造时传入 |
| 4 | **缺对接口回内核补** | 需要的协议未公开时回 taifeng 立项，不绕道 import 私有模块 |
| 5 | **后端走 extras** | 每个后端一个 optional extra，核心包除 taifeng 外零运行时依赖 |
| 6 | **表结构走迁移** | 表结构变更必须有迁移脚本，不在运行时隐式建表改表 |

## 实现约束

- Python 3.12+，所有模块顶部 `from __future__ import annotations`
- 异步用 `anyio` / `asyncio`，不写同步阻塞 IO；长时操作必须可取消
- 配置经构造函数注入，**禁止 `os.getenv`**
- 文件 ≤ 800 行（警戒 500），函数 ≤ 80 行，圈复杂度 ≤ 10
- 所有 module / class / function 必须有中文 docstring，关键逻辑块写中文注释
- 禁止 silent fallback（`except: pass`、带默认值吞掉异常数据）
- 测试走真实后端（容器化的 Postgres / Redis），不用 mock 代替存储行为

## 常用命令

```bash
uv sync --extra dev
uv run pytest                                       # 全量测试
uv run ruff check --select F,S108,I,TC src tests    # 门禁 lint
uv run mypy src/                                    # 门禁类型检查（strict）
```

## 完成定义

标记完成或提交前：

1. 跑通 `uv run pytest`、门禁 ruff、`mypy src/`，三者全绿；
2. 在对话或 PR 描述里贴出实际命令与关键输出；
3. 红测试不得以「本来就红」为由跳过，先查清是否与本次改动相关。

## 文档体系

| 目录 | 是什么 | 变更后怎么处理 |
| --- | --- | --- |
| `docs/architecture/` | 当前生效的设计（活文档） | 直接更新，永远代表现状 |
| `docs/decisions/` | ADR（为什么这么定） | 只增不改；推翻旧决策写新 ADR 标 `Supersedes #NNNN` |

实现完成但 `docs/architecture/` 未同步，不得合并。

## 语言

文档、注释、commit message 用中文；变量 / 函数 / 类名用英文。
