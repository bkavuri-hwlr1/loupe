"""Local SQLite persistence primitives."""

from llm_cli.storage.connection import (
    connect_control,
    connect_database,
    connect_knowledge,
    connect_vectors,
    immediate_transaction,
)
from llm_cli.storage.control import ControlStore
from llm_cli.storage.migrations import (
    CONTROL_MIGRATIONS,
    KNOWLEDGE_MIGRATIONS,
    VECTOR_MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    current_schema_version,
)

__all__ = [
    "CONTROL_MIGRATIONS",
    "KNOWLEDGE_MIGRATIONS",
    "VECTOR_MIGRATIONS",
    "ControlStore",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "connect_control",
    "connect_database",
    "connect_knowledge",
    "connect_vectors",
    "current_schema_version",
    "immediate_transaction",
]
