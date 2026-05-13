from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.projects.project_cleanup_service import ProjectCleanupService
from codecollector.projects.project_registry import ProjectRegistry, RegisteredProject


LOGGER = get_logger(__name__)

class ProjectRootAlreadyRegisteredError(ValueError):
    def __init__(self, *, project_root: Path, existing_project: RegisteredProject) -> None:
        self.project_root = str(project_root.resolve())
        self.existing_project_id = existing_project.project_id
        self.existing_project_name = existing_project.project_name
        message = (
            'Project root уже зарегистрирован: '
            f'project_root={self.project_root} '
            f'existing_project_id={self.existing_project_id} '
            f'existing_project_name={self.existing_project_name}'
        )
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            'error_code': 'project_root_already_registered',
            'project_root': self.project_root,
            'existing_project_id': self.existing_project_id,
            'existing_project_name': self.existing_project_name,
        }



class ProjectService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.registry = ProjectRegistry(self.tool_root, config)

    def register_project(
        self,
        *,
        project_name: str,
        project_root: Path,
        languages: list[str] | None = None,
        verification_commands: list[str] | None = None,
        index_excludes: list[str] | None = None,
        reference_library_ids: list[str] | None = None,
        reference_library_paths: list[str] | None = None,
    ) -> RegisteredProject:
        resolved_root = project_root.resolve()
        if not resolved_root.exists() or not resolved_root.is_dir():
            raise ValueError(f'Project root does not exist or is not a directory: {resolved_root}')
        existing = self.registry.list_by_project_root(str(resolved_root))
        if existing:
            first = existing[0]
            LOGGER.warning(
                'Project root registration rejected because it is already registered: project_root=%s existing_project_id=%s existing_project_name=%s',
                resolved_root,
                first.project_id,
                first.project_name,
            )
            raise ProjectRootAlreadyRegisteredError(project_root=resolved_root, existing_project=first)
        timestamp = datetime.now(UTC).isoformat()
        payload: dict[str, Any] = {
            'project_name': project_name.strip() or resolved_root.name,
            'project_root': str(resolved_root),
            'languages': languages or ['python'],
            'verification_commands': verification_commands or [],
            'index_excludes': index_excludes or [],
            'reference_library_ids': reference_library_ids or [],
            'reference_library_paths': reference_library_paths or [],
            'status': 'registered',
            'created_at': timestamp,
            'updated_at': timestamp,
            'knowledge_path': '',
            'knowledge_updated_at': '',
        }
        return self.registry.create(payload)

    def get_project(self, project_id: str) -> RegisteredProject:
        project = self.registry.get(project_id)
        if project is None:
            raise ValueError(f'Unknown project_id: {project_id}')
        return project

    def list_projects(self) -> list[RegisteredProject]:
        return self.registry.list()

    def delete_project_registration(self, project_id: str) -> bool:
        return self.registry.delete(project_id)

    def delete_project(self, project_id: str) -> dict[str, Any]:
        project = self.registry.get(project_id)
        if project is None:
            return {'project_id': project_id, 'deleted': False, 'cleanup': {}}

        project_root = str(Path(project.project_root).resolve())
        sibling_projects = [
            item
            for item in self.registry.list_by_project_root(project_root)
            if item.project_id != project_id
        ]
        deleted = self.registry.delete(project_id)
        if sibling_projects:
            LOGGER.warning(
                'Project data cleanup skipped because project_root is still registered by other projects: project_root=%s remaining_project_ids=%s',
                project_root,
                [item.project_id for item in sibling_projects],
            )
            cleanup = {
                'project_root': project_root,
                'skipped': True,
                'reason': 'project_root_still_registered',
                'remaining_project_ids': [item.project_id for item in sibling_projects],
                'graph_deleted': {},
                'vector_deleted': 0,
                'warnings': [],
            }
        else:
            cleanup = ProjectCleanupService(self.tool_root, self.config).cleanup_project_data(project_root)
        return {'project_id': project_id, 'deleted': deleted, 'cleanup': cleanup}

    def mark_status(self, project_id: str, status: str) -> RegisteredProject:
        project = self.get_project(project_id)
        updated = RegisteredProject.from_dict({**asdict(project), 'status': status, 'updated_at': datetime.now(UTC).isoformat()})
        return self.registry.update(updated)

    def update_onboarding_metadata(self, project_id: str, *, status: str, knowledge_path: str, knowledge_updated_at: str) -> RegisteredProject:
        project = self.get_project(project_id)
        updated = RegisteredProject.from_dict({
            **asdict(project),
            'status': status,
            'updated_at': datetime.now(UTC).isoformat(),
            'knowledge_path': knowledge_path,
            'knowledge_updated_at': knowledge_updated_at,
        })
        return self.registry.update(updated)
