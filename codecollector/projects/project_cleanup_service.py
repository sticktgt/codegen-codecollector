from __future__ import annotations

from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.indexing.storage_factory import create_index_store
from codecollector.logger import get_logger
from codecollector.vector_search.service import DescriptionVectorSearchService

LOGGER = get_logger(__name__)


class ProjectCleanupService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config

    def cleanup_project_data(self, project_root: Path | str) -> dict[str, Any]:
        resolved_root = Path(project_root).resolve()
        project_key = str(resolved_root)
        result: dict[str, Any] = {
            'project_root': project_key,
            'graph_deleted': {},
            'vector_deleted': 0,
            'warnings': [],
        }

        try:
            store = create_index_store(resolved_root, self.config)
            if hasattr(store, 'delete_project'):
                result['graph_deleted'] = store.delete_project(project_key)
        except Exception as exc:
            message = f'Не удалось очистить graph index для project_root={project_key}: {exc}'
            LOGGER.warning(message)
            result['warnings'].append(message)

        try:
            store = create_index_store(resolved_root, self.config)
            vector_service = DescriptionVectorSearchService(resolved_root, store, self.config)
            result['vector_deleted'] = vector_service.delete_project_documents(project_key)
        except Exception as exc:
            message = f'Не удалось очистить vector index для project_root={project_key}: {exc}'
            LOGGER.warning(message)
            result['warnings'].append(message)

        self._append_index_cleanup_mismatch_warning(result)

        LOGGER.info(
            'project data cleanup finished: project_root=%s graph_deleted=%s vector_deleted=%s warnings=%s',
            project_key,
            result.get('graph_deleted'),
            result.get('vector_deleted'),
            len(result.get('warnings') or []),
        )
        return result


    def _append_index_cleanup_mismatch_warning(self, result: dict[str, Any]) -> None:
        graph_deleted = result.get('graph_deleted')
        if not isinstance(graph_deleted, dict) or 'cc_search_documents' not in graph_deleted:
            return
        graph_search_deleted = graph_deleted.get('cc_search_documents')
        vector_deleted = result.get('vector_deleted')
        if not isinstance(graph_search_deleted, int) or not isinstance(vector_deleted, int):
            return
        if graph_search_deleted == vector_deleted:
            return
        project_root = result.get('project_root', '')
        message = (
            'Количество удаленных search documents и vector documents различается: '
            f'project_root={project_root} '
            f'cc_search_documents={graph_search_deleted} '
            f'vector_deleted={vector_deleted}. '
            'Это может означать уже существовавший рассинхрон индекса.'
        )
        LOGGER.warning(message)
        warnings = result.setdefault('warnings', [])
        if isinstance(warnings, list):
            warnings.append(message)

