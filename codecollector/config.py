from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codecollector.logger import get_logger

LOGGER = get_logger(__name__)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config.yaml'


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
    reference_library_dir: str
    reference_top_n: int
    reference_full_file_max_lines: int
    reference_vector_weight: float
    codegenerator_root_dir: str
    codegenerator_config_path: str
    codegenerator_python: str
    codegenerator_request_format: str
    codegenerator_target_context_chars: int
    # legacy compatibility fields; may remain unused by newer adapter logic
    codegenerator_max_full_file_chars: int
    codegenerator_max_related_test_chars: int
    codegenerator_max_reference_chars: int
    codegenerator_max_request_chars: int
    codegenerator_test_generation_mode: str
    codegenerator_repair_enabled: bool
    codegenerator_max_repair_attempts: int
    verification_run_ruff: bool
    verification_run_recommended_tests: bool
    verification_run_full_project_tests: bool

    @property
    def ui_default_demo_project_path(self) -> Path:
        return (self.root_path / self.ui_default_demo_project).resolve()


def _load_yaml_config(file_path: Path) -> dict[str, Any]:
    if not file_path.exists():
        LOGGER.warning('Config file not found at %s, using empty config.', file_path)
        return {}
    with file_path.open('r', encoding='utf-8') as handle:
        payload = yaml.safe_load(handle) or {}
        return payload if isinstance(payload, dict) else {}


def _cast_type(value: str, desired_type: type[Any]) -> Any:
    try:
        if desired_type is bool:
            return value.lower() in ('1', 'true', 'yes', 'on')
        if desired_type is int:
            return int(value)
        if desired_type is float:
            return float(value)
        if desired_type is list:
            return yaml.safe_load(value)
        return value
    except Exception as exc:
        LOGGER.warning('Could not cast value to %s: %s', desired_type, exc)
        return value


def _apply_env_overrides(config: dict[str, Any], prefix: str = '') -> dict[str, Any]:
    for key, value in list(config.items()):
        full_key = f'{prefix}__{key}'.upper() if prefix else key.upper()
        if isinstance(value, dict):
            config[key] = _apply_env_overrides(value, full_key)
            continue
        env_value = os.getenv(full_key)
        if env_value is not None:
            LOGGER.debug('Overriding %s from env', full_key)
            config[key] = _cast_type(env_value, type(value))
    return config


def _guess_type(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except Exception:
        return value


def _inject_dynamic_env_vars(config: dict[str, Any], prefix: str = 'RS__') -> dict[str, Any]:
    for env_key, raw_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        parts = [item for item in env_key[len(prefix):].split('__') if item]
        if not parts:
            continue
        cursor = config
        for part in parts[:-1]:
            lower_part = part.lower()
            matched = next((k for k in cursor.keys() if k.lower() == lower_part), None)
            if matched is None:
                matched = lower_part
                cursor[matched] = {}
            elif not isinstance(cursor[matched], dict):
                LOGGER.warning("Converting '%s' to object to inject subkeys for env %s", matched, env_key)
                cursor[matched] = {}
            cursor = cursor[matched]
        last = parts[-1].lower()
        existing = next((k for k in cursor.keys() if k.lower() == last), None)
        if existing is None:
            cursor[last] = _guess_type(raw_val)
            LOGGER.debug('Injected %s from env', env_key)
    return config


def _merge_config(path: Path) -> dict[str, Any]:
    payload = _load_yaml_config(path)
    payload = _apply_env_overrides(payload, 'RS')
    payload = _inject_dynamic_env_vars(payload)
    return payload


def load_config(config_path: Path | None = None) -> AppConfig:
    resolved_path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    payload: dict[str, Any] = _merge_config(resolved_path)
    codegen = payload.get('codegenerator', {}) if isinstance(payload.get('codegenerator', {}), dict) else {}
    return AppConfig(
        root_path=resolved_path.parent,
        app_name=payload.get('app', {}).get('name', 'codecollector'),
        version=str(payload.get('app', {}).get('version', '0.12.0')),
        overlay_dirname=payload.get('index', {}).get('overlay_dirname', '.codecollector'),
        workspace_root_dirname=payload.get('workspace', {}).get('root_dirname', '.workspaces'),
        runs_root_dirname=payload.get('runs', {}).get('root_dirname', '.runs'),
        log_level=str(payload.get('logging', {}).get('level', 'INFO')),
        log_format=str(payload.get('logging', {}).get('format', '%(asctime)s | %(levelname)s | %(name)s | %(message)s')),
        ui_default_demo_project=str(payload.get('ui', {}).get('default_demo_project', 'demo_projects/sample_python_app')),
        storage_backend=str(payload.get('storage', {}).get('backend', 'postgres')),
        postgres_graph_connection=str(payload.get('postgres', {}).get('graph_connection', '')),
        postgres_vector_connection=str(payload.get('postgres', {}).get('vector_connection', '')),
        postgres_schema=str(payload.get('postgres', {}).get('schema', 'public')),
        postgres_collection_prefix=str(payload.get('postgres', {}).get('collection_prefix', 'codecollector')),
        embedding_provider=str(payload.get('embedding', {}).get('provider', 'ollama')),
        embedding_ollama_base_url=str(payload.get('embedding', {}).get('ollama', {}).get('base_url', '')),
        embedding_ollama_model=str(payload.get('embedding', {}).get('ollama', {}).get('model', 'nomic-embed-text-v2-moe')),
        embedding_timeout_sec=int(payload.get('embedding', {}).get('timeout_sec', payload.get('embedding', {}).get('ollama', {}).get('timeout_sec', 420))),
        search_default_limit=int(payload.get('search', {}).get('default_limit', 5)),
        search_vector_enabled=bool(payload.get('search', {}).get('vector_enabled', True)),
        search_vector_backend=str(payload.get('search', {}).get('vector_backend', 'pgvector')),
        search_vector_weight=float(payload.get('search', {}).get('vector_weight', 4.0)),
        reference_library_dir=str(payload.get('reference_library', {}).get('dir', 'reference_library')),
        reference_top_n=int(payload.get('reference_library', {}).get('top_n', 2)),
        reference_full_file_max_lines=int(payload.get('reference_library', {}).get('full_file_max_lines', 32)),
        reference_vector_weight=float(payload.get('reference_library', {}).get('vector_weight', 3.0)),
        codegenerator_root_dir=str(codegen.get('root_dir', '../codegenerator')),
        codegenerator_config_path=str(codegen.get('config_path', 'config.yaml')),
        codegenerator_python=str(codegen.get('python', 'python')),
        codegenerator_request_format=str(codegen.get('request_format', 'json')),
        codegenerator_target_context_chars=int(codegen.get('target_context_chars', 5200)),
        codegenerator_max_full_file_chars=int(codegen.get('max_full_file_chars', 1800)),
        codegenerator_max_related_test_chars=int(codegen.get('max_related_test_chars', 900)),
        codegenerator_max_reference_chars=int(codegen.get('max_reference_chars', 1400)),
        codegenerator_max_request_chars=int(codegen.get('max_request_chars', 5200)),
        codegenerator_test_generation_mode=str(codegen.get('test_generation_mode', 'always')),
        codegenerator_repair_enabled=bool(codegen.get('repair_enabled', True)),
        codegenerator_max_repair_attempts=int(codegen.get('max_repair_attempts', 1)),
        verification_run_ruff=bool(payload.get('verification', {}).get('run_ruff', False)),
        verification_run_recommended_tests=bool(payload.get('verification', {}).get('run_recommended_tests', True)),
        verification_run_full_project_tests=bool(payload.get('verification', {}).get('run_full_project_tests', False)),
    )
