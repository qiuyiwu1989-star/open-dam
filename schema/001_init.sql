-- open-dam · 初始化 schema(幂等,可反复执行)
-- 由 postgres 容器 docker-entrypoint-initdb.d 自动执行

CREATE EXTENSION IF NOT EXISTS vector;

-- 资产表:每张图一行,内容寻址(sha256 唯一)
CREATE TABLE IF NOT EXISTS asset (
  asset_id    bigserial PRIMARY KEY,
  sha256      text UNIQUE NOT NULL,
  phash       text,
  filename    text,
  bytes       bigint, fmt text, width int, height int,
  ave_rgb     text,                 -- '#a0a0a0'
  hue         text,                 -- 红|橙|黄|绿|青|蓝|紫|粉|中性|黑|白
  category    text,                 -- 零样本分类 top-1
  tags        text[] DEFAULT '{}',  -- 零样本打标
  storage_key text NOT NULL,        -- uploads/ab/<sha256>.png
  status      text NOT NULL DEFAULT 'processing',  -- processing|active|rejected
  q_score     double precision,     -- 质检分(清晰度)
  dupe_of     bigint,               -- phash 完全相同的先入资产(近重复折叠)
  created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS asset_status_idx ON asset(status, created_at DESC);
CREATE INDEX IF NOT EXISTS asset_hue_idx    ON asset(hue) WHERE status='active';
CREATE INDEX IF NOT EXISTS asset_cat_idx    ON asset(category) WHERE status='active';

-- 向量表:单一 HNSW 索引(cosine)
CREATE TABLE IF NOT EXISTS embedding (
  asset_id  bigint PRIMARY KEY REFERENCES asset(asset_id) ON DELETE CASCADE,
  model     text NOT NULL DEFAULT 'clip-vit-b32',
  embedding vector(512) NOT NULL
);
CREATE INDEX IF NOT EXISTS embedding_hnsw ON embedding USING hnsw (embedding vector_cosine_ops);

-- 集合
CREATE TABLE IF NOT EXISTS collection (
  id         bigserial PRIMARY KEY,
  name       text UNIQUE NOT NULL,
  created_at timestamptz DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collection_item (
  coll_id  bigint REFERENCES collection(id) ON DELETE CASCADE,
  asset_id bigint REFERENCES asset(asset_id) ON DELETE CASCADE,
  added_at timestamptz DEFAULT now(),
  PRIMARY KEY (coll_id, asset_id)
);

-- 任务队列(worker 用 SELECT ... FOR UPDATE SKIP LOCKED 领任务)
CREATE TABLE IF NOT EXISTS task (
  task_id    bigserial PRIMARY KEY,
  kind       text NOT NULL DEFAULT 'ingest',
  asset_id   bigint REFERENCES asset(asset_id) ON DELETE CASCADE,
  status     text NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|dead
  attempts   int NOT NULL DEFAULT 0,
  last_error text,
  run_after  timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now(),
  UNIQUE (kind, asset_id)
);
CREATE INDEX IF NOT EXISTS task_pick ON task(run_after) WHERE status='pending';
