-- Keep existing reviews and file storage; identify full-revision child jobs.
ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'review'
  CHECK (kind IN ('review','revision'));
CREATE INDEX IF NOT EXISTS jobs_parent_kind ON jobs(source_id, kind);
