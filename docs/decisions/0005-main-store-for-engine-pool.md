# ADR 0005：PostgresMessageStore 当 EnginePool 的会话主存

- 状态：Accepted
- 日期：2026-10-02
- 关联：Supersedes #0003；补充 ADR 0004（决策 10、后果第三条）；taifeng ADR 0110、ADR 0111

## 背景

ADR 0003、0004 写下时，taifeng（2026.9.30.1）有两处缺口：

- `MessageStore.list_threads` 的返回类型 `ThreadInfo` 不在稳定层，本仓只好经单点模块 `_kernel_types.py` 从子模块引入（ADR 0003）；
- `EnginePool.create` 固定自建本机 JSONL 主存，不接受外部 `MessageStore`。经 `EnginePool` 承载会话的宿主只能把 Postgres 当镜像（`PostgresMessageMirror`），`PostgresMessageStore` 只有直接构造 `AgentEngine` 时用得上。

taifeng 2026.10.1.10 补上了这两处：

- ADR 0110 把 `ThreadInfo`、`AtomicBatchMessageStore`、`BatchAppendAck`、模拟器 `SimClient` 等提到稳定层；
- ADR 0111 给 `EnginePool.create` 加了 `message_store=`：与 `storage_dir` / `threads_dir` 互斥；对象须满足 `MessageStore`；模型客户端走 Responses 协议而 store 没有实现 `AtomicBatchMessageStore` 时建池即报错；不给 `thread_directory` 时用 `NullThreadDirectory`；池建成后拥有 store，`close()` 时调 `store.close()`；审计模式不接受外部 store。

平台侧多实例部署要的是会话本身可共享：任一实例都能接管任一线程。镜像做不到——它是钩子驱动、写入失败不影响会话的视图，主存仍在节点本地。

把 `PostgresMessageStore` 当主存，又带出一个 ADR 0004 没有覆盖的问题：`position` 取自写入方的墙钟，接管方的时钟比原持有方慢时，它写的消息位置会小于库里已有的消息，回放时排到前面。对镜像这只是「少量错位」（ADR 0004 后果第三条）；对主存，这是喂给模型的历史乱了序，不能接受。

## 决策

1. **稳定协议签名里的类型从 `taifeng` 顶层引入**，删掉 `_kernel_types.py`。取代 ADR 0003：单点模块是绕开缺口的权宜之计，缺口在内核侧补上后就不再需要。冒烟测试改为断言 `ThreadInfo` 在稳定层、单点模块已不存在。

2. **`PostgresMessageStore` 可作 `EnginePool` 的会话主存。** 用法：

   ```python
   store = PostgresMessageStore(pool, namespace=namespace)
   engines = await taifeng.EnginePool.create(
       skills_dir=..., model_client=..., message_store=store, thread_directory=store.directory,
   )
   ```

   不给 `storage_dir`。边界：

   - 同一命名空间不要再挂 `PostgresMessageMirror`：两者写同一张表；
   - 只配 Chat 协议的模型客户端（决策 4）；
   - `list_threads` 返回的 `item_count` 恒为 0；
   - 同一线程同一时刻只能有一个写者：原持有方的写入全部结束后，接管方才能回放并写入（见「后果」）。

   直接构造 `AgentEngine(store=...)` 的用法照旧可用。

3. **接管时以回放看到的最大位置抬高全局下限。** `load_thread` 在归还连接之前，把这次回放看到的最大 `position` 交给 `PositionClock.observe`；之后本实例发出的位置严格大于它，并继续严格递增。这是对 ADR 0004 决策 10 的补充：位置仍在调用进入时、第一个 `await` 之前同步取得，只是多了一个下限。

   下限是**整个实例共用一个**，不按线程分别记：

   - 按线程记换不来额外保证。正确性只要求一个线程的新写入排在它已有消息之后；全局下限不小于每个被观察过的线程的最大位置，对每个线程都成立。按线程记唯一的区别是观察一个线程不会抬高别的线程的位置，但时钟每发一个位置就把全局下限推到那里，本实例往被观察的线程写一次，这点区别也就没了。
   - 按线程记要一张随线程数增长的表，长驻进程还得处理容量与淘汰；淘汰掉的线程退回全局下限，复杂度换不来什么。
   - 被别的线程抬高无害：`position` 只用于同一线程内排序，不当时间用，值大一些不影响任何读者。效果类似 Lamport 时钟。

4. **不实现 `AtomicBatchMessageStore`。** 这个协议已随 taifeng ADR 0110 进了稳定层，技术上可以做；但唯一的使用方 xuanpu 只用 Chat 协议，现在做就是没有使用方的代码。等出现 Responses 协议的需求再加：届时要按 `batch_id` 记录已提交的批次、保证重试幂等，需要一次表结构迁移。

## 否决的方案

- **继续只做镜像，主存留在本机**：跨节点接管要靠共享卷，不满足多实例共享会话的目标。
- **按线程分别记下限**：见决策 3。第一版实现过（带容量与淘汰），随后简化掉。
- **写入时由数据库分配位置（序列或 `max(position) + 1`）**：位置就按落库先后排了，又回到 ADR 0004 撞上的并发乱序；`max + 1` 还要在事务里锁住线程。
- **顺手实现原子批量写**：见决策 4。

## 后果

- `PostgresMessageStore` 只配 Chat 协议的模型客户端。配 Responses 协议的客户端，内核在建池时抛 `UnsupportedPersistenceCapabilityError`（不在稳定层，稳定层基类是 `LLMError`）。
- **单写者前提由宿主保证，本包解决不了。** 同一线程同一时刻只能有一个实例写入。用租约分配线程的宿主要注意：租约过期不等于原持有方的在途写入已经落库——宿主要保证续期失败的一方立即停止写入（例如续期失败即关闭该线程的引擎），否则两边交错写入，回放顺序不再有保证。本包不提供跨实例的写者锁：那要配合内核的写者接管语义，属 ADR 0002 那条线。
- ADR 0003 被本 ADR 取代。
- ADR 0004 后果中「接管点前后的少量镜像消息可能错位」对主存不再成立（决策 3）；对镜像仍成立——镜像不回放，没有可观察的位置，仍只按墙钟取值。
- 池关闭时会调 `store.close()`，本实现的 `close()` 不关连接池；连接池仍由宿主创建和关闭，可以在多个池之间共用。
- 审计模式（`audit=`）不接受外部 store：要审计的会话仍用本机 JSONL 主存。
