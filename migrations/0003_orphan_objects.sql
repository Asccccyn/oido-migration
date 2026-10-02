-- T02：补偿失败的孤儿对象登记（带归属、可恢复、可重试）
-- 场景：register_upload 写文件成功、元数据失败、补偿 unlink 又失败——
-- 此前被静默吞掉，字节脱离一切索引与删除链路。本表让每个孤儿有据可查、可清扫。
CREATE TABLE IF NOT EXISTS orphan_objects (
  orphan_id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_key TEXT NOT NULL UNIQUE,
  audio_id TEXT NOT NULL,             -- 本次上传拟用的 audio_id（该资产行未建成）
  owner TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orphans_owner ON orphan_objects(owner, resolved_at);
