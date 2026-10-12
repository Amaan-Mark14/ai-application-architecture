-- Local preparation cache schema v1; unknown versions are rejected, never overwritten.
CREATE TABLE cache_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    configuration TEXT NOT NULL,
    generation INTEGER NOT NULL
);
CREATE TABLE documents (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    document_json TEXT NOT NULL,
    chunks_json TEXT NOT NULL
);
CREATE TABLE embeddings (
    content_hash TEXT PRIMARY KEY,
    vector_json TEXT NOT NULL
);
PRAGMA user_version = 1;
