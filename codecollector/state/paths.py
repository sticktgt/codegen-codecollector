from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codecollector.config import AppConfig


@dataclass(slots=True)
class StatePaths:
    root: Path
    projects: Path
    libraries: Path
    sessions: Path


class StatePathService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config

    def build(self) -> StatePaths:
        state_root_dirname = getattr(self.config, 'state_root_dirname', '.state')
        root = (self.tool_root / state_root_dirname).resolve()
        return StatePaths(
            root=root,
            projects=root / 'projects',
            libraries=root / 'libraries',
            sessions=root / 'sessions',
        )

    def ensure(self) -> StatePaths:
        paths = self.build()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.projects.mkdir(parents=True, exist_ok=True)
        paths.libraries.mkdir(parents=True, exist_ok=True)
        paths.sessions.mkdir(parents=True, exist_ok=True)
        return paths
