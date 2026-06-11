from __future__ import annotations

import difflib
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.onboarding.knowledge_traceability import KnowledgeTraceabilityService
from codecollector.onboarding.onboarding_service import OnboardingService
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_service import SessionService
from codecollector.workspace.staging_manager import StagingManager
from codecollector.vector_search.ollama_embeddings import reset_embedding_usage, snapshot_embedding_usage

LOGGER = get_logger(__name__)


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

SKIP_DIR_NAMES = {
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    '.git',
    '.idea',
    '.vscode',
}

BINARY_SUFFIXES = {
    '.pyc', '.pyo', '.so', '.dll', '.dylib', '.a', '.o',
    '.db', '.sqlite', '.sqlite3',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico',
    '.pdf', '.zip', '.gz', '.bz2', '.xz',
}

@dataclass(slots=True)
class ResolvedWorkspace:
    workspace_id: str
    workspace_path: Path
    session_id: str | None
    project_id: str | None
    project_root: Path | None
    run_id: str | None


class WorkspaceService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.sessions = SessionService(self.tool_root, config)
        self.projects = ProjectService(self.tool_root, config)
        self.staging = StagingManager(self.tool_root, workspace_root_dirname=config.workspace_root_dirname)
        self.workspace_root = self.tool_root / config.workspace_root_dirname
        self.runs_root = self.tool_root / config.runs_root_dirname
        self.onboarding = OnboardingService(self.tool_root, config)

    def _workspace_path(self, workspace_id: str) -> Path:
        return (self.workspace_root / workspace_id).resolve()

    def _find_session_for_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        for payload in self.sessions.list_sessions():
            ids = [str(item) for item in payload.get('workspace_ids', []) or []]
            if workspace_id in ids or str(payload.get('last_workspace_id') or '') == workspace_id:
                return payload
        return None

    def _run_bundle_path(self, run_id: str) -> Path:
        """Return the most likely pipeline run artifact path for a run id.

        Run directories are named by the full run id, for example:
        pipeline-20260515T152756.954502Z-740abb

        The bundle file is written by run label and may omit the random suffix:
        pipeline_run_20260515T152756.954502Z.json

        Older code built the filename from the full run id and therefore missed
        the bundle during workspace apply. Prefer an exact historical path when
        present, then fall back to the newest pipeline_run_*.json in the run dir.
        """
        run_dir = self.runs_root / run_id
        run_label = run_id.replace('pipeline-', '', 1)
        exact_path = run_dir / f'pipeline_run_{run_label}.json'
        if exact_path.exists():
            return exact_path

        if not run_dir.exists():
            return exact_path

        candidates = sorted(
            run_dir.glob('pipeline_run_*.json'),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
        return exact_path

    def _load_run_bundle(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        path = self._run_bundle_path(run_id)
        if not path.exists():
            LOGGER.warning('Pipeline run bundle not found for run_id=%s expected_path=%s', run_id, path)
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def _normalize_rel_path(self, value: Any) -> str:
        rel = str(value or '').strip().replace('\\', '/')
        while rel.startswith('./'):
            rel = rel[2:]
        return rel

    def _append_excluded_file(self, result: list[str], seen: set[str], value: Any) -> None:
        rel = self._normalize_rel_path(value)
        if not rel or self._is_skipped_path(rel):
            return
        key = rel.lower()
        if key in seen:
            # Preserve exact path casing when a later source contains a concrete
            # generated-test path. Older run artifacts sometimes lowercased the
            # same file in excluded_files.
            for index, existing in enumerate(result):
                if existing.lower() == key and existing != rel and self._is_generated_test_path(rel):
                    result[index] = rel
                    return
            return
        seen.add(key)
        result.append(rel)

    def _is_generated_test_path(self, rel_path: str) -> bool:
        rel = self._normalize_rel_path(rel_path)
        if not rel:
            return False
        path = Path(rel)
        return (
            len(path.parts) >= 2
            and path.parts[0] == 'tests'
            and path.name.startswith('test_generated_')
            and path.suffix == '.py'
        )

    def _bundle_workspace_path(self, bundle: dict[str, Any] | None) -> Path | None:
        if not bundle:
            return None
        merge_plan = bundle.get('merge_plan') or {}
        apply_result = bundle.get('apply_result') or {}
        for candidate in (
            merge_plan.get('workspace_path'),
            apply_result.get('workspace_path'),
            bundle.get('workspace_path'),
        ):
            if candidate:
                try:
                    return Path(str(candidate)).resolve()
                except OSError:
                    return Path(str(candidate))
        return None

    def _normalize_bundle_path(self, value: Any, bundle: dict[str, Any] | None = None) -> str:
        rel = self._normalize_rel_path(value)
        if not rel:
            return ''
        path = Path(rel)
        if path.is_absolute():
            roots: list[Path] = []
            workspace_path = self._bundle_workspace_path(bundle)
            if workspace_path is not None:
                roots.append(workspace_path)
            roots.extend([self.workspace_root.resolve(), self.tool_root.resolve()])
            for root in roots:
                try:
                    rel_path = path.resolve().relative_to(root)
                    return rel_path.as_posix()
                except (ValueError, OSError):
                    continue
            return ''
        return rel

    def _append_excluded_bundle_file(
        self,
        result: list[str],
        seen: set[str],
        value: Any,
        bundle: dict[str, Any] | None,
    ) -> None:
        rel = self._normalize_bundle_path(value, bundle)
        self._append_excluded_file(result, seen, rel)

    def _is_excluded_path(self, rel_path: str, excluded_files: set[str]) -> bool:
        rel = self._normalize_rel_path(rel_path)
        if not rel:
            return False
        # Exact comparison is preferred. Case-insensitive fallback protects old run
        # artifacts where generated test file paths were lowercased by mistake.
        return rel in excluded_files or rel.lower() in {item.lower() for item in excluded_files}

    def _extract_excluded_files_from_bundle(self, bundle: dict[str, Any] | None) -> list[str]:
        """Return files that must not be copied from workspace to project.

        Generated tests can be physically present in the final workspace even when
        semantic or runtime verification rejected them. The apply step is the last
        safety boundary, so it must honor every exclusion source from the run
        bundle and preserve exact path casing when it is available.
        """
        if not bundle:
            return []

        excluded: list[str] = []
        seen: set[str] = set()

        merge_plan = bundle.get('merge_plan') or {}
        apply_result = bundle.get('apply_result') or {}
        generated_test_apply = bundle.get('generated_test_apply') or {}
        verification_report = bundle.get('verification_report') or {}
        verification_summary = verification_report.get('summary') or {}
        execution_summary = bundle.get('execution_summary') or {}

        changed_candidates: list[Any] = []
        impact = apply_result.get('impact') or {}
        diff_payload = apply_result.get('diff') or {}
        changed_candidates.extend(list(impact.get('changed_files') or []))
        changed_candidates.extend(list(diff_payload.get('changed_files') or []))
        changed_candidates.extend(list(merge_plan.get('changed_files') or []))

        for item in list(merge_plan.get('excluded_files') or []):
            self._append_excluded_bundle_file(excluded, seen, item, bundle)

        # If generated tests were rejected, candidate/applied generated tests must
        # never be copied to the user project. Older bundles may put the same data
        # in different summary fields, so collect all known forms.
        generated_test_rejected = bool(
            generated_test_apply.get('skipped')
            or generated_test_apply.get('verification_failed')
            or verification_summary.get('generated_test_failed')
            or verification_summary.get('generated_test_runtime_only')
            or execution_summary.get('status') == 'generated_test_verification_failed'
        )

        # Accepted generated tests are only safe to copy if generated-test
        # verification did not fail. When verification_failed=true, applied_tests
        # are the rejected tests and should be excluded.
        accepted_generated_tests: set[str] = set()
        if not generated_test_rejected and not generated_test_apply.get('skipped'):
            for item in list(generated_test_apply.get('applied_tests') or []):
                rel = self._normalize_bundle_path(item, bundle)
                if rel:
                    accepted_generated_tests.add(rel.lower())

        for item in list(generated_test_apply.get('excluded_files') or []):
            self._append_excluded_bundle_file(excluded, seen, item, bundle)
        for item in list(verification_summary.get('generated_test_excluded_files') or []):
            self._append_excluded_bundle_file(excluded, seen, item, bundle)
        for item in list(verification_summary.get('generated_test_failed_files') or []):
            self._append_excluded_bundle_file(excluded, seen, item, bundle)

        if generated_test_rejected:
            for item in list(generated_test_apply.get('candidate_test_files') or []):
                self._append_excluded_bundle_file(excluded, seen, item, bundle)
            for item in list(generated_test_apply.get('applied_tests') or []):
                self._append_excluded_bundle_file(excluded, seen, item, bundle)

            # Fail-safe: if the run says generated tests failed, exclude every
            # changed generated-test file that was not explicitly accepted. This
            # protects old bundles with missing excluded_files or wrong casing.
            for item in changed_candidates:
                rel = self._normalize_bundle_path(item, bundle)
                if not rel or not self._is_generated_test_path(rel):
                    continue
                if rel.lower() in accepted_generated_tests:
                    continue
                self._append_excluded_file(excluded, seen, rel)

        return excluded

    def _is_skipped_path(self, rel_path: str) -> bool:
        path = Path(rel_path)
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            return True
        if path.suffix.lower() in BINARY_SUFFIXES:
            return True
        return False

    def _read_text_lines_safe(self, path: Path) -> list[str] | None:
        if not path.exists():
            return []
        try:
            return path.read_text(encoding='utf-8').splitlines(keepends=True)
        except UnicodeDecodeError:
            LOGGER.warning('Skip non-utf8 file in workspace diff: %s', path)
            return None

    def _extract_changed_files_from_bundle(self, bundle: dict[str, Any] | None, *, include_excluded: bool = False) -> list[str]:
        if not bundle:
            return []
        merge_plan = bundle.get('merge_plan') or {}
        apply_result = bundle.get('apply_result') or {}
        impact = apply_result.get('impact') or {}
        diff_payload = apply_result.get('diff') or {}
        excluded_files = set(self._extract_excluded_files_from_bundle(bundle))

        candidates: list[str] = []
        candidates.extend(list(impact.get('changed_files') or []))
        candidates.extend(list(diff_payload.get('changed_files') or []))
        candidates.extend(list(merge_plan.get('changed_files') or []))

        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            rel = self._normalize_bundle_path(item, bundle)
            if not rel:
                continue
            if self._is_skipped_path(rel):
                continue
            if not include_excluded and self._is_excluded_path(rel, excluded_files):
                continue
            if rel not in seen:
                seen.add(rel)
                normalized.append(rel)
        return normalized

    def resolve(self, workspace_id: str) -> ResolvedWorkspace:
        workspace_path = self._workspace_path(workspace_id)
        if not workspace_path.exists():
            raise FileNotFoundError(f'Workspace not found: {workspace_id}')
        session = self._find_session_for_workspace(workspace_id)
        if session is None:
            return ResolvedWorkspace(workspace_id, workspace_path, None, None, None, None)
        project_id = str(session.get('project_id') or '') or None
        project = self.projects.get_project(project_id) if project_id else None
        return ResolvedWorkspace(
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            session_id=str(session.get('session_id') or '') or None,
            project_id=project_id,
            project_root=Path(project.project_root).resolve() if project is not None else None,
            run_id=str(session.get('last_run_id') or '') or None,
        )

    def get_workspace(self, workspace_id: str) -> dict[str, Any]:
        resolved = self.resolve(workspace_id)
        session = self._find_session_for_workspace(workspace_id)
        bundle = self._load_run_bundle(resolved.run_id)
        excluded_files = self._extract_excluded_files_from_bundle(bundle) 
        changed_files = self._extract_changed_files_from_bundle(bundle)
        if not changed_files and resolved.project_root is not None:
            changed_files = self._collect_changed_files(resolved.project_root, resolved.workspace_path)
        if excluded_files:
            excluded_set = {self._normalize_rel_path(item) for item in excluded_files}
            changed_files = [
                rel for rel in changed_files
                if not self._is_excluded_path(rel, excluded_set)
            ]
        return {
            'workspace_id': workspace_id,
            'workspace_path': str(resolved.workspace_path),
            'session_id': resolved.session_id,
            'project_id': resolved.project_id,
            'project_root': str(resolved.project_root) if resolved.project_root else None,
            'run_id': resolved.run_id,
            'changed_files': changed_files,
            'excluded_files': excluded_files,
            'is_last_workspace': bool(session and str(session.get('last_workspace_id') or '') == workspace_id),
        }

    def diff_workspace(self, workspace_id: str) -> dict[str, Any]:
        resolved = self.resolve(workspace_id)
        if resolved.project_root is None:
            raise ValueError(f'Workspace {workspace_id} is not linked to a registered project')
        bundle = self._load_run_bundle(resolved.run_id)
        excluded_files = self._extract_excluded_files_from_bundle(bundle)        
        changed_files = self._extract_changed_files_from_bundle(bundle)
        if not changed_files:
            changed_files = self._collect_changed_files(resolved.project_root, resolved.workspace_path)

        if excluded_files:
            excluded_set = {self._normalize_rel_path(item) for item in excluded_files}
            changed_files = [
                rel for rel in changed_files
                if not self._is_excluded_path(rel, excluded_set)
            ]
        unified_parts: list[str] = []
        for rel in changed_files:
            if self._is_skipped_path(rel):
                continue            
            before = resolved.project_root / rel
            after = resolved.workspace_path / rel
            before_lines = self._read_text_lines_safe(before)
            after_lines = self._read_text_lines_safe(after)
            if before_lines is None or after_lines is None:
                continue
            unified_parts.append(''.join(difflib.unified_diff(before_lines, after_lines, fromfile=str(before), tofile=str(after))))
        return {
            'workspace_id': workspace_id,
            'workspace_path': str(resolved.workspace_path),
            'project_root': str(resolved.project_root),
            'run_id': resolved.run_id,
            'changed_files': [rel for rel in changed_files if not self._is_skipped_path(rel)],
            'excluded_files': excluded_files,            
            'unified_diff': ''.join(unified_parts),
        }

    def _normalize_string_list(self, values: list[Any] | tuple[Any, ...] | None) -> list[str]:
        result: list[str] = []
        for item in values or []:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _extract_traceability_symbols_from_bundle(self, bundle: dict[str, Any] | None) -> list[str]:
        return self._extract_traceability_targets_from_bundle(bundle, project_root=None)['symbols']

    def _extract_traceability_targets_from_bundle(
        self,
        bundle: dict[str, Any] | None,
        *,
        project_root: Path | None,
    ) -> dict[str, list[str]]:
        if not bundle:
            return {'symbols': [], 'modules': []}

        apply_result = bundle.get('apply_result') if isinstance(bundle.get('apply_result'), dict) else {}
        artifact = apply_result.get('artifact') if isinstance(apply_result.get('artifact'), dict) else {}
        impact = apply_result.get('impact') if isinstance(apply_result.get('impact'), dict) else {}
        execution_summary = bundle.get('execution_summary') if isinstance(bundle.get('execution_summary'), dict) else {}

        operation = str(artifact.get('operation') or execution_summary.get('operation') or '').strip()
        if operation == 'insert_after_symbol':
            created_symbols = self._extract_inserted_traceability_symbols(artifact, impact, execution_summary)
            if project_root is not None:
                resolved = self._resolve_inserted_traceability_targets_against_knowledge(
                    project_root=project_root,
                    created_symbols=created_symbols,
                    artifact=artifact,
                    execution_summary=execution_summary,
                )
                if resolved['symbols'] or resolved['modules']:
                    return resolved
            elif created_symbols:
                return {'symbols': created_symbols, 'modules': []}

            LOGGER.warning(
                'Traceability target skipped for insert_after_symbol: created symbol was not resolved target=%s parent=%s',
                artifact.get('target_qualname') or execution_summary.get('selected_target'),
                artifact.get('parent_qualname') or execution_summary.get('parent_qualname'),
            )
            return {'symbols': [], 'modules': []}

        symbols: list[str] = []
        self._append_traceability_qualname(symbols, artifact.get('target_qualname'))
        self._append_traceability_qualname(symbols, impact.get('target_qualname'))
        self._append_traceability_qualname(symbols, execution_summary.get('selected_target'))
        if not symbols:
            for item in self._bundle_changed_symbols(impact, execution_summary):
                self._append_traceability_qualname(symbols, item)
        return {'symbols': symbols, 'modules': []}

    def _resolve_inserted_traceability_targets_against_knowledge(
        self,
        *,
        project_root: Path,
        created_symbols: list[str],
        artifact: dict[str, Any],
        execution_summary: dict[str, Any],
    ) -> dict[str, list[str]]:
        knowledge_symbols, knowledge_modules = self._knowledge_symbol_and_module_names(project_root)

        existing_symbols = [qualname for qualname in created_symbols if qualname in knowledge_symbols]
        if existing_symbols:
            return {'symbols': existing_symbols, 'modules': []}

        fallback_modules: list[str] = []
        parent_qualname = str(artifact.get('parent_qualname') or execution_summary.get('parent_qualname') or '').strip()
        if parent_qualname in knowledge_modules:
            self._append_traceability_qualname(fallback_modules, parent_qualname)

        for qualname in created_symbols:
            module_name = self._nearest_known_module_for_qualname(qualname, knowledge_modules)
            self._append_traceability_qualname(fallback_modules, module_name)

        if fallback_modules:
            return {'symbols': [], 'modules': fallback_modules}
        return {'symbols': [], 'modules': []}

    def _knowledge_symbol_and_module_names(self, project_root: Path) -> tuple[set[str], set[str]]:
        knowledge_path = project_root / self.config.overlay_dirname / 'knowledge.yaml'
        if not knowledge_path.exists():
            return set(), set()
        try:
            payload = yaml.safe_load(knowledge_path.read_text(encoding='utf-8')) or {}
        except Exception as exc:  # noqa: BLE001 - traceability update can still skip safely.
            LOGGER.warning('Could not read knowledge.yaml to resolve traceability targets: path=%s error=%s', knowledge_path, exc)
            return set(), set()
        if not isinstance(payload, dict):
            return set(), set()
        symbols = payload.get('symbols')
        modules = payload.get('modules')
        symbol_names = set(symbols.keys()) if isinstance(symbols, dict) else set()
        module_names = set(modules.keys()) if isinstance(modules, dict) else set()
        return symbol_names, module_names

    def _nearest_known_module_for_qualname(self, qualname: str, knowledge_modules: set[str]) -> str:
        current = str(qualname or '').strip()
        while '.' in current:
            current = current.rsplit('.', 1)[0]
            if current in knowledge_modules:
                return current
        return ''

    def _extract_inserted_traceability_symbols(
        self,
        artifact: dict[str, Any],
        impact: dict[str, Any],
        execution_summary: dict[str, Any],
    ) -> list[str]:
        parent_qualname = str(artifact.get('parent_qualname') or execution_summary.get('parent_qualname') or '').strip()
        expected_kind = str(artifact.get('expected_new_symbol_kind') or execution_summary.get('expected_new_symbol_kind') or '').strip()
        insert_scope = str(artifact.get('insert_scope') or execution_summary.get('insert_scope') or '').strip()

        if not parent_qualname:
            parent_qualname = self._insert_parent_from_target(artifact, execution_summary)

        candidates: list[str] = []
        for item in self._bundle_changed_symbols(impact, execution_summary):
            self._append_traceability_qualname(candidates, item)

        generated_from_checks = self._collect_generated_symbol_names(bundle_fragment=execution_summary)
        for item in generated_from_checks:
            self._append_traceability_qualname(candidates, item)

        if parent_qualname:
            direct_children = [
                qualname for qualname in candidates
                if self._is_direct_child_symbol(qualname, parent_qualname)
            ]
            if expected_kind in {'method', 'function', 'class'}:
                filtered = self._filter_inserted_symbols_by_kind_suffix(direct_children, expected_kind)
                if filtered:
                    return filtered
            if direct_children:
                return direct_children

        replacement_name = self._top_level_symbol_name_from_code(str(artifact.get('replacement_code') or artifact.get('source_code') or ''))
        if replacement_name:
            if insert_scope == 'class_body' and parent_qualname:
                fallback = f'{parent_qualname}.{replacement_name}'
            else:
                module_prefix = parent_qualname or self._module_prefix_from_target(artifact, execution_summary)
                fallback = f'{module_prefix}.{replacement_name}' if module_prefix else replacement_name
            result: list[str] = []
            self._append_traceability_qualname(result, fallback)
            return result

        return []

    def _bundle_changed_symbols(self, impact: dict[str, Any], execution_summary: dict[str, Any]) -> list[Any]:
        symbols = impact.get('symbols_in_changed_files')
        if isinstance(symbols, list) and symbols:
            return symbols
        summary_symbols = execution_summary.get('symbols_in_changed_files')
        return summary_symbols if isinstance(summary_symbols, list) else []

    def _append_traceability_qualname(self, result: list[str], value: Any) -> None:
        qualname = str(value or '').strip()
        if not qualname or qualname.startswith('tests.'):
            return
        if qualname not in result:
            result.append(qualname)

    def _is_direct_child_symbol(self, qualname: str, parent_qualname: str) -> bool:
        if qualname == parent_qualname or not qualname.startswith(f'{parent_qualname}.'):
            return False
        remainder = qualname[len(parent_qualname) + 1:]
        return bool(remainder) and '.' not in remainder

    def _filter_inserted_symbols_by_kind_suffix(self, qualnames: list[str], expected_kind: str) -> list[str]:
        if expected_kind != 'method':
            return qualnames
        return [qualname for qualname in qualnames if qualname.rsplit('.', 1)[-1] != '__init__']

    def _insert_parent_from_target(self, artifact: dict[str, Any], execution_summary: dict[str, Any]) -> str:
        target = str(artifact.get('target_qualname') or execution_summary.get('selected_target') or '').strip()
        insert_scope = str(artifact.get('insert_scope') or execution_summary.get('insert_scope') or '').strip()
        if insert_scope == 'class_body':
            return target
        if '.' in target:
            return target.rsplit('.', 1)[0]
        return ''

    def _module_prefix_from_target(self, artifact: dict[str, Any], execution_summary: dict[str, Any]) -> str:
        target_file = str(artifact.get('target_file') or execution_summary.get('target_file') or '').strip()
        if target_file.endswith('.py'):
            return target_file[:-3].replace('/', '.').replace('\\', '.')
        target = str(artifact.get('target_qualname') or execution_summary.get('selected_target') or '').strip()
        return target.rsplit('.', 1)[0] if '.' in target else ''

    def _top_level_symbol_name_from_code(self, source_code: str) -> str:
        import ast
        import textwrap

        try:
            tree = ast.parse(textwrap.dedent(source_code))
        except SyntaxError:
            return ''
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return str(node.name)
        return ''

    def _collect_generated_symbol_names(self, *, bundle_fragment: dict[str, Any]) -> list[str]:
        result: list[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                generated = value.get('generated_symbol_names')
                if isinstance(generated, list):
                    for item in generated:
                        self._append_traceability_qualname(result, item)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(bundle_fragment)
        return result

    def apply_workspace(
        self,
        workspace_id: str,
        *,
        change_request_id: str | None = None,
        requirement_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        timings: dict[str, int] = {}
        counts: dict[str, int] = {}

        stage_started = time.perf_counter()
        resolved = self.resolve(workspace_id)
        timings['resolve_workspace_ms'] = int((time.perf_counter() - stage_started) * 1000)

        if resolved.project_root is None or resolved.project_id is None:
            raise ValueError(f'Workspace {workspace_id} is not linked to a registered project')
        session = self._find_session_for_workspace(workspace_id)
        if session is not None and str(session.get('last_workspace_id') or '') != workspace_id:
            raise ValueError(
                f'Workspace {workspace_id} is not the final workspace of the session and cannot be applied'
            )

        stage_started = time.perf_counter()
        diff_payload = self.diff_workspace(workspace_id)
        timings['diff_workspace_ms'] = int((time.perf_counter() - stage_started) * 1000)

        bundle = self._load_run_bundle(resolved.run_id)
        changed_files = self._extract_changed_files_from_bundle(bundle, include_excluded=True)
        if not changed_files:
            changed_files = diff_payload['changed_files']
        excluded_files = list(diff_payload.get('excluded_files') or [])
        excluded_set = {self._normalize_rel_path(item) for item in excluded_files}
        counts['changed_files'] = len(changed_files)
        counts['excluded_files'] = len(excluded_files)

        applied_files: list[str] = []
        copied_files: list[str] = []
        deleted_files: list[str] = []
        skipped_excluded_files: list[str] = []

        stage_started = time.perf_counter()
        for rel in changed_files:
            if self._is_excluded_path(rel, excluded_set):
                normalized_rel = self._normalize_rel_path(rel)
                skipped_excluded_files.append(normalized_rel)
                LOGGER.info('Skip excluded workspace file during apply: workspace_id=%s file=%s', workspace_id, normalized_rel)
                continue
            src = resolved.workspace_path / rel
            dst = resolved.project_root / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied_files.append(rel)
            elif dst.exists():
                dst.unlink()
                deleted_files.append(rel)
            applied_files.append(rel)
        timings['copy_files_ms'] = int((time.perf_counter() - stage_started) * 1000)
        counts['applied_files'] = len(applied_files)
        counts['copied_files'] = len(copied_files)
        counts['deleted_files'] = len(deleted_files)
        counts['skipped_excluded_files'] = len(skipped_excluded_files)

        LOGGER.info(
            'Workspace apply copied files: workspace_id=%s changed=%s applied=%s copied=%s deleted=%s skipped_excluded=%s',
            workspace_id,
            counts['changed_files'],
            counts['applied_files'],
            counts['copied_files'],
            counts['deleted_files'],
            counts['skipped_excluded_files'],
        )

        stage_started = time.perf_counter()
        reset_embedding_usage()
        onboarding_result = self.onboarding.onboard_project(resolved.project_id, full_rebuild=False)
        timings['index_refresh_ms'] = int((time.perf_counter() - stage_started) * 1000)
        embedding_usage = snapshot_embedding_usage()

        onboarding_payload = asdict(onboarding_result)

        stage_started = time.perf_counter()
        traceability_update: dict[str, Any] = {
            'updated': False,
            'skipped_reason': 'missing_traceability_ids',
            'symbols': [],
            'modules': [],
            'requirements': self._normalize_string_list(requirement_ids),
            'change_request_id': str(change_request_id or '').strip(),
            'knowledge_path': str(Path(onboarding_payload.get('knowledge_path') or resolved.project_root / self.config.overlay_dirname / 'knowledge.yaml')),
        }
        trace_requirement_ids = self._normalize_string_list(requirement_ids)
        trace_change_request_id = str(change_request_id or '').strip()
        if trace_change_request_id or trace_requirement_ids:
            try:
                traceability_targets = self._extract_traceability_targets_from_bundle(
                    bundle,
                    project_root=resolved.project_root,
                )
                traceability_update = KnowledgeTraceabilityService(resolved.project_root, self.config).update_for_applied_workspace(
                    symbol_qualnames=traceability_targets['symbols'],
                    module_names=traceability_targets['modules'],
                    change_request_id=trace_change_request_id,
                    requirement_ids=trace_requirement_ids,
                    applied_at=_utc_now(),
                )
                if traceability_update.get('updated'):
                    try:
                        from codecollector.indexing.storage_factory import create_index_store
                        from codecollector.overlays.service import OverlayService

                        overlays = OverlayService(resolved.project_root, overlay_dirname=self.config.overlay_dirname)
                        store = create_index_store(resolved.project_root, self.config)
                        store.replace_knowledge_relations(str(resolved.project_root), overlays.knowledge_relations())
                        traceability_update['knowledge_relations_refreshed'] = True
                    except Exception as relation_exc:  # noqa: BLE001 - YAML update is the source of truth; relation refresh can retry on next reindex.
                        LOGGER.warning(
                            'Knowledge relation refresh after traceability update failed for workspace_id=%s: %s',
                            workspace_id,
                            relation_exc,
                        )
                        traceability_update['knowledge_relations_refreshed'] = False
                        traceability_update['knowledge_relations_refresh_error'] = str(relation_exc)
            except Exception as exc:  # noqa: BLE001 - apply itself has already succeeded; report traceability failure separately.
                LOGGER.warning('Knowledge traceability update failed for workspace_id=%s: %s', workspace_id, exc)
                traceability_update = {
                    'updated': False,
                    'skipped_reason': 'error',
                    'error': str(exc),
                    'symbols': [],
                    'modules': [],
                    'requirements': trace_requirement_ids,
                    'change_request_id': trace_change_request_id,
                    'knowledge_path': str(Path(onboarding_payload.get('knowledge_path') or resolved.project_root / self.config.overlay_dirname / 'knowledge.yaml')),
                }
        timings['traceability_update_ms'] = int((time.perf_counter() - stage_started) * 1000)

        index_refresh = {
            'indexed_files': onboarding_payload.get('indexed_files', 0),
            'unchanged_files': onboarding_payload.get('unchanged_files', 0),
            'deleted_files': onboarding_payload.get('deleted_files', 0),
            'search_documents_count': onboarding_payload.get('search_documents_count', 0),
            'search_documents_changed': onboarding_payload.get('search_documents_changed', False),
            'graph_indexing_ms': onboarding_payload.get('graph_indexing_ms', 0),
            'search_documents_sync_ms': onboarding_payload.get('search_documents_sync_ms', 0),
            'vector_index_sync_ms': onboarding_payload.get('vector_index_sync_ms', 0),
            'embedded_documents_count': onboarding_payload.get('embedded_documents_count', 0),
            'vector_sync_mode': onboarding_payload.get('vector_sync_mode', ''),
            'embedding_usage': embedding_usage,
        }

        if session is not None:
            stage_started = time.perf_counter()
            session['status'] = 'applied'
            session['updated_at'] = _utc_now()
            self.sessions.registry.save(session)
            timings['session_update_ms'] = int((time.perf_counter() - stage_started) * 1000)

        timings['total_ms'] = int((time.perf_counter() - total_started) * 1000)
        LOGGER.info(
            'Workspace apply timings: workspace_id=%s total_ms=%s timings=%s counts=%s index_refresh=%s',
            workspace_id,
            timings['total_ms'],
            timings,
            counts,
            index_refresh,
        )

        return {
            'workspace_id': workspace_id,
            'workspace_path': str(resolved.workspace_path),
            'project_id': resolved.project_id,
            'project_root': str(resolved.project_root),
            'run_id': resolved.run_id,
            'applied_files': applied_files,
            'copied_files': copied_files,
            'deleted_files': deleted_files,
            'excluded_files': excluded_files,
            'skipped_excluded_files': skipped_excluded_files,
            'counts': counts,
            'timings': timings,
            'index_refresh': index_refresh,
            'applied': True,
            'onboarding': onboarding_payload,
            'traceability_update': traceability_update,
        }

    def delete_workspace(self, workspace_id: str) -> dict[str, Any]:
        resolved = self.resolve(workspace_id)
        self.staging.delete_workspace(resolved.workspace_path)
        return {
            'workspace_id': workspace_id,
            'deleted': True,
        }

    def _collect_changed_files(self, project_root: Path, workspace_path: Path) -> list[str]:
        changed: list[str] = []
        seen: set[str] = set()
        for path in workspace_path.rglob('*'):
            if path.is_dir():
                continue
            rel = str(path.relative_to(workspace_path))
            if self._is_skipped_path(rel):
                continue            
            seen.add(rel)
            project_file = project_root / rel
            if not project_file.exists() or project_file.read_bytes() != path.read_bytes():
                changed.append(rel)
        for path in project_root.rglob('*'):
            if path.is_dir():
                continue
            rel = str(path.relative_to(project_root))
            if self._is_skipped_path(rel):
                continue
            if rel in seen:
                continue
            workspace_file = workspace_path / rel
            if not workspace_file.exists():
                changed.append(rel)
        return sorted(set(changed))
