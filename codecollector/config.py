from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class AppConfig:
    root_path: Path
    app_name: str
    version: str
    overlay_dirname: str
    workspace_root_dirname: str
    runs_root_dirname: str
    log_level: str
    log_format: str
    ui_default_demo_project: str
    search_default_limit: int

    @property
    def ui_default_demo_project_path(self) -> Path:
        return (self.root_path / self.ui_default_demo_project).resolve()



def load_config(config_path: Path | None = None) -> AppConfig:
    resolved_path = (config_path or Path(__file__).resolve().parents[1] / 'config.yaml').resolve()
    payload: dict[str, Any] = yaml.safe_load(resolved_path.read_text(encoding='utf-8')) or {}
    return AppConfig(
        root_path=resolved_path.parent,
        app_name=payload.get('app', {}).get('name', 'codecollector'),
        version=str(payload.get('app', {}).get('version', '0.8.0')),
        overlay_dirname=payload.get('index', {}).get('overlay_dirname', '.codecollector'),
        workspace_root_dirname=payload.get('workspace', {}).get('root_dirname', '.workspaces'),
        runs_root_dirname=payload.get('runs', {}).get('root_dirname', '.runs'),
        log_level=str(payload.get('logging', {}).get('level', 'INFO')),
        log_format=str(payload.get('logging', {}).get('format', '%(asctime)s | %(levelname)s | %(name)s | %(message)s')),
        ui_default_demo_project=str(payload.get('ui', {}).get('default_demo_project', 'demo_projects/sample_python_app')),
        search_default_limit=int(payload.get('search', {}).get('default_limit', 5)),
    )
