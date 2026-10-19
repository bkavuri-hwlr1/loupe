"""The code graph extends only the rebuildable knowledge migration plane."""

from llm_cli.storage.migrations import KNOWLEDGE_MIGRATIONS, Migration

GRAPH_MIGRATIONS = (
    *KNOWLEDGE_MIGRATIONS,
    Migration(
        4,
        "deterministic_code_graph",
        """
CREATE TABLE knowledge_graph_files (
    document_id TEXT PRIMARY KEY REFERENCES knowledge_documents(document_id)
        ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    parser TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(source_id,relative_path)
);
CREATE TABLE knowledge_graph_nodes (
    source_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id)
        ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    PRIMARY KEY(source_id,node_id)
);
CREATE INDEX knowledge_graph_nodes_document ON knowledge_graph_nodes(document_id);
CREATE TABLE knowledge_graph_aliases (
    source_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    node_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id)
        ON DELETE CASCADE,
    PRIMARY KEY(source_id,alias,node_id)
);
CREATE INDEX knowledge_graph_aliases_node ON knowledge_graph_aliases(source_id,node_id);
CREATE INDEX knowledge_graph_aliases_document ON knowledge_graph_aliases(document_id);
CREATE TABLE knowledge_graph_edges (
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES knowledge_documents(document_id)
        ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    generation_id TEXT NOT NULL,
    PRIMARY KEY(document_id,ordinal)
);
CREATE INDEX knowledge_graph_edges_source
    ON knowledge_graph_edges(source_id,source_node);
CREATE INDEX knowledge_graph_edges_target
    ON knowledge_graph_edges(source_id,target_node);
""",
    ),
    Migration(
        5,
        "compact_code_graph_keys",
        """
-- Graph facts are rebuildable. Preserve lexical documents, generations and
-- cursors; invalidate graph discovery until an explicit complete rebuild.
DROP TABLE knowledge_graph_edges;
DROP TABLE knowledge_graph_aliases;
DROP TABLE knowledge_graph_nodes;
DROP TABLE knowledge_graph_files;
DELETE FROM store_metadata WHERE key LIKE 'graph_version:%';
CREATE TABLE knowledge_graph_sources (
    source_key INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE REFERENCES knowledge_sources(source_id)
        ON DELETE CASCADE
);
CREATE TABLE knowledge_graph_files (
    file_key INTEGER PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE REFERENCES knowledge_documents(document_id)
        ON DELETE CASCADE,
    source_key INTEGER NOT NULL REFERENCES knowledge_graph_sources(source_key)
        ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    parser TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    error_code TEXT,
    UNIQUE(source_key,relative_path)
);
CREATE TABLE knowledge_graph_nodes (
    source_key INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES knowledge_graph_files(file_key)
        ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    PRIMARY KEY(source_key,node_id)
);
CREATE INDEX knowledge_graph_nodes_document ON knowledge_graph_nodes(document_id);
CREATE TABLE knowledge_graph_aliases (
    source_key INTEGER NOT NULL,
    alias TEXT NOT NULL,
    node_id TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES knowledge_graph_files(file_key)
        ON DELETE CASCADE,
    PRIMARY KEY(source_key,alias,node_id)
);
CREATE INDEX knowledge_graph_aliases_node
    ON knowledge_graph_aliases(source_key,node_id);
CREATE INDEX knowledge_graph_aliases_document ON knowledge_graph_aliases(document_id);
CREATE TABLE knowledge_graph_edges (
    source_key INTEGER NOT NULL,
    document_id INTEGER NOT NULL REFERENCES knowledge_graph_files(file_key)
        ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    generation_id TEXT NOT NULL,
    PRIMARY KEY(document_id,ordinal)
);
CREATE INDEX knowledge_graph_edges_source
    ON knowledge_graph_edges(source_key,source_node);
CREATE INDEX knowledge_graph_edges_target
    ON knowledge_graph_edges(source_key,target_node);
""",
    ),
)
