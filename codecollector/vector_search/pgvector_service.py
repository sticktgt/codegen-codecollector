from __future__ import annotations

import hashlib
import re
from pathlib import Path

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.vector_search.ollama_embeddings import OllamaEmbeddings

LOGGER = get_logger(__name__)


def _slug(value: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9]+', '_', value).strip('_').lower()
    return cleaned[:40] or 'project'


class PGVectorDescriptionSearchService:
    def __init__(self, project_root: Path, config: AppConfig) -> None:
        self.project_root = project_root.resolve()
        self.project_key = str(self.project_root)
        self.config = config
        self.collection_name = f"{config.postgres_collection_prefix}_{_slug(self.project_root.name)}_{hashlib.sha1(self.project_key.encode()).hexdigest()[:8]}"
        self.embedding = OllamaEmbeddings(
            base_url=config.embedding_ollama_base_url,
            model=config.embedding_ollama_model,
            timeout_sec=config.embedding_timeout_sec,
        )

    def _connect(self):
        import psycopg
        return psycopg.connect(self.config.postgres_graph_connection)

    def _vector_store(self):
        from langchain_postgres import PGVector
        return PGVector(
            embeddings=self.embedding,
            collection_name=self.collection_name,
            connection=self.config.postgres_vector_connection,
            use_jsonb=True,
            pre_delete_collection=False,
        )

    def sync_documents(self, documents: list[dict[str, str]]) -> None:
        store = self._vector_store()
        self._delete_existing_project_docs()
        if not documents:
            return
        try:
            from langchain_core.documents import Document
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("langchain-core is required for PGVector search") from exc

        docs = [
            Document(
                page_content=item['text'],
                metadata={
                    'project_root': self.project_key,
                    'qualname': item['qualname'],
                    'file_path': item['file_path'],
                    'source_kind': item['source_kind'],
                    'title': item['title'],
                    'doc_id': item['doc_id'],
                },
                id=item['doc_id'],
            )
            for item in documents
        ]
        store.add_documents(docs, ids=[item['doc_id'] for item in documents])

    def _delete_existing_project_docs(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM langchain_pg_embedding WHERE cmetadata->>'project_root' = %s",
                (self.project_key,),
            )
            conn.commit()

    def search(self, query: str, limit: int = 10) -> dict[str, float]:
        store = self._vector_store()
        results = store.similarity_search_with_score(query, k=limit * 3)
        scores: dict[str, float] = {}
        for doc, score in results:
            metadata = doc.metadata or {}
            if metadata.get('project_root') != self.project_key:
                continue
            qualname = str(metadata.get('qualname', ''))
            if not qualname:
                continue
            # Similarity search with score often returns distance-like values; convert to a bounded similarity.
            similarity = 1.0 / (1.0 + float(score))
            if similarity > scores.get(qualname, 0.0):
                scores[qualname] = similarity
        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
        return dict(ranked)
