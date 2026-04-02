from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.state.ids import new_project_id
from codecollector.state.paths import StatePathService
from codecollector.state.storage import JsonStateStore


@dataclass(slots=True)
class RegisteredProject:
    project_id: str
    project_name: str
    project_root: str
    languages: list[str]
    verification_commands: list[str]
    index_excludes: list[str]
    reference_library_ids: list[str]
    reference_library_paths: list[str]
    status: str
    created_at: str
    updated_at: str
    knowledge_path: str = ''
    knowledge_updated_at: str = ''

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> 'RegisteredProject':
        return cls(
            project_id=str(payload['project_id']),
            project_name=str(payload['project_name']),
            project_root=str(payload['project_root']),
            languages=[str(item) for item in payload.get('languages', []) or []],
            verification_commands=[str(item) for item in payload.get('verification_commands', []) or []],
            index_excludes=[str(item) for item in payload.get('index_excludes', []) or []],
            reference_library_ids=[str(item) for item in payload.get('reference_library_ids', []) or []],
            reference_library_paths=[str(item) for item in payload.get('reference_library_paths', []) or []],
            status=str(payload.get('status', 'registered')),
            created_at=str(payload.get('created_at', '')),
            updated_at=str(payload.get('updated_at', '')),
            knowledge_path=str(payload.get('knowledge_path', '')),
            knowledge_updated_at=str(payload.get('knowledge_updated_at', '')),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            'project_id': self.project_id,
            'project_name': self.project_name,
            'project_root': self.project_root,
            'languages': self.languages,
            'verification_commands': self.verification_commands,
            'index_excludes': self.index_excludes,
            'reference_library_ids': self.reference_library_ids,
            'reference_library_paths': self.reference_library_paths,
            'status': self.status,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'knowledge_path': self.knowledge_path,
            'knowledge_updated_at': self.knowledge_updated_at,
        }


class ProjectRegistry:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.paths = StatePathService(tool_root, config).ensure()
        self.store = JsonStateStore(self.paths.projects)

    def create(self, payload: dict[str, Any]) -> RegisteredProject:
        project = RegisteredProject.from_dict({**payload, 'project_id': payload.get('project_id') or new_project_id()})
        self.store.write(f'{project.project_id}.json', project.to_dict())
        return project

    def get(self, project_id: str) -> RegisteredProject | None:
        rel = f'{project_id}.json'
        if not self.store.exists(rel):
            return None
        return RegisteredProject.from_dict(self.store.read(rel))

    def list(self) -> list[RegisteredProject]:
        items: list[RegisteredProject] = []
        for path in self.store.list():
            items.append(RegisteredProject.from_dict(self.store.read(path.name)))
        return items

    def delete(self, project_id: str) -> bool:
        rel = f'{project_id}.json'
        if not self.store.exists(rel):
            return False
        self.store.delete(rel)
        return True

    def update(self, project: RegisteredProject) -> RegisteredProject:
        self.store.write(f'{project.project_id}.json', project.to_dict())
        return project
