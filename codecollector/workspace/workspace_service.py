from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.onboarding.onboarding_service import OnboardingService
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_service import SessionService
from codecollector.workspace.staging_manager import StagingManager

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
        run_label = run_id.replace('pipeline-', '', 1)
        return self.runs_root / run_id / f'pipeline_run_{run_label}.json'

    def _load_run_bundle(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        path = self._run_bundle_path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def _extract_excluded_files_from_bundle(self, bundle: dict[str, Any] | None) -> list[str]:
        if not bundle:
            return []

        merge_plan = bundle.get('merge_plan') or {}
        excluded: list[str] = []
        seen: set[str] = set()

        for item in list(merge_plan.get('excluded_files') or []):
            rel = str(item or '').strip().replace('\\', '/')
            if not rel:
                continue
            if self._is_skipped_path(rel):
                continue
            if rel not in seen:
                seen.add(rel)
                excluded.append(rel)

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

    def _extract_changed_files_from_bundle(self, bundle: dict[str, Any] | None) -> list[str]:
        if not bundle:
            return []
        merge_plan = bundle.get('merge_plan') or {}
        apply_result = bundle.get('apply_result') or {}
        impact = apply_result.get('impact') or {}
        diff_payload = apply_result.get('diff') or {}
        excluded_files = {
            str(item or '').strip().replace('\\', '/')
            for item in list(merge_plan.get('excluded_files') or [])
            if str(item or '').strip()
        }        

        candidates: list[str] = []
        candidates.extend(list(impact.get('changed_files') or []))
        candidates.extend(list(diff_payload.get('changed_files') or []))
        candidates.extend(list(merge_plan.get('changed_files') or []))

        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            rel = str(item or '').strip()
            if not rel:
                continue
            # Если путь абсолютный и указывает на файл внутри workspace, переводим в относительный.
            if rel.startswith(str(self.workspace_root)) or rel.startswith(str(self.tool_root)):
                # абсолютные пути из bundle нам здесь не нужны
                continue
            rel = rel.replace('\\', '/')
            if self._is_skipped_path(rel):
                continue
            if rel in excluded_files:
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
           excluded_set = {item.replace('\\', '/') for item in excluded_files}
           changed_files = [
               rel for rel in changed_files
               if rel.replace('\\', '/') not in excluded_set
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
            excluded_set = {item.replace('\\', '/') for item in excluded_files}
            changed_files = [
                rel for rel in changed_files
                if rel.replace('\\', '/') not in excluded_set
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

    def apply_workspace(self, workspace_id: str) -> dict[str, Any]:
        resolved = self.resolve(workspace_id)
        if resolved.project_root is None or resolved.project_id is None:
            raise ValueError(f'Workspace {workspace_id} is not linked to a registered project')
        session = self._find_session_for_workspace(workspace_id)
        if session is not None and str(session.get('last_workspace_id') or '') != workspace_id:
            raise ValueError(
                f'Workspace {workspace_id} is not the final workspace of the session and cannot be applied'
            )
        diff_payload = self.diff_workspace(workspace_id)
        changed_files = diff_payload['changed_files']
        excluded_files = list(diff_payload.get('excluded_files') or [])        
        applied_files: list[str] = []
        for rel in changed_files:
            src = resolved.workspace_path / rel
            dst = resolved.project_root / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()
            applied_files.append(rel)

        onboarding_result = self.onboarding.onboard_project(resolved.project_id, full_rebuild=False)

        if session is not None:
            session['status'] = 'applied'
            session['updated_at'] = _utc_now()
            self.sessions.registry.save(session)

        return {
            'workspace_id': workspace_id,
            'workspace_path': str(resolved.workspace_path),
            'project_id': resolved.project_id,
            'project_root': str(resolved.project_root),
            'run_id': resolved.run_id,
            'applied_files': applied_files,
            'excluded_files': excluded_files,            
            'applied': True,
            'onboarding': asdict(onboarding_result),
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
