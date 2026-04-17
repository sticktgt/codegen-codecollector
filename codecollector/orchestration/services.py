from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from codecollector.config import AppConfig, load_config
from codecollector.context.service import ContextService
from codecollector.domain.models import ApplyResult, ChangeRequest, ContextPack, PatchArtifact, PipelineRunResult, SearchCandidate
from codecollector.indexing.builder import BuildReport, PythonIndexBuilder
from codecollector.indexing.storage_factory import create_index_store
from codecollector.logger import get_logger
from codecollector.orchestration.pipeline_service import PipelineService
from codecollector.orchestration.run_artifacts import RunArtifactsManager
from codecollector.overlays.service import OverlayService
from codecollector.reference_library.service import ReferenceLibraryService
from codecollector.patching.apply_service import ApplyService
from codecollector.search.service import SearchService
from codecollector.vector_search.service import DescriptionVectorSearchService
from codecollector.validation.service import ValidationService
from codecollector.vector_search.ollama_embeddings import reset_embedding_usage

LOGGER = get_logger(__name__)


class ProjectServices:
    def __init__(self, project_root: Path, tool_root: Path | None = None, config: AppConfig | None = None) -> None:
        self.project_root = project_root.resolve()
        self.config = config or load_config()
        self.tool_root = (tool_root or self.config.root_path).resolve()
        self.overlays = OverlayService(self.project_root, overlay_dirname=self.config.overlay_dirname)
        self.store = create_index_store(self.project_root, self.config)
        self.builder = PythonIndexBuilder(self.project_root, self.store)
        self.vector_search_service = DescriptionVectorSearchService(self.project_root, self.store, config=self.config)
        self.search_service = SearchService(
            self.project_root,
            self.store,
            self.overlays,
            self.vector_search_service,
            config=self.config,
        )
        self.context_service = ContextService(self.project_root, self.store, self.overlays)
        self.reference_library_service = ReferenceLibraryService(self.tool_root, self.store, self.vector_search_service, self.config)
        self.apply_service = ApplyService(
            self.tool_root,
            config=self.config,
            overlay_dirname=self.config.overlay_dirname,
            workspace_root_dirname=self.config.workspace_root_dirname,
        )
        self.validation_service = ValidationService()
        self.run_artifacts = RunArtifactsManager(self.tool_root, runs_root_dirname=self.config.runs_root_dirname)
        self.pipeline_service = PipelineService(self, self.run_artifacts)

    def build_index(self, full_rebuild: bool = False) -> BuildReport:
        LOGGER.info('Building index for %s (full_rebuild=%s)', self.project_root, full_rebuild)
        self.overlays.refresh()
        started_at = time.perf_counter()
        report = self.builder.build(full_rebuild=full_rebuild)
        report.graph_indexing_ms = int((time.perf_counter() - started_at) * 1000)
        self.store.replace_knowledge_relations(str(self.project_root), self.overlays.knowledge_relations())
        sync_stats = self._sync_search_documents()
        report.search_documents_sync_ms = sync_stats['search_documents_sync_ms']
        report.vector_index_sync_ms = sync_stats['vector_index_sync_ms']
        report.search_documents_count = sync_stats['search_documents_count']
        report.search_documents_changed = sync_stats['search_documents_changed']
        ref_stats = self.reference_library_service.sync_documents()
        report.reference_documents_count = int(ref_stats['reference_documents_count'])
        report.reference_documents_changed = bool(ref_stats['reference_documents_changed'])
        report.reference_sync_ms = int(ref_stats.get('reference_sync_ms', 0))
        report.reference_vector_sync_ms = int(ref_stats.get('reference_vector_sync_ms', 0))
        LOGGER.info(
            'Build timings for %s: graph_indexing_ms=%s search_documents_sync_ms=%s vector_index_sync_ms=%s search_documents_count=%s changed=%s reference_sync_ms=%s reference_vector_sync_ms=%s reference_docs=%s reference_changed=%s',
            self.project_root,
            report.graph_indexing_ms,
            report.search_documents_sync_ms,
            report.vector_index_sync_ms,
            report.search_documents_count,
            report.search_documents_changed,
            report.reference_sync_ms,
            report.reference_vector_sync_ms,
            report.reference_documents_count,
            report.reference_documents_changed,
        )
        return report

    def search(
        self,
        query: str,
        limit: int = 5,
        use_vector_search: bool | None = None,
        requested_operation: str = 'replace_symbol',
    ) -> list[SearchCandidate]:
        LOGGER.info('Searching in %s for query=%r limit=%s', self.project_root, query, limit)
        self.overlays.refresh()
        return self.search_service.search(
            query=query,
            limit=limit,
            use_vector_search=use_vector_search,
            requested_operation=requested_operation,
        )

    def context(self, qualname: str) -> ContextPack:
        LOGGER.info('Building context for %s in %s', qualname, self.project_root)
        self.overlays.refresh()
        return self.context_service.build_context(qualname)

    def apply(self, artifact: PatchArtifact, generated_tests: list[dict[str, str]] | None = None) -> ApplyResult:
        LOGGER.info('Applying artifact %s (%s) to %s', artifact.target_qualname, artifact.operation, self.project_root)
        return self.apply_service.apply_artifact(self.project_root, artifact, generated_tests=generated_tests)

    def retrieve_reference_artifacts(self, change_request: ChangeRequest, selected_target: str) -> list:
        target = self.store.get_symbol(str(self.project_root), selected_target)
        target_summary = ''
        if target is not None:
            bundle = self.overlays.candidate_text_bundle(target.module_name, target.qualname)
            target_summary = ' '.join([target.name, target.docstring, str(bundle.get('symbol_title', '')), str(bundle.get('symbol_description', ''))]).strip()
        return self.reference_library_service.retrieve_for_change_request(
            query=change_request.search_text(),
            target_summary=target_summary,
            limit=self.config.reference_top_n,
        )

    def pipeline_replay(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        artifact_file: Path,
        operation: str = 'replace_symbol',
        limit: int | None = None,
        use_vector_search: bool | None = None,
    ) -> PipelineRunResult:
        LOGGER.info('Running replay pipeline for %s with target %s', self.project_root, selected_target)
        return self.pipeline_service.run_replay(
            change_request=change_request,
            selected_target=selected_target,
            artifact_file=artifact_file.resolve(),
            operation=operation,
            limit=limit or self.config.search_default_limit,
            use_vector_search=use_vector_search,
        )

    def pipeline_generate(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        requested_operation: str = 'replace_symbol',
        limit: int | None = None,
        use_vector_search: bool | None = None,
        skip_search: bool = False,
    ) -> PipelineRunResult:
        LOGGER.info('Running generate pipeline for %s with target %s', self.project_root, selected_target)
        return self.pipeline_service.run_generate(
            change_request=change_request,
            selected_target=selected_target,
            requested_operation=requested_operation,
            limit=limit or self.config.search_default_limit,
            use_vector_search=use_vector_search,
            skip_search=skip_search,
        )


    def pipeline_repair(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        previous_result_payload: dict[str, Any],
        verification_report: dict[str, Any],
        requested_operation: str = 'replace_symbol',
    ) -> PipelineRunResult:
        LOGGER.info('Running repair pipeline for %s with target %s', self.project_root, selected_target)
        return self.pipeline_service.run_repair(
            change_request=change_request,
            selected_target=selected_target,
            previous_result_payload=previous_result_payload,
            verification_report=verification_report,
            requested_operation=requested_operation,
        )
    
    def reset_embedding_usage(self) -> None:
        reset_embedding_usage()    
    
    def get_embedding_usage_summary(self) -> dict[str, Any] | None:
        try:
            return self.vector_search_service.get_embedding_usage_summary()
        except Exception:
            LOGGER.exception('Failed to read embedding usage summary from vector_search_service')
            return None 

    def _sync_search_documents(self) -> dict[str, int | bool]:
        documents: list[dict[str, str]] = []
        for symbol in self.store.list_symbols(str(self.project_root)):
            if symbol.kind == 'module':
                continue
            bundle = self.overlays.candidate_text_bundle(symbol.module_name, symbol.qualname)
            requirement_text = ' '.join(
                f"{item.get('id', '')} {item.get('title', '')} {item.get('description', '')}"
                for item in bundle.get('requirements', [])
            )
            parts = [
                symbol.docstring,
                str(bundle.get('module_title', '')),
                str(bundle.get('module_description', '')),
                str(bundle.get('symbol_title', '')),
                str(bundle.get('symbol_description', '')),
                ' '.join(bundle.get('keywords', []) or []),
                requirement_text,
            ]
            search_text = '\n'.join(part.strip() for part in parts if part and part.strip())
            if not search_text:
                continue
            documents.append(
                {
                    'doc_id': f'symbol::{symbol.qualname}',
                    'qualname': symbol.qualname,
                    'file_path': symbol.file_path,
                    'source_kind': 'symbol_description',
                    'title': str(bundle.get('symbol_title', symbol.name)),
                    'text': search_text,
                }
            )
        existing = self.store.list_search_documents(str(self.project_root))
        normalized_new = sorted(documents, key=lambda item: item['doc_id'])
        normalized_existing = sorted(existing, key=lambda item: item['doc_id'])
        changed = normalized_new != normalized_existing
        search_sync_ms = 0
        vector_sync_ms = 0
        if changed:
            started = time.perf_counter()
            self.store.replace_search_documents(str(self.project_root), documents)
            search_sync_ms = int((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            self.vector_search_service.sync_documents(documents)
            vector_sync_ms = int((time.perf_counter() - started) * 1000)
            LOGGER.info('Search documents changed for %s: synced %s docs to graph/vector stores', self.project_root, len(documents))
        else:
            LOGGER.info('Search documents unchanged for %s: skip sync to graph/vector stores', self.project_root)
        return {
            'search_documents_sync_ms': search_sync_ms,
            'vector_index_sync_ms': vector_sync_ms,
            'search_documents_count': len(documents),
            'search_documents_changed': changed,
        }
