# 接口参考（活文档）

> 本包对外的全部接口。用法示例见仓库根目录的 [README](../../README.md)，设计与数据布局见[架构总览](README.md)。

所有后端都遵守同一组约定：

- **连接由宿主管理**。构造时传入连接池或客户端，本包不创建、也不关闭它们。
- **不在运行时建表改表**。表结构靠迁移工具建立；每个 Postgres 后端都有 `check_schema()`，表结构落后时抛 `SchemaOutOfDateError`，建议在启动时调用一次。
- **不认识租户**。要隔离数据，给每个隔离单元一个不同的命名空间（或键前缀、`scope`）。

## 命名空间

### `SqlNamespace(schema="public", prefix="taifeng_")`

Postgres 里的一组表。

| 字段 | 约束 |
| --- | --- |
| `schema` | 小写字母或下划线开头，只含小写字母、数字、下划线，最长 63 |
| `prefix` | 表名前缀，规则同上，最长 32；空串表示不加前缀 |

标识符会拼进 SQL，所以字符集是写死的，不合法抛 `StoreConfigError`。

### Redis 键前缀

`RedisThreadDirectory(client, prefix="taifeng")`：字母、数字、下划线开头，可含 `_ . : -`，最长 128。

## 迁移

### 命令行 `taifeng-stores-migrate`

```bash
taifeng-stores-migrate --dsn postgresql://… --schema app --prefix tf_ \
    --component core --component memory --embedding-dimensions 1536
# 已执行 core/0001_threads_and_messages
# 已执行 memory/0001_memory
```

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--dsn` | 必填 | 连接串 |
| `--schema` | `public` | schema 名。不存在会自动创建 |
| `--prefix` | `taifeng_` | 表名前缀 |
| `--component` | `core` | 要迁移的组件，可重复。`core`：线程与消息表；`memory`：向量记忆表 |
| `--embedding-dimensions` | — | `memory` 组件的向量维度，迁移它时必填。**建表后不可更改**，换嵌入模型要用新的命名空间 |
| `--status` | — | 只显示各组件的当前版本与要求版本，不执行 |

`memory` 组件要求数据库已经装好 `vector` 扩展（`CREATE EXTENSION vector`，通常需要管理员预先执行）。

成功退出码是 0；失败时错误打到标准错误。可以重复运行，已是最新时什么都不做。

### 函数（`taifeng_stores.postgres`）

| 函数 | 说明 |
| --- | --- |
| `await apply_migrations(pool, namespace, *, components=("core",), embedding_dimensions=None)` | 迁到最新，返回本次执行了哪些。整个过程在一个事务里并持有咨询锁：失败全部回滚，多处同时运行也只执行一次 |
| `await current_version(pool, namespace, component)` | 数据库里的版本；从未迁移过是 0 |
| `latest_version(component)` | 代码要求的版本 |
| `await ensure_schema_current(pool, namespace, component)` | 只读核对，落后抛 `SchemaOutOfDateError` |

已执行过的迁移脚本会核对摘要：脚本内容与执行时不一致抛 `MigrationError`。

## Postgres 后端（`taifeng_stores.postgres`）

### `PostgresThreadDirectory(pool, *, namespace=None)`

实现 taifeng 的 `ThreadDirectory`：线程元数据的登记、更新、过滤与翻页。

| 方法 | 说明 |
| --- | --- |
| `upsert_metadata(meta)` | 登记或覆盖一个线程 |
| `update_metadata(thread_id, patch)` | 改部分字段；线程不存在抛 `ThreadNotFoundError` |
| `get_metadata(thread_id)` | 不存在返回 `None` |
| `list_threads(limit=50, cursor=None, filter=None)` | 按 `(updated_at, thread_id)` 倒序翻页，返回 `ThreadPage`（`items`、`next_cursor`）。`ThreadFilter` 可按入口 skill、创建时间范围、标签、来源过滤，条件之间是「并且」 |

行为与内核自带的 SQLite 实现一致，由同一套契约测试保证。两处差异：不做孤儿检测；游标损坏时写日志而不是发事件。

### `PostgresMessageMirror(pool, *, namespace=None)`

实现 taifeng 的 `IndexHook`：把会话消息镜像进 Postgres，供其他节点、审计与查询服务读取。

- 这是镜像，不是主存。写入失败不影响会话；与会话主存不一致时以主存为准。
- 同一条消息重复写入会去重。
- 回放顺序是 `(position, seq)`，与消息产生的顺序一致（内核并发触发写入时也成立）。

### `PostgresMessageStore(pool, *, namespace=None)`

实现 taifeng 的 `MessageStore`：把 Postgres 当会话主存。

- 只能配 Chat 协议的模型客户端（Responses 协议需要的原子批量写协议 taifeng 尚未公开）。
- 目前 `EnginePool` 不接受外部 `MessageStore`，只有直接构造 `AgentEngine` 时用得上。
- `store.directory` 是同一命名空间下的 `PostgresThreadDirectory`。
- 多实例共享时，同一线程同一时刻只能有一个实例写入（由宿主保证，例如租约）。接管线程的实例先 `load_thread` 回放再追加：回放记下库里已有的最大位置，之后的写入排在已有消息之后，与各实例的时钟偏差无关。另有关键字参数 `now_ns`，只为测试注入时钟。

### `PgvectorMemoryStore(pool, embedder, *, dimensions, …)`

实现 taifeng 的 `MemoryStore`：基于 pgvector 的长期记忆。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `embedder` | 必填 | 嵌入实现，见下 |
| `dimensions` | 必填 | 向量维度，必须与迁移时指定的一致 |
| `scope` | `"default"` | 记忆的共享边界：同一个 `scope` 下的所有线程共享记忆，不同 `scope` 互不可见。按用户、团队还是项目划分由宿主决定 |
| `namespace` | 默认命名空间 | |
| `top_k` | `5` | 每次最多取回几条 |
| `min_similarity` | `0.0` | 余弦相似度下限，低于它的不取回 |
| `max_chars` | `4000` | 取回文本的总字符上限 |
| `max_item_chars` | `8000` | 单条记忆的字符上限，超出部分不存 |
| `include_current_thread` | `False` | 是否取回当前线程自己写入的记忆。默认不取：那些内容通常还在上下文里 |
| `kinds` | 用户消息与助手消息 | 哪些类型的消息进长期记忆 |

| 行为 | 说明 |
| --- | --- |
| 什么时候写 | 每轮对话结束、上下文压缩丢弃内容之前、会话结束时。同一条消息只存一次。注意内核在每轮结束时只交来这一轮的输出，用户输入要到会话结束时才一并交来 |
| 什么时候读 | 每轮对话开始前，按用户输入取回相关记忆 |
| `await store.forget_thread(thread_id)` | 删除某个线程写入的全部记忆，返回删除条数。用于数据删除请求 |

### `Embedder`

宿主提供的嵌入实现，只要有这一个方法：

```python
class MyEmbedder:
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...     # 返回与 texts 等长的向量列表，每个向量的长度等于 dimensions
```

本包不绑定任何嵌入服务。返回的条数或维度不对会抛 `ValueError`。

## Redis 后端（`taifeng_stores.redis`）

### `RedisThreadDirectory(client, *, prefix="taifeng")`

实现 taifeng 的 `ThreadDirectory`。`client` 是 `redis.asyncio.Redis`。

- 元数据更新由服务端脚本原子完成，并发更新不会互相覆盖。
- 适合线程数量不大、过滤条件简单的场景：带过滤条件的列表是按时间倒序扫描、逐条比对，代价随线程总数增长。需要按标签、按入口 skill 高效过滤时用 Postgres 后端。
- 另有 `delete_thread(thread_id)`（协议之外的方法）。
- 不需要迁移。

## 异常

| 异常 | 基类 | 何时抛出 |
| --- | --- | --- |
| `StoreConfigError` | `ValueError` | 构造参数不合法：命名空间含非法字符、维度不是正整数、未知的迁移组件 |
| `SchemaOutOfDateError` | `RuntimeError` | 表结构落后于代码要求。带 `component`、`current`、`required` |
| `MigrationError` | `RuntimeError` | 迁移脚本执行失败，或已执行的迁移与脚本内容对不上 |

线程目录的运行期错误用 taifeng 自己的异常：`ThreadNotFoundError`、`DirectoryError`。
