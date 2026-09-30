-- 长期记忆表（pgvector）。
-- 前提：数据库已安装 vector 扩展（CREATE EXTENSION 需要相应权限，由运维预先执行）。
-- ${dimensions} 是向量维度，建表后不可更改；换嵌入模型需要新建命名空间。

CREATE TABLE "${schema}"."${prefix}memory" (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope      TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    content    TEXT NOT NULL,
    embedding  vector(${dimensions}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT "${prefix}memory_item_key" UNIQUE (scope, thread_id, item_id)
);

CREATE INDEX "${prefix}memory_scope_idx"
    ON "${schema}"."${prefix}memory" (scope);
CREATE INDEX "${prefix}memory_embedding_idx"
    ON "${schema}"."${prefix}memory" USING hnsw (embedding vector_cosine_ops);
