-- 线程目录与消息表。
-- ${schema} / ${prefix} 由迁移工具代入；取值在代入前已按标识符字符集校验。

CREATE TABLE "${schema}"."${prefix}thread" (
    thread_id      TEXT PRIMARY KEY,
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL,
    entry_skill_id TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'user',
    tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
    extra          JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 列表按 (updated_at, thread_id) 倒序翻页
CREATE INDEX "${prefix}thread_updated_idx"
    ON "${schema}"."${prefix}thread" (updated_at DESC, thread_id DESC);
CREATE INDEX "${prefix}thread_entry_skill_idx"
    ON "${schema}"."${prefix}thread" (entry_skill_id);
CREATE INDEX "${prefix}thread_tags_idx"
    ON "${schema}"."${prefix}thread" USING GIN (tags);

CREATE TABLE "${schema}"."${prefix}message" (
    seq        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- 回放顺序 = (position, seq)。position 是写入方在「调用进入时」取的序号：
    -- 行落库的先后可能被并发打乱，调用进入的先后不会。同一批消息共用一个 position，批内靠 seq。
    position   BIGINT NOT NULL,
    thread_id  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    -- 同一条消息重复写入时去重（镜像写入可能重试）
    CONSTRAINT "${prefix}message_item_key" UNIQUE (thread_id, item_id)
);

CREATE INDEX "${prefix}message_thread_position_idx"
    ON "${schema}"."${prefix}message" (thread_id, position, seq);
