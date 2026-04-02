from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class StagingManager:
    def __init__(self, tool_root: Path, workspace_root_dirname: str = '.workspaces') -> None:
        self.tool_root = tool_root.resolve()
        self.workspace_root = self.tool_root / workspace_root_dirname
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def create_workspace(self, source_project: Path) -> Path:
        timestamp = datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%S.%fZ')
        destination = self.workspace_root / f'{source_project.name}-{timestamp}-{uuid4().hex[:6]}'
        LOGGER.info('Creating staging workspace %s from %s', destination, source_project)
        shutil.copytree(source_project, destination)
        return destination

    def delete_workspace(self, workspace: Path) -> None:
        workspace = workspace.resolve()
        if not workspace.exists():
            return
        LOGGER.info('Deleting staging workspace %s', workspace)
        shutil.rmtree(workspace, ignore_errors=False)
