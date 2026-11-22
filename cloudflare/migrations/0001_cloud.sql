-- Cloudflare D1; no manuscript data is included in migrations.
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('queued','running','complete','partial','failed')),
  stage TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('demo','configured')),
  input_json TEXT NOT NULL,
  config_json TEXT NOT NULL,
  source_id TEXT,
  source_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_active ON jobs(state, updated_at);
CREATE TABLE IF NOT EXISTS files (
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  chunk INTEGER NOT NULL,
  content TEXT NOT NULL,
  PRIMARY KEY (job_id, path, chunk)
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS login_limits (
  client_hash TEXT NOT NULL,
  window INTEGER NOT NULL,
  attempts INTEGER NOT NULL,
  PRIMARY KEY (client_hash, window)
);
