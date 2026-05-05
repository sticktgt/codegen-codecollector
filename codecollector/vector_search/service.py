from __future__ import annotations

from pathlib import Path

from codecollector.config import AppConfig
from codecollector.indexing.base import IndexStore
from codecollector.logger import get_logger
from codecollector.vector_search.ollama_embeddings import snapshot_embedding_usage
from codecollector.vector_search.pgvector_service import PGVectorDescriptionSearchService

LOGGER = get_logger(__name__)


class DescriptionVectorSearchService:
    def __init__(self, project_root: Path, store: IndexStore, config: AppConfig) -> None:
        self.project_root = project_root.resolve()
        self.project_key = str(self.project_root)
        self.store = store
        self.config = config
        self.backend = PGVectorDescriptionSearchService(self.project_root, config)

    def sync_documents(self, documents: list[dict[str, str]], project_key: str | None = None) -> None:
        self.backend.sync_documents(documents, project_key=project_key or self.project_key)

    def search(self, query: str, limit: int = 10, project_key: str | None = None) -> dict[str, float]:
        return self.backend.search(query, limit=limit, project_key=project_key or self.project_key)

    def search_many(
        self,
        queries: list[str],
        limit: int = 10,
        project_key: str | None = None,
    ) -> dict[str, dict[str, float]]:
        return self.backend.search_many(
            queries,
            limit=limit,
            project_key=project_key or self.project_key,
        )    
    
    def get_embedding_usage_summary(self) -> dict[str, object]:
        return snapshot_embedding_usage()
