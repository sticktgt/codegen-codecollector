from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.domain.models import ReferenceArtifact
from codecollector.indexing.base import IndexStore
from codecollector.logger import get_logger
from codecollector.vector_search.service import DescriptionVectorSearchService

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class ReferenceDescriptor:
    artifact_id: str
    language: str
    artifact_type: str
    usage_mode: str
    source_path: str
    title: str
    description: str
    keywords: list[str]
    snippet_start_line: int | None = None
    snippet_end_line: int | None = None


class ReferenceLibraryService:
    def __init__(
        self,
        tool_root: Path,
        store: IndexStore,
        vector_search: DescriptionVectorSearchService | None,
        config: AppConfig,
    ) -> None:
        self.tool_root = tool_root.resolve()
        self.store = store
        self.vector_search = vector_search
        self.config = config
        self.library_root = (self.tool_root / config.reference_library_dir).resolve()
        self.manifest_path = self.library_root / 'library.yaml'
        self.library_key = str(self.library_root)

    def sync_documents(self, force: bool = False) -> dict[str, int | bool]:
        descriptors = self._load_manifest()
        documents = [self._to_search_document(item) for item in descriptors]
        existing = self.store.list_search_documents(self.library_key)
        search_sync_ms = 0
        vector_sync_ms = 0
        if existing and not force:
            LOGGER.info('Reference library already indexed: skip sync')
            return {
                'reference_documents_count': len(existing),
                'reference_documents_changed': False,
                'reference_sync_ms': search_sync_ms,
                'reference_vector_sync_ms': vector_sync_ms,
            }
        import time
        started = time.perf_counter()
        self.store.replace_search_documents(self.library_key, documents)
        search_sync_ms = int((time.perf_counter() - started) * 1000)
        if self.vector_search is not None:
            started = time.perf_counter()
            self.vector_search.sync_documents(documents, project_key=self.library_key)
            vector_sync_ms = int((time.perf_counter() - started) * 1000)
        LOGGER.info('Reference library synced: %s documents', len(documents))
        return {
            'reference_documents_count': len(documents),
            'reference_documents_changed': True,
            'reference_sync_ms': search_sync_ms,
            'reference_vector_sync_ms': vector_sync_ms,
        }

    def retrieve_for_change_request(self, query: str, target_summary: str, limit: int) -> list[ReferenceArtifact]:
        descriptors = {item.artifact_id: item for item in self._load_manifest()}
        docs = self.store.list_search_documents(self.library_key)
        vector_scores = self.vector_search.search(
            f'{query}\n{target_summary}'.strip(),
            limit=max(limit * 3, 6),
            project_key=self.library_key,
        ) if self.vector_search is not None else {}

        scored: list[tuple[float, str, list[str]]] = []
        query_text = f'{query} {target_summary}'.lower()
        for item in docs:
            artifact_id = item['qualname']
            descriptor = descriptors.get(artifact_id)
            if descriptor is None:
                continue
            score = 0.0
            reasons: list[str] = []
            haystack = f"{item.get('title', '')}\n{item.get('text', '')}".lower()
            for term in self._terms(query_text):
                if term in haystack:
                    score += 1.5
                    reasons.append(f'термин «{term}» найден в описании reference-артефакта')
            vscore = vector_scores.get(artifact_id, 0.0)
            if vscore > 0:
                score += vscore * self.config.reference_vector_weight
                reasons.insert(0, f'векторное сходство reference-артефакта: {vscore:.2f}')
            if descriptor.artifact_type == 'reusable_component':
                score += 0.3
            if descriptor.usage_mode == 'reuse':
                score += 0.2
            if score > 0:
                scored.append((score, artifact_id, reasons[:5]))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected: list[ReferenceArtifact] = []
        for score, artifact_id, reasons in scored[:limit]:
            descriptor = descriptors[artifact_id]
            selected.append(self._materialize(descriptor, score, '; '.join(reasons)))
        return selected

    def _materialize(self, descriptor: ReferenceDescriptor, score: float, why_selected: str) -> ReferenceArtifact:
        source_path = (self.library_root / descriptor.source_path).resolve()
        content = source_path.read_text(encoding='utf-8')
        lines = content.splitlines()
        content_mode = 'full_file'
        selected_span: dict[str, int] | None = None
        if len(lines) > self.config.reference_full_file_max_lines:
            start = descriptor.snippet_start_line or 1
            end = descriptor.snippet_end_line or min(len(lines), self.config.reference_full_file_max_lines)
            snippet = '\n'.join(lines[start - 1:end])
            content = snippet
            content_mode = 'snippet'
            selected_span = {'start_line': start, 'end_line': end}
        return ReferenceArtifact(
            artifact_id=descriptor.artifact_id,
            title=descriptor.title,
            description=descriptor.description,
            artifact_type=descriptor.artifact_type,
            usage_mode=descriptor.usage_mode,
            language=descriptor.language,
            relevance_score=round(score, 2),
            why_selected=why_selected,
            content_mode=content_mode,
            source_path=str(Path('reference_library') / descriptor.source_path),
            content=content,
            selected_span=selected_span,
        )

    def _to_search_document(self, descriptor: ReferenceDescriptor) -> dict[str, str]:
        source_path = (self.library_root / descriptor.source_path).resolve()
        file_text = source_path.read_text(encoding='utf-8')
        parts = [descriptor.title, descriptor.description, ' '.join(descriptor.keywords), self._extract_docstring(file_text)]
        return {
            'doc_id': f'reference::{descriptor.artifact_id}',
            'qualname': descriptor.artifact_id,
            'file_path': descriptor.source_path,
            'source_kind': 'reference_artifact',
            'title': descriptor.title,
            'text': '\n'.join(part.strip() for part in parts if part and part.strip()),
        }

    def _load_manifest(self) -> list[ReferenceDescriptor]:
        payload: dict[str, Any] = yaml.safe_load(self.manifest_path.read_text(encoding='utf-8')) or {}
        items = []
        for raw in payload.get('artifacts', []) or []:
            items.append(ReferenceDescriptor(
                artifact_id=str(raw['artifact_id']),
                language=str(raw.get('language', 'python')),
                artifact_type=str(raw.get('artifact_type', 'template')),
                usage_mode=str(raw.get('usage_mode', 'adapt')),
                source_path=str(raw['source_path']),
                title=str(raw.get('title', raw['artifact_id'])),
                description=str(raw.get('description', '')),
                keywords=[str(item) for item in raw.get('keywords', []) or []],
                snippet_start_line=raw.get('snippet_start_line'),
                snippet_end_line=raw.get('snippet_end_line'),
            ))
        return items

    def _extract_docstring(self, file_text: str) -> str:
        if file_text.startswith('"""'):
            parts = file_text.split('"""', 2)
            if len(parts) >= 3:
                return parts[1].strip()
        return ''

    def _terms(self, text: str) -> list[str]:
        import re
        return [item for item in re.findall(r"[a-zA-Zа-яА-ЯёЁ_]{3,}", text) if item not in {'изменить', 'сделать', 'использовать'}]
