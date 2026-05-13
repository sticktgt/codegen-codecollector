from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.onboarding.knowledge_builder import KnowledgeBuilder
from codecollector.onboarding.knowledge_enrichment_service import KnowledgeEnrichmentService
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService

LOGGER = get_logger(__name__)

class OnboardingFailedError(RuntimeError):
    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}

    def to_dict(self) -> dict[str, Any]:
        return {'error_code': 'onboarding_failed', **self.payload}

    def attach_rollback(self, rollback: dict[str, Any] | None) -> None:
        if rollback is not None:
            self.payload['rollback'] = rollback


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
    onboarding_root: str = ''
    architecture_doc_path: str = ''
    knowledge_enrichment_status: str = 'skipped'
    knowledge_enrichment_warnings: list[str] = field(default_factory=list)
    knowledge_enrichment_unmatched_mentions: list[dict[str, Any]] = field(default_factory=list)
    knowledge_enrichment_llm_usage: dict[str, Any] = field(default_factory=dict)
    knowledge_enrichment_prompt_budget: dict[str, Any] = field(default_factory=dict)
    knowledge_enrichment_trace_path: str = ''


class OnboardingService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.projects = ProjectService(self.tool_root, config)

    def onboard_project(
        self,
        project_id: str | None = None,
        *,
        input_root: Path | None = None,
        project_name: str | None = None,
        full_rebuild: bool = False,
        use_architecture_enrichment: bool = True,
    ) -> ProjectOnboardingResult:
        project = None
        created_from_input_root = input_root is not None
        try:
            project, onboarding_root, architecture_doc_path = self._resolve_project(
                project_id,
                input_root,
                project_name,
            )
            if created_from_input_root:
                project = self.projects.mark_status(project.project_id, 'onboarding')

            services = ProjectServices(Path(project.project_root), tool_root=self.tool_root, config=self.config)
            build_report = services.build_index(full_rebuild=full_rebuild)
            symbols = services.store.list_symbols(str(services.project_root))
            symbol_count = len([item for item in symbols if item.kind != 'module'])
            module_count = len([item for item in symbols if item.kind == 'module'])

            enrichment_payload: dict[str, Any] | None = None
            enrichment_status = 'skipped'
            enrichment_warnings: list[str] = []
            enrichment_unmatched: list[dict[str, Any]] = []
            enrichment_usage: dict[str, Any] = {}
            enrichment_budget: dict[str, Any] = {}
            enrichment_trace_path = ''
            if use_architecture_enrichment and architecture_doc_path is not None:
                enrichment_result = KnowledgeEnrichmentService(self.tool_root, self.config).enrich_from_architect_file(
                    architect_path=architecture_doc_path,
                    project_root=services.project_root,
                    symbols=symbols,
                )
                enrichment_status = enrichment_result.status
                enrichment_warnings = list(enrichment_result.warnings)
                enrichment_unmatched = list(enrichment_result.unmatched_mentions)
                enrichment_usage = dict(enrichment_result.llm_usage)
                enrichment_budget = dict(enrichment_result.prompt_budget)
                enrichment_trace_path = enrichment_result.trace_path
                enrichment_payload = dict(enrichment_result.payload) if enrichment_result.payload else None
                if created_from_input_root and enrichment_status != 'applied':
                    reason = '; '.join(enrichment_warnings) or f'status={enrichment_status}'
                    raise OnboardingFailedError(
                        f'Knowledge enrichment из архитектурного описания не выполнен: {reason}',
                        {
                            'project_id': project.project_id,
                            'project_root': project.project_root,
                            'onboarding_root': str(onboarding_root or ''),
                            'architecture_doc_path': str(architecture_doc_path or ''),
                            'knowledge_enrichment_status': enrichment_status,
                            'knowledge_enrichment_warnings': enrichment_warnings,
                            'knowledge_enrichment_unmatched_mentions': enrichment_unmatched,
                            'knowledge_enrichment_llm_usage': enrichment_usage,
                            'knowledge_enrichment_prompt_budget': enrichment_budget,
                            'knowledge_enrichment_trace_path': enrichment_trace_path,
                        },
                    )
                if enrichment_payload and enrichment_unmatched:
                    architecture = enrichment_payload.setdefault('architecture', {})
                    if isinstance(architecture, dict):
                        architecture['unmatched_mentions'] = enrichment_unmatched
            elif use_architecture_enrichment and created_from_input_root and architecture_doc_path is None:
                enrichment_status = 'skipped'
                names = ', '.join(self.config.onboarding_architecture_doc_names)
                message = f'Файл архитектурного описания не найден в onboarding root: ожидались {names}.'
                LOGGER.warning(message)
                enrichment_warnings = [message]
            elif not use_architecture_enrichment:
                enrichment_status = 'disabled'
                enrichment_warnings = ['Architecture enrichment отключен параметром вызова onboarding.']

            knowledge_report = KnowledgeBuilder(services.project_root, self.config).rebuild(symbols, enrichment=enrichment_payload)
            enrichment_warnings.extend(str(item) for item in knowledge_report.get('enrichment_warnings') or [])
            knowledge_updated_at = datetime.now(UTC).isoformat()
            status = 'ready'
            self.projects.update_onboarding_metadata(
                project.project_id,
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
                onboarding_root=str(onboarding_root or ''),
                architecture_doc_path=str(architecture_doc_path or ''),
                knowledge_enrichment_status=enrichment_status,
                knowledge_enrichment_warnings=self._dedupe(enrichment_warnings),
                knowledge_enrichment_unmatched_mentions=enrichment_unmatched,
                knowledge_enrichment_llm_usage=enrichment_usage,
                knowledge_enrichment_prompt_budget=enrichment_budget,
                knowledge_enrichment_trace_path=enrichment_trace_path,
            )
        except Exception as exc:
            rollback_result = None
            if created_from_input_root and project is not None:
                rollback_result = self._rollback_input_root_onboarding(project.project_id)
            if isinstance(exc, OnboardingFailedError):
                exc.attach_rollback(rollback_result)
            raise

    def _resolve_project(
        self,
        project_id: str | None,
        input_root: Path | None,
        project_name: str | None,
    ):
        if input_root is None:
            if not project_id:
                raise ValueError('Для onboarding нужно передать --project-id или --input-root.')
            project = self.projects.get_project(project_id)
            return project, None, None

        if project_id:
            raise ValueError('Параметры --project-id и --input-root нельзя использовать вместе: --input-root регистрирует новый проект.')
        onboarding_root = input_root.resolve()
        if not onboarding_root.exists() or not onboarding_root.is_dir():
            raise ValueError(f'Папка onboarding не найдена: {onboarding_root}')
        source_root = onboarding_root / 'src'
        if not source_root.exists() or not source_root.is_dir():
            raise ValueError(f'В папке onboarding должна быть директория src с кодом: {source_root}')
        architecture_doc_path = self._find_architecture_doc(onboarding_root)
        if architecture_doc_path is None:
            LOGGER.warning(
                'Architecture document not found in onboarding root: root=%s expected_names=%s',
                onboarding_root,
                self.config.onboarding_architecture_doc_names,
            )
        project = self.projects.register_project(
            project_name=project_name or onboarding_root.name,
            project_root=source_root,
            languages=['python'],
            verification_commands=[],
            index_excludes=[],
            reference_library_paths=[],
        )
        LOGGER.info(
            'Registered project from onboarding input root: project_id=%s project_root=%s architecture_doc=%s',
            project.project_id,
            project.project_root,
            architecture_doc_path,
        )
        return project, onboarding_root, architecture_doc_path

    def _find_architecture_doc(self, onboarding_root: Path) -> Path | None:
        for raw_name in self.config.onboarding_architecture_doc_names:
            name = str(raw_name).strip()
            if not name:
                continue
            candidate = (onboarding_root / name).resolve()
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def _rollback_input_root_onboarding(self, project_id: str) -> dict[str, Any] | None:
        try:
            result = self.projects.delete_project(project_id)
            LOGGER.info('Rolled back onboarding project registration and indexed data: %s', result)
            return result
        except Exception as cleanup_exc:
            LOGGER.warning('Failed to rollback onboarding project %s: %s', project_id, cleanup_exc)
            return {'project_id': project_id, 'deleted': False, 'cleanup': {}, 'error': str(cleanup_exc)}

    def _dedupe(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
        return result
