-- oído 初始 schema（OIDO-EXEC §3.1）
-- SQLite：元数据 + 任务状态 + outbox + tombstone。
-- 大数组（PCM/频谱/embedding）一律走对象存储，不进库。
-- 注：journal_mode=WAL 由 open_db 设置（R04 后迁移语句须可在事务内执行）。

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_assets (
  audio_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'private',
  source_kind TEXT NOT NULL CHECK (source_kind IN ('user_upload', 'assistant_tts', 'external_import')),
  original_object_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  media_mime TEXT,
  sample_rate_hz INTEGER,
  channels INTEGER,
  lifecycle TEXT NOT NULL DEFAULT 'active'
    CHECK (lifecycle IN ('active', 'deletion_requested', 'tombstoned', 'purged')),
  retention TEXT NOT NULL DEFAULT 'standard',  -- standard | temporary
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS audio_origins (
  origin_id TEXT PRIMARY KEY,
  audio_id TEXT NOT NULL REFERENCES audio_assets(audio_id),
  origin_kind TEXT NOT NULL,               -- siren_voice_message | direct_upload | external
  external_id TEXT,                        -- voice_message_id / event_id 等
  voice_profile_id TEXT,                   -- assistant_tts 专用（D02）
  voice_profile_version TEXT,              -- assistant_tts 专用
  provider_model_revision TEXT,
  UNIQUE (origin_kind, external_id)
);
CREATE INDEX IF NOT EXISTS idx_origins_audio ON audio_origins(audio_id);

CREATE TABLE IF NOT EXISTS audio_variants (
  variant_id TEXT PRIMARY KEY,
  audio_id TEXT NOT NULL REFERENCES audio_assets(audio_id),
  parent_variant_id TEXT REFERENCES audio_variants(variant_id),
  transform_kind TEXT NOT NULL,            -- decode_main | resample | downmix | crop ...
  sample_rate_hz INTEGER NOT NULL,
  channels INTEGER NOT NULL,
  sample_count INTEGER NOT NULL,
  timebase_id TEXT NOT NULL,
  transform_params TEXT NOT NULL,          -- JSON：谱系（EXEC §3.4）
  object_key TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_variants_audio ON audio_variants(audio_id);

CREATE TABLE IF NOT EXISTS analysis_runs (
  analysis_id TEXT PRIMARY KEY,
  audio_id TEXT NOT NULL REFERENCES audio_assets(audio_id),
  profile TEXT NOT NULL,                   -- full_evidence | conversation_fast | inspect_range
  requested_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
  revision INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT,
  request_digest TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  fencing_token INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem
  ON analysis_runs(requested_by, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_audio ON analysis_runs(audio_id);

CREATE TABLE IF NOT EXISTS analysis_stages (
  stage_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
  capability TEXT NOT NULL,                -- C01..C16 之外的 stage 名，如 dsp_f0
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'skipped')),
  reason_code TEXT,
  detail TEXT,
  UNIQUE (analysis_id, capability)
);

CREATE TABLE IF NOT EXISTS evidence_items (
  evidence_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
  revision INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('measurement', 'model_detection', 'model_description')),
  feature TEXT NOT NULL,
  payload TEXT NOT NULL,                   -- JSON：完整合同（EXEC §3.5）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence_items(analysis_id, revision);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  analysis_id TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
  kind TEXT NOT NULL,                      -- png_overview | png_waveform | ...
  object_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  media_type TEXT NOT NULL,
  render_params TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  key TEXT NOT NULL,                       -- 幂等键（ARCH §10.3）
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON events_outbox(delivered_at);

CREATE TABLE IF NOT EXISTS idempotency_records (
  principal TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  response_ref TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (principal, endpoint, key)
);

CREATE TABLE IF NOT EXISTS deletion_tombstones (
  audio_id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  applied_backups TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS acoustic_baselines (
  profile_id TEXT NOT NULL,                -- qiaosheng | jiaming:<voice_profile_version>
  version INTEGER NOT NULL,
  stats_json TEXT NOT NULL,                -- 只含批准指标；无绝对音量/声纹（D06）
  sample_count INTEGER NOT NULL,
  sample_asset_ids TEXT NOT NULL,          -- 样本集合版本
  created_at TEXT NOT NULL,
  cleared_at TEXT,
  PRIMARY KEY (profile_id, version)
);

CREATE TABLE IF NOT EXISTS access_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  principal TEXT NOT NULL,
  action TEXT NOT NULL,
  object_id TEXT NOT NULL,
  decision TEXT NOT NULL,                  -- allow | deny
  at TEXT NOT NULL
  -- 不记录：原声正文、token、签名地址（ARCH §6.1）
);
