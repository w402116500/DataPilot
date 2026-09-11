CREATE TABLE IF NOT EXISTS graph_builds (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL,
    rebuild_key TEXT NOT NULL,
    schema_revision INTEGER NOT NULL CHECK (schema_revision >= 0),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    graph_version TEXT NOT NULL UNIQUE,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    origin_kind TEXT NOT NULL DEFAULT 'automated' CHECK (origin_kind IN ('automated', 'manual', 'restore')),
    publication_state TEXT NOT NULL DEFAULT 'published' CHECK (publication_state IN ('candidate', 'published')),
    base_graph_version TEXT,
    UNIQUE (datasource_id, schema_revision, attempt_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_builds_datasource_running
    ON graph_builds (datasource_id)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS ix_graph_builds_lookup
    ON graph_builds (datasource_id, schema_revision, status, attempt_no DESC);

CREATE TABLE IF NOT EXISTS semantic_drafts (
    datasource_id TEXT PRIMARY KEY,
    base_graph_version TEXT NOT NULL,
    schema_revision INTEGER NOT NULL CHECK (schema_revision >= 0),
    draft_revision INTEGER NOT NULL CHECK (draft_revision >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'published', 'discarded')),
    changes_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_change_log (
    id TEXT PRIMARY KEY,
    datasource_id TEXT NOT NULL,
    draft_revision INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_publish_log (
    datasource_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (datasource_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS semantic_rebuild_conflicts (
    build_id TEXT NOT NULL,
    object_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (build_id, object_key),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS semantic_version_operations (
    datasource_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('restore', 'resolve_candidate')),
    source_graph_version TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(datasource_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS datasource_graph_heads (
    datasource_id TEXT PRIMARY KEY,
    current_graph_version TEXT NOT NULL,
    current_build_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (current_build_id) REFERENCES graph_builds (id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT NOT NULL,
    datasource_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    source_id TEXT,
    properties_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (build_id, id),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_nodes_version_type_name
    ON nodes (datasource_id, graph_version, type, name);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT NOT NULL,
    datasource_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    type TEXT NOT NULL,
    confidence REAL CHECK (confidence >= 0 AND confidence <= 1),
    evidence_json TEXT,
    properties_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (build_id, id),
    UNIQUE (build_id, source_id, target_id, type),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE,
    FOREIGN KEY (build_id, source_id) REFERENCES nodes (build_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (build_id, target_id) REFERENCES nodes (build_id, id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_edges_version_type
    ON edges (datasource_id, graph_version, type);

CREATE TABLE IF NOT EXISTS column_profiles (
    id TEXT NOT NULL,
    datasource_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    column_id TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (build_id, id),
    UNIQUE (build_id, column_id),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE,
    FOREIGN KEY (build_id, column_id) REFERENCES nodes (build_id, id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS node_embeddings (
    datasource_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    node_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_vector TEXT NOT NULL,
    searchable_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (build_id, node_id, embedding_model),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE,
    FOREIGN KEY (build_id, node_id) REFERENCES nodes (build_id, id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS pending_edges (
    id TEXT NOT NULL,
    datasource_id TEXT NOT NULL,
    build_id TEXT NOT NULL,
    graph_version TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    type TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    missing_endpoints_json TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (build_id, id),
    FOREIGN KEY (build_id) REFERENCES graph_builds (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_pending_edges_version_type
    ON pending_edges (datasource_id, graph_version, type);
