from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codecollector.config import AppConfig
from codecollector.onboarding.knowledge_builder import KnowledgeBuilder
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService


@dataclass(slots=True)
class ProjectOnboardingResult:
    project_id: str
    project_root: str
    status: str
    indexed_files: int
    unchanged_files: int
    deleted_files: int
    search_documents_count: int
    reference_documents_count: int
    graph_indexing_ms: int
    search_documents_sync_ms: int
    vector_index_sync_ms: int
    reference_sync_ms: int
    reference_vector_sync_ms: int
    symbol_count: int
    module_count: int
    knowledge_path: str
    knowledge_updated_at: str
    knowledge_updated: bool


class OnboardingService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.projects = ProjectService(self.tool_root, config)

    def onboard_project(self, project_id: str, *, full_rebuild: bool = False) -> ProjectOnboardingResult:
        project = self.projects.get_project(project_id)
        services = ProjectServices(Path(project.project_root), tool_root=self.tool_root, config=self.config)
        build_report = services.build_index(full_rebuild=full_rebuild)
        symbols = services.store.list_symbols(str(services.project_root))
        symbol_count = len([item for item in symbols if item.kind != 'module'])
        module_count = len([item for item in symbols if item.kind == 'module'])
        knowledge_report = KnowledgeBuilder(services.project_root, self.config).rebuild(symbols)
        knowledge_updated_at = datetime.now(UTC).isoformat()
        status = 'ready'
        self.projects.update_onboarding_metadata(
            project_id,
            status=status,
            knowledge_path=str(knowledge_report['knowledge_path']),
            knowledge_updated_at=knowledge_updated_at,
        )
        return ProjectOnboardingResult(
            project_id=project.project_id,
            project_root=project.project_root,
            status=status,
            indexed_files=build_report.indexed_files,
            unchanged_files=build_report.unchanged_files,
            deleted_files=build_report.deleted_files,
            search_documents_count=build_report.search_documents_count,
            reference_documents_count=build_report.reference_documents_count,
            graph_indexing_ms=build_report.graph_indexing_ms,
            search_documents_sync_ms=build_report.search_documents_sync_ms,
            vector_index_sync_ms=build_report.vector_index_sync_ms,
            reference_sync_ms=build_report.reference_sync_ms,
            reference_vector_sync_ms=build_report.reference_vector_sync_ms,
            symbol_count=symbol_count,
            module_count=module_count,
            knowledge_path=str(knowledge_report['knowledge_path']),
            knowledge_updated_at=knowledge_updated_at,
            knowledge_updated=bool(knowledge_report['knowledge_updated']),
        )
