-- 0004：来源明确的转写正本（联合施工 04/05：复用来源明确 ASR，不把 TTS 脚本当发音）。
-- est 受信导入时随原声一次性带入；对齐用，不作为"模型已听过声音"的证明。
CREATE TABLE IF NOT EXISTS audio_transcripts (
  audio_id TEXT PRIMARY KEY REFERENCES audio_assets(audio_id),
  text TEXT NOT NULL,                      -- 转写正文（非空；空转写不入行）
  source TEXT NOT NULL,                    -- siren:transcribe | manual_revision | ...
  source_revision TEXT,                    -- 来源版本（est asset revision / 修订号）
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_audio ON audio_transcripts(audio_id);
