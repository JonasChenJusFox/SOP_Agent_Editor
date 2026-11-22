-- Local knowledge cache, SQLite schema version 1.
-- KnowledgeIndex initializes this automatically and sets PRAGMA user_version.
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    commit_sha TEXT,
    snapshot_hash TEXT,
    status TEXT NOT NULL,
    diagnostic TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    passage_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY(repository, path),
    FOREIGN KEY(repository) REFERENCES sources(name) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS passages (
    internal_id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    excerpt TEXT NOT NULL,
    FOREIGN KEY(repository, path) REFERENCES files(repository, path) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS passages_source ON passages(repository, path);
CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
    internal_id UNINDEXED,
    body,
    tokenize='unicode61'
);
