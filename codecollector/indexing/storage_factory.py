from __future__ import annotations

from pathlib import Path

from codecollector.config import AppConfig
from codecollector.indexing.storage_postgres import PostgresIndexStore


def create_index_store(project_root: Path, config: AppConfig):
    _ = project_root
    return PostgresIndexStore(config.postgres_graph_connection, schema=config.postgres_schema)
