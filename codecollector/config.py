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
    state_root_dirname: str
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
    codegenerator_include_reference_artifacts: bool
    codegenerator_include_full_file_for_non_symbol_targets: bool
    codegenerator_include_full_file_for_generate_test: bool
    codegenerator_include_full_file_for_repair: bool
    codegenerator_repair_full_file_chars: int
    codegenerator_generate_related_tests_max_items: int
    codegenerator_generate_test_related_tests_max_items: int
    codegenerator_repair_related_tests_max_items: int
    codegenerator_generate_related_symbols_max_items: int
    codegenerator_generate_related_symbol_chars: int
    codegenerator_generate_test_related_symbols_max_items: int
    codegenerator_generate_test_related_symbol_chars: int
    codegenerator_repair_related_symbols_max_items: int
    codegenerator_repair_related_symbol_chars: int
    codegenerator_generate_reference_max_items: int
    codegenerator_generate_test_reference_max_items: int
    codegenerator_repair_reference_max_items: int
    codegenerator_test_generation_mode: str
    codegenerator_repair_enabled: bool
    codegenerator_max_repair_attempts: int
    verification_run_ruff: bool
    verification_run_recommended_tests: bool
    verification_run_full_project_tests: bool
    analysis_llm_enabled: bool
    analysis_llm_base_url: str
    analysis_llm_api_key: str
    analysis_llm_model: str
    analysis_llm_timeout_sec: int
    analysis_llm_temperature: float
    analysis_llm_num_ctx: int
    analysis_llm_num_predict: int
    analysis_llm_keep_alive: int | str
    analysis_llm_think: bool | None
    analysis_llm_max_prompt_chars: int
    analysis_llm_search_plan_max_prompt_chars: int
    analysis_llm_rerank_max_prompt_chars: int
    analysis_llm_rerank_soft_overflow_ratio: float
    analysis_llm_rerank_json_indent: int
    analysis_llm_rerank_drop_operation_definitions_on_overflow: bool
    analysis_llm_rerank_candidate_drop_fields: list[str]
    analysis_llm_rerank_candidate_keep_fields: list[str]
    analysis_llm_rerank_emergency_min_candidate_cards: int
    analysis_llm_rerank_raw_preview_chars: int
    analysis_prompt_dir: str
    analysis_system_template: str
    analysis_search_plan_template: str
    analysis_rerank_template: str
    analysis_project_map_max_files: int
    analysis_project_map_min_files: int
    analysis_project_map_symbols_per_file: int
    analysis_project_map_min_symbols_per_file: int
    analysis_project_map_module_doc_chars: int
    analysis_project_map_symbol_doc_chars: int
    analysis_base_search_limit: int
    analysis_max_search_plan_queries: int
    analysis_max_recall_candidates: int
    analysis_max_candidate_cards: int
    analysis_min_candidate_cards: int
    analysis_candidate_source_chars: int
    analysis_candidate_source_min_chars: int
    analysis_candidate_related_test_chars: int
    analysis_candidate_related_test_min_chars: int
    analysis_candidate_related_tests: int
    analysis_candidate_siblings: int
    analysis_candidate_min_siblings: int
    analysis_candidate_sibling_doc_chars: int
    analysis_candidate_module_doc_chars: int
    analysis_candidate_relation_limit: int
    analysis_candidate_search_reasons: int
    analysis_max_return_candidates: int
    analysis_min_confidence_auto_operation: float
    analysis_min_confidence_auto_recommend_target: float
    onboarding_architecture_doc_names: list[str]
    onboarding_architecture_enrichment_system_template: str
    onboarding_architecture_enrichment_template: str
    onboarding_architecture_enrichment_max_prompt_chars: int
    onboarding_architecture_enrichment_soft_overflow_ratio: float
    onboarding_architecture_doc_max_chars: int
    onboarding_architecture_doc_min_chars: int
    onboarding_architecture_doc_min_ratio: float
    onboarding_architecture_enrichment_max_modules: int
    onboarding_architecture_enrichment_max_symbols: int
    onboarding_architecture_enrichment_docstring_chars: int
    onboarding_architecture_enrichment_json_indent: int
    onboarding_architecture_enrichment_num_predict: int

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




def _string_list_config(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = default
    result = [str(item).strip() for item in items if str(item).strip()]
    return result or list(default)

def _merge_config(path: Path) -> dict[str, Any]:
    payload = _load_yaml_config(path)
    payload = _apply_env_overrides(payload, 'RS')
    payload = _inject_dynamic_env_vars(payload)
    return payload


def load_config(config_path: Path | None = None) -> AppConfig:
    resolved_path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    payload: dict[str, Any] = _merge_config(resolved_path)
    codegen = payload.get('codegenerator', {}) if isinstance(payload.get('codegenerator', {}), dict) else {}
    analysis = payload.get('analysis', {}) if isinstance(payload.get('analysis', {}), dict) else {}
    llm_assist = analysis.get('llm_assist', {}) if isinstance(analysis.get('llm_assist', {}), dict) else {}
    prompts = analysis.get('prompts', {}) if isinstance(analysis.get('prompts', {}), dict) else {}
    recall = analysis.get('recall', {}) if isinstance(analysis.get('recall', {}), dict) else {}
    candidate_context = analysis.get('candidate_context', {}) if isinstance(analysis.get('candidate_context', {}), dict) else {}
    analysis_result = analysis.get('result', {}) if isinstance(analysis.get('result', {}), dict) else {}
    onboarding = payload.get('onboarding', {}) if isinstance(payload.get('onboarding', {}), dict) else {}
    onboarding_knowledge = onboarding.get('knowledge', {}) if isinstance(onboarding.get('knowledge', {}), dict) else {}
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
        state_root_dirname=str(payload.get('state', {}).get('root_dirname', '.state')),
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
        codegenerator_include_reference_artifacts=bool(codegen.get('include_reference_artifacts', False)),
        codegenerator_include_full_file_for_non_symbol_targets=bool(codegen.get('include_full_file_for_non_symbol_targets', True)),
        codegenerator_include_full_file_for_generate_test=bool(codegen.get('include_full_file_for_generate_test', True)),
        codegenerator_include_full_file_for_repair=bool(codegen.get('include_full_file_for_repair', True)),
        codegenerator_repair_full_file_chars=int(codegen.get('repair_full_file_chars', 6000)),
        codegenerator_generate_related_tests_max_items=int(codegen.get('generate_related_tests_max_items', 1)),
        codegenerator_generate_test_related_tests_max_items=int(codegen.get('generate_test_related_tests_max_items', 1)),
        codegenerator_repair_related_tests_max_items=int(codegen.get('repair_related_tests_max_items', 1)),
        codegenerator_generate_related_symbols_max_items=int(codegen.get('generate_related_symbols_max_items', 4)),
        codegenerator_generate_related_symbol_chars=int(codegen.get('generate_related_symbol_chars', 700)),
        codegenerator_generate_test_related_symbols_max_items=int(codegen.get('generate_test_related_symbols_max_items', 4)),
        codegenerator_generate_test_related_symbol_chars=int(codegen.get('generate_test_related_symbol_chars', 700)),
        codegenerator_repair_related_symbols_max_items=int(codegen.get('repair_related_symbols_max_items', 3)),
        codegenerator_repair_related_symbol_chars=int(codegen.get('repair_related_symbol_chars', 500)),
        codegenerator_generate_reference_max_items=int(codegen.get('generate_reference_max_items', 1)),
        codegenerator_generate_test_reference_max_items=int(codegen.get('generate_test_reference_max_items', 1)),
        codegenerator_repair_reference_max_items=int(codegen.get('repair_reference_max_items', 1)),
        codegenerator_test_generation_mode=str(codegen.get('test_generation_mode', 'always')),
        codegenerator_repair_enabled=bool(codegen.get('repair_enabled', True)),
        codegenerator_max_repair_attempts=int(codegen.get('max_repair_attempts', 1)),
        verification_run_ruff=bool(payload.get('verification', {}).get('run_ruff', False)),
        verification_run_recommended_tests=bool(payload.get('verification', {}).get('run_recommended_tests', True)),
        verification_run_full_project_tests=bool(payload.get('verification', {}).get('run_full_project_tests', False)),
        analysis_llm_enabled=bool(llm_assist.get('enabled', True)),
        analysis_llm_base_url=str(llm_assist.get('base_url', '')),
        analysis_llm_api_key=str(llm_assist.get('api_key', '')),
        analysis_llm_model=str(llm_assist.get('model', 'qwen3-coder-next')),
        analysis_llm_timeout_sec=int(llm_assist.get('timeout_sec', 420)),
        analysis_llm_temperature=float(llm_assist.get('temperature', 0.0)),
        analysis_llm_num_ctx=int(llm_assist.get('num_ctx', 16384)),
        analysis_llm_num_predict=int(llm_assist.get('num_predict', 700)),
        analysis_llm_keep_alive=llm_assist.get('keep_alive', 0),
        analysis_llm_think=llm_assist.get('think', False),
        analysis_llm_max_prompt_chars=int(llm_assist.get('max_prompt_chars', 12000)),
        analysis_llm_search_plan_max_prompt_chars=int(llm_assist.get('search_plan_max_prompt_chars', llm_assist.get('max_prompt_chars', 8000))),
        analysis_llm_rerank_max_prompt_chars=int(llm_assist.get('rerank_max_prompt_chars', llm_assist.get('max_prompt_chars', 12000))),
        analysis_llm_rerank_soft_overflow_ratio=float(llm_assist.get('rerank_soft_overflow_ratio', 1.03)),
        analysis_llm_rerank_json_indent=int(llm_assist.get('rerank_json_indent', 2)),
        analysis_llm_rerank_drop_operation_definitions_on_overflow=bool(llm_assist.get('rerank_drop_operation_definitions_on_overflow', False)),
        analysis_llm_rerank_candidate_drop_fields=list(llm_assist.get('rerank_candidate_drop_fields', [])),
        analysis_llm_rerank_candidate_keep_fields=list(llm_assist.get('rerank_candidate_keep_fields', [])),
        analysis_llm_rerank_emergency_min_candidate_cards=int(llm_assist.get('rerank_emergency_min_candidate_cards', candidate_context.get('min_candidate_cards', 5))),
        analysis_llm_rerank_raw_preview_chars=int(llm_assist.get('rerank_raw_preview_chars', 1200)),
        analysis_prompt_dir=str(prompts.get('dir', 'codecollector/prompts')),
        analysis_system_template=str(prompts.get('system_template', 'codecollector/prompts/analyze_system_template.txt')),
        analysis_search_plan_template=str(prompts.get('search_plan_template', 'codecollector/prompts/analyze_search_plan_user_template.txt')),
        analysis_rerank_template=str(prompts.get('rerank_template', 'codecollector/prompts/analyze_candidate_rerank_user_template.txt')),
        analysis_project_map_max_files=int(recall.get('project_map_max_files', 10)),
        analysis_project_map_min_files=int(recall.get('project_map_min_files', 4)),
        analysis_project_map_symbols_per_file=int(recall.get('project_map_symbols_per_file', 12)),
        analysis_project_map_min_symbols_per_file=int(recall.get('project_map_min_symbols_per_file', 4)),
        analysis_project_map_module_doc_chars=int(recall.get('project_map_module_doc_chars', 140)),
        analysis_project_map_symbol_doc_chars=int(recall.get('project_map_symbol_doc_chars', 90)),
        analysis_base_search_limit=int(recall.get('base_search_limit', 10)),
        analysis_max_search_plan_queries=int(recall.get('max_search_plan_queries', 3)),
        analysis_max_recall_candidates=int(recall.get('max_recall_candidates', 12)),
        analysis_max_candidate_cards=int(candidate_context.get('max_candidate_cards', 7)),
        analysis_min_candidate_cards=int(candidate_context.get('min_candidate_cards', 5)),
        analysis_candidate_source_chars=int(candidate_context.get('candidate_source_chars', 260)),
        analysis_candidate_source_min_chars=int(candidate_context.get('candidate_source_min_chars', 120)),
        analysis_candidate_related_test_chars=int(candidate_context.get('related_test_chars', 180)),
        analysis_candidate_related_test_min_chars=int(candidate_context.get('related_test_min_chars', 80)),
        analysis_candidate_related_tests=int(candidate_context.get('related_tests', 1)),
        analysis_candidate_siblings=int(candidate_context.get('siblings', 3)),
        analysis_candidate_min_siblings=int(candidate_context.get('min_siblings', 1)),
        analysis_candidate_sibling_doc_chars=int(candidate_context.get('sibling_doc_chars', 70)),
        analysis_candidate_module_doc_chars=int(candidate_context.get('module_doc_chars', 100)),
        analysis_candidate_relation_limit=int(candidate_context.get('relation_limit', 1)),
        analysis_candidate_search_reasons=int(candidate_context.get('search_reasons', 3)),
        analysis_max_return_candidates=int(analysis_result.get('max_candidates', 5)),
        analysis_min_confidence_auto_operation=float(llm_assist.get('min_confidence_auto_operation', 0.65)),
        analysis_min_confidence_auto_recommend_target=float(llm_assist.get('min_confidence_auto_recommend_target', 0.65)),
        onboarding_architecture_doc_names=_string_list_config(onboarding_knowledge.get('architecture_doc_names'), ['ARCHITECT.md', 'ARCHITECTURE.md']),
        onboarding_architecture_enrichment_system_template=str(onboarding_knowledge.get('architecture_enrichment_system_template', 'codecollector/prompts/knowledge_architect_enrichment_system_template.txt')),
        onboarding_architecture_enrichment_template=str(onboarding_knowledge.get('architecture_enrichment_template', 'codecollector/prompts/knowledge_architect_enrichment_user_template.txt')),
        onboarding_architecture_enrichment_max_prompt_chars=int(onboarding_knowledge.get('architecture_enrichment_max_prompt_chars', llm_assist.get('max_prompt_chars', 32000))),
        onboarding_architecture_enrichment_soft_overflow_ratio=float(onboarding_knowledge.get('architecture_enrichment_soft_overflow_ratio', 1.10)),
        onboarding_architecture_doc_max_chars=int(onboarding_knowledge.get('architecture_doc_max_chars', 18000)),
        onboarding_architecture_doc_min_chars=int(onboarding_knowledge.get('architecture_doc_min_chars', 5000)),
        onboarding_architecture_doc_min_ratio=float(onboarding_knowledge.get('architecture_doc_min_ratio', 0.30)),
        onboarding_architecture_enrichment_max_modules=int(onboarding_knowledge.get('architecture_enrichment_max_modules', 200)),
        onboarding_architecture_enrichment_max_symbols=int(onboarding_knowledge.get('architecture_enrichment_max_symbols', 500)),
        onboarding_architecture_enrichment_docstring_chars=int(onboarding_knowledge.get('architecture_enrichment_docstring_chars', 160)),
        onboarding_architecture_enrichment_json_indent=int(onboarding_knowledge.get('architecture_enrichment_json_indent', 0)),
        onboarding_architecture_enrichment_num_predict=int(onboarding_knowledge.get('architecture_enrichment_num_predict', 4096)),
    )
