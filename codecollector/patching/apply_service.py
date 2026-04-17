from __future__ import annotations

import ast
from pathlib import Path

from codecollector.config import AppConfig, load_config
from codecollector.domain.models import ApplyResult, ImpactSummary, PatchArtifact, SymbolRecord
from codecollector.indexing.builder import PythonIndexBuilder
from codecollector.indexing.base import IndexStore
from codecollector.indexing.storage_factory import create_index_store
from codecollector.logger import get_logger
from codecollector.overlays.service import OverlayService
from codecollector.validation.service import ValidationService
from codecollector.workspace.diff_service import DiffService
from codecollector.workspace.staging_manager import StagingManager

LOGGER = get_logger(__name__)


class ApplyService:
    def __init__(self, tool_root: Path, config: AppConfig | None = None, overlay_dirname: str = '.codecollector', workspace_root_dirname: str = '.workspaces') -> None:
        self.tool_root = tool_root.resolve()
        self.config = config or load_config()
        self.overlay_dirname = overlay_dirname
        self.staging = StagingManager(self.tool_root, workspace_root_dirname=workspace_root_dirname)
        self.diff_service = DiffService()

    def apply_artifact(self, source_project: Path, artifact: PatchArtifact, generated_tests: list[dict[str, str]] | None = None) -> ApplyResult:
        source_project = source_project.resolve()
        workspace = self.staging.create_workspace(source_project)
        try:
            project_key = str(workspace)
            store = create_index_store(workspace, self.config)
            overlays = OverlayService(workspace, overlay_dirname=self.overlay_dirname)
            builder = PythonIndexBuilder(workspace, store)
            builder.build(full_rebuild=True)
            store.replace_knowledge_relations(project_key, overlays.knowledge_relations())

            symbol = store.get_symbol(project_key, artifact.target_qualname)
            if symbol is None:
                raise ValueError(f'Unknown qualname in workspace: {artifact.target_qualname}')

            target_path = workspace / symbol.file_path
            before_path = source_project / symbol.file_path
            self._apply_operation_to_file(target_path, symbol, artifact)
            changed_files = [target_path]
            for test_artifact in generated_tests or []:
                test_path = workspace / str(test_artifact['file_path'])
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_source = str(test_artifact['source_code']).rstrip('\n') + '\n'
                ast.parse(test_source)
                test_path.write_text(test_source, encoding='utf-8')
                changed_files.append(test_path)
            validation = ValidationService().validate_project(workspace, changed_files=changed_files)

            reindexed = False
            if validation.is_valid:
                LOGGER.info('Targeted reindex of changed workspace files after successful validation: %s', workspace)
                reindex_report = builder.build_changed_files(changed_files)
                store.replace_knowledge_relations(project_key, overlays.knowledge_relations())
                reindexed = reindex_report.indexed_files > 0

            diff = self.diff_service.summarize_file_change(before_path, target_path)
            impact = self._build_impact_summary(
                project_key,
                store,
                overlays,
                artifact.target_qualname,
                changed_files=[str(path.relative_to(workspace)) for path in changed_files],
                validation_ok=validation.is_valid,
            )
            return ApplyResult(workspace, artifact, diff, validation, reindexed, impact)
        except Exception:
            try:
                self.staging.delete_workspace(workspace)
            except Exception:
                LOGGER.exception('Failed to delete staging workspace after apply failure: %s', workspace)
            raise

    def _apply_operation_to_file(self, target_path: Path, symbol: SymbolRecord, artifact: PatchArtifact) -> None:
        LOGGER.info('Applying %s for %s in %s', artifact.operation, symbol.qualname, target_path)
        source = target_path.read_text(encoding='utf-8')
        lines = source.splitlines()
        payload_lines = self._normalize_payload_lines(artifact.replacement_code)

        if artifact.operation == 'replace_symbol':
            start = symbol.start_line - 1
            end = symbol.end_line
            lines[start:end] = payload_lines
        elif artifact.operation == 'insert_after_symbol':
            insert_at = symbol.end_line
            lines[insert_at:insert_at] = self._with_spacing_before_insert(lines, insert_at, payload_lines)
        else:
            raise ValueError(f'Unsupported patch operation: {artifact.operation}')

        updated_source = '\n'.join(lines).rstrip('\n') + '\n'
        ast.parse(updated_source)
        target_path.write_text(updated_source, encoding='utf-8')

    def _normalize_payload_lines(self, payload: str) -> list[str]:
        stripped = payload.rstrip('\n')
        if not stripped:
            raise ValueError('Patch artifact is empty.')
        return stripped.splitlines()

    def _with_spacing_before_insert(self, lines: list[str], insert_at: int, payload_lines: list[str]) -> list[str]:
        result = list(payload_lines)
        if insert_at > 0 and lines[insert_at - 1].strip():
            result = [''] + result
        if insert_at < len(lines) and lines[insert_at].strip():
            result = result + ['']
        return result

    def _build_impact_summary(
        self,
        project_key: str,
        store: IndexStore,
        overlays: OverlayService,
        target_qualname: str,
        changed_files: list[str],
        validation_ok: bool,
    ) -> ImpactSummary:
        changed_file_path = changed_files[0] if changed_files else ''
        direct_inbound = [
            relation for relation in store.list_inbound_relations_for_qualname(project_key, target_qualname)
            if relation.relation_kind in {'calls', 'exposed_by_controller', 'covered_by_test'}
        ]

        inbound_callers: list[str] = []
        related_tests: list[str] = []
        seen_callers: set[str] = set()
        seen_tests: set[str] = set()
        frontier: list[str] = []

        for relation in direct_inbound:
            if relation.source_qualname not in seen_callers:
                seen_callers.add(relation.source_qualname)
                inbound_callers.append(relation.source_qualname)
                frontier.append(relation.source_qualname)
            if relation.relation_kind == 'covered_by_test' or relation.source_qualname.startswith('tests.'):
                if relation.source_qualname not in seen_tests:
                    seen_tests.add(relation.source_qualname)
                    related_tests.append(relation.source_qualname)

        for caller_qualname in list(frontier):
            for relation in store.list_inbound_relations_for_qualname(project_key, caller_qualname):
                if relation.relation_kind not in {'calls', 'exposed_by_controller', 'covered_by_test'}:
                    continue
                if relation.source_qualname not in seen_callers:
                    seen_callers.add(relation.source_qualname)
                    inbound_callers.append(relation.source_qualname)
                if relation.source_qualname.startswith('tests.') and relation.source_qualname not in seen_tests:
                    seen_tests.add(relation.source_qualname)
                    related_tests.append(relation.source_qualname)

        linked_requirements = overlays.requirements_for_symbol(target_qualname)
        recommended_tests = sorted(related_tests)
        recommended_test_commands = self._recommended_test_commands(store, project_key, recommended_tests)
        notes = ['Impact summary is heuristic and based on the current graph index.']
        if validation_ok:
            notes.append('Структурная валидация прошла успешно; изменение можно анализировать дальше вручную.')
        else:
            notes.append('Структурная валидация завершилась с ошибкой; оценка последствий неполная.')
        if linked_requirements:
            notes.append('Для target есть связанные требования из knowledge-слоя.')
        if recommended_tests:
            notes.append('Для изменения подобраны рекомендуемые тесты к прогону.')
        if not inbound_callers:
            notes.append('Во входящем графе вызовов не найдено вызывающих символов.')

        symbols_in_changed_files: list[str] = []
        seen_symbols: set[str] = set()
        if target_qualname:
            symbols_in_changed_files.append(target_qualname)
            seen_symbols.add(target_qualname)
        for file_path in changed_files:
            if file_path == changed_file_path:
                continue
            for changed_symbol in store.list_symbols_in_file(project_key, file_path):
                if changed_symbol.kind == 'module' or changed_symbol.qualname in seen_symbols:
                    continue
                seen_symbols.add(changed_symbol.qualname)
                symbols_in_changed_files.append(changed_symbol.qualname)

        return ImpactSummary(
            target_qualname=target_qualname,
            changed_files=changed_files,
            symbols_in_changed_files=symbols_in_changed_files,
            inbound_callers=inbound_callers,
            related_tests=related_tests,
            linked_requirements=linked_requirements,
            notes=notes,
            recommended_tests=recommended_tests,
            recommended_test_commands=recommended_test_commands,
        )

    def _recommended_test_commands(self, store: IndexStore, project_key: str, test_qualnames: list[str]) -> list[str]:
        commands: list[str] = []
        seen_paths: set[str] = set()
        for qualname in test_qualnames:
            symbol = store.get_symbol(project_key, qualname)
            if symbol and symbol.file_path not in seen_paths:
                seen_paths.add(symbol.file_path)
                commands.append(f'pytest {symbol.file_path}')
        return commands
