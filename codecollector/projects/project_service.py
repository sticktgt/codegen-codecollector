from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.projects.project_registry import ProjectRegistry, RegisteredProject


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
