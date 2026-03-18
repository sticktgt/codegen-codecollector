from __future__ import annotations

from pathlib import Path

from codecollector.config import AppConfig, load_config
from codecollector.context.service import ContextService
from codecollector.domain.models import ApplyResult, ChangeRequest, ContextPack, PatchArtifact, PipelineRunResult, SearchCandidate
from codecollector.indexing.builder import BuildReport, PythonIndexBuilder
from codecollector.indexing.storage_sqlite import SQLiteIndexStore
from codecollector.logger import get_logger
from codecollector.orchestration.pipeline_service import PipelineService
from codecollector.orchestration.run_artifacts import RunArtifactsManager
from codecollector.overlays.service import OverlayService
from codecollector.patching.apply_service import ApplyService
from codecollector.search.service import SearchService

LOGGER = get_logger(__name__)


class ProjectServices:
    def __init__(self, project_root: Path, tool_root: Path | None = None, config: AppConfig | None = None) -> None:
        self.project_root = project_root.resolve()
        self.config = config or load_config()
        self.tool_root = (tool_root or self.config.root_path).resolve()
        self.overlays = OverlayService(self.project_root, overlay_dirname=self.config.overlay_dirname)
        self.store = SQLiteIndexStore(self.project_root / self.config.overlay_dirname / 'index.db')
        self.builder = PythonIndexBuilder(self.project_root, self.store)
        self.search_service = SearchService(self.project_root, self.store, self.overlays)
        self.context_service = ContextService(self.project_root, self.store, self.overlays)
        self.apply_service = ApplyService(
            self.tool_root,
            overlay_dirname=self.config.overlay_dirname,
            workspace_root_dirname=self.config.workspace_root_dirname,
        )
        self.run_artifacts = RunArtifactsManager(self.tool_root, runs_root_dirname=self.config.runs_root_dirname)
        self.pipeline_service = PipelineService(self, self.run_artifacts)

    def build_index(self, full_rebuild: bool = False) -> BuildReport:
        LOGGER.info('Building index for %s (full_rebuild=%s)', self.project_root, full_rebuild)
        self.overlays.refresh()
        report = self.builder.build(full_rebuild=full_rebuild)
        self.store.replace_knowledge_relations(str(self.project_root), self.overlays.knowledge_relations())
        return report

    def search(self, query: str, limit: int = 5) -> list[SearchCandidate]:
        LOGGER.info('Searching in %s for query=%r limit=%s', self.project_root, query, limit)
        self.overlays.refresh()
        return self.search_service.search(query=query, limit=limit)

    def context(self, qualname: str) -> ContextPack:
        LOGGER.info('Building context for %s in %s', qualname, self.project_root)
        self.overlays.refresh()
        return self.context_service.build_context(qualname)

    def apply(self, artifact: PatchArtifact) -> ApplyResult:
        LOGGER.info('Applying artifact %s (%s) to %s', artifact.target_qualname, artifact.operation, self.project_root)
        return self.apply_service.apply_artifact(self.project_root, artifact)

    def pipeline_replay(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        artifact_file: Path,
        operation: str = 'replace_symbol',
        limit: int | None = None,
    ) -> PipelineRunResult:
        LOGGER.info('Running replay pipeline for %s with target %s', self.project_root, selected_target)
        return self.pipeline_service.run_replay(
            change_request=change_request,
            selected_target=selected_target,
            artifact_file=artifact_file.resolve(),
            operation=operation,
            limit=limit or self.config.search_default_limit,
        )
