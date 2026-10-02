-- 审计 F08：分析请求的范围/预算必须持久化（哈希不可反推，durable worker 需要原请求）
ALTER TABLE analysis_runs ADD COLUMN request_json TEXT;
