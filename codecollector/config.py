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
    storage_backend: str
    postgres_graph_connection: str
    postgres_vector_connection: str
    postgres_schema: str
    postgres_collection_prefix: str
    embedding_provider: str
    embedding_ollama_base_url: str
    embedding_ollama_model: str
    embedding_timeout_sec: int
    search_default_limit: int
    search_vector_enabled: bool
    search_vector_backend: str
    search_vector_weight: float

    @property
    def ui_default_demo_project_path(self) -> Path:
        return (self.root_path / self.ui_default_demo_project).resolve()



def load_config(config_path: Path | None = None) -> AppConfig:
    resolved_path = (config_path or Path(__file__).resolve().parents[1] / 'config.yaml').resolve()
    payload: dict[str, Any] = yaml.safe_load(resolved_path.read_text(encoding='utf-8')) or {}
    return AppConfig(
        root_path=resolved_path.parent,
        app_name=payload.get('app', {}).get('name', 'codecollector'),
        version=str(payload.get('app', {}).get('version', '0.10.3')),
        overlay_dirname=payload.get('index', {}).get('overlay_dirname', '.codecollector'),
        workspace_root_dirname=payload.get('workspace', {}).get('root_dirname', '.workspaces'),
        runs_root_dirname=payload.get('runs', {}).get('root_dirname', '.runs'),
        log_level=str(payload.get('logging', {}).get('level', 'INFO')),
        log_format=str(payload.get('logging', {}).get('format', '%(asctime)s | %(levelname)s | %(name)s | %(message)s')),
        ui_default_demo_project=str(payload.get('ui', {}).get('default_demo_project', 'demo_projects/sample_python_app')),
        storage_backend=str(payload.get('storage', {}).get('backend', 'postgres')),
        postgres_graph_connection=str(payload.get('postgres', {}).get('graph_connection', 'postgresql://postgres:postgres@localhost:5432/vector_db')),
        postgres_vector_connection=str(payload.get('postgres', {}).get('vector_connection', 'postgresql+psycopg://postgres:postgres@localhost:5432/vector_db')),
        postgres_schema=str(payload.get('postgres', {}).get('schema', 'public')),
        postgres_collection_prefix=str(payload.get('postgres', {}).get('collection_prefix', 'codecollector')),
        embedding_provider=str(payload.get('embedding', {}).get('provider', 'ollama')),
        embedding_ollama_base_url=str(payload.get('embedding', {}).get('ollama', {}).get('base_url', 'http://192.168.50.165:18081')),
        embedding_ollama_model=str(payload.get('embedding', {}).get('ollama', {}).get('model', 'nomic-embed-text-v2-moe')),
        embedding_timeout_sec=int(payload.get('embedding', {}).get('ollama', {}).get('timeout_sec', payload.get('embedding', {}).get('timeout_sec', 420))),
        search_default_limit=int(payload.get('search', {}).get('default_limit', 5)),
        search_vector_enabled=bool(payload.get('search', {}).get('vector_enabled', True)),
        search_vector_backend=str(payload.get('search', {}).get('vector_backend', 'pgvector')),
        search_vector_weight=float(payload.get('search', {}).get('vector_weight', 4.0)),
    )
