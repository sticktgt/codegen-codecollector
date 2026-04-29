from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.domain.models import ChangeRequest, ContextPack, PatchArtifact
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)



def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _select_related_tests(context_pack: ContextPack, limit: int = 1) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    total_chars = 0

    def sort_key(item):
        source = item.source_code or ''
        is_module_level = item.qualname == item.file_path.replace('/', '.')[:-3] if item.file_path.endswith('.py') else False
        return (is_module_level, len(source), item.qualname)

    for item in sorted(context_pack.related_tests, key=sort_key):
        source = item.source_code or ''
        selected.append({
            'qualname': item.qualname,
            'file_path': item.file_path,
            'source': source,
            'truncated': False,
        })
        total_chars += len(source)
        if len(selected) >= max(0, limit):
            break

    return selected, total_chars


@dataclass(slots=True)
class CodeGeneratorCallResult:
    request_path: str
    result_path: str
    command: list[str]
    request_payload: dict[str, Any]
    result_payload: dict[str, Any]
    trace_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None




def build_generation_request(
    project_root: Path,
    change_request: ChangeRequest,
    target_qualname: str,
    context_pack: ContextPack,
    config: AppConfig,
    generated_code_artifact: dict[str, Any] | None = None,
    mode: str = 'generate',
    operation: str = 'replace_symbol',
) -> dict[str, Any]:
    target = context_pack.target
    full_file_source = (project_root / target.file_path).read_text(encoding='utf-8')
    full_file_included = False
    if mode == 'generate_test':
        full_file_included = bool(config.codegenerator_include_full_file_for_generate_test)
    elif target.kind not in {'function', 'method'}:
        full_file_included = bool(config.codegenerator_include_full_file_for_non_symbol_targets)
    if not full_file_included:
        full_file_source = ''

    target_source = target.source_code
    target_truncated = False

    related_tests: list[dict[str, Any]] = []
    related_test_chars = 0
    reference_artifacts: list[dict[str, Any]] = []
    reference_chars = 0

    related_test_limits = {
        'generate': config.codegenerator_generate_related_tests_max_items,
        'generate_test': config.codegenerator_generate_test_related_tests_max_items,
        'repair': config.codegenerator_repair_related_tests_max_items,
    }
    reference_limits = {
        'generate': config.codegenerator_generate_reference_max_items,
        'generate_test': config.codegenerator_generate_test_reference_max_items,
        'repair': config.codegenerator_repair_reference_max_items,
    }

    related_test_limit = max(0, int(related_test_limits.get(mode, 0) or 0))
    if related_test_limit > 0:
        related_tests, related_test_chars = _select_related_tests(
            context_pack,
            limit=related_test_limit,
        )

    selected_reference_items = list(context_pack.reference_artifacts[:max(0, int(reference_limits.get(mode, 0) or 0))])
    LOGGER.info(
        'Reference artifact candidates: count=%s items=%s',
        len(selected_reference_items),
        [
            {
                'artifact_id': item.artifact_id,
                'title': item.title,
                'content_chars': len(item.content or ''),
                'usage_mode': item.usage_mode,
                'content_mode': item.content_mode,
            }
            for item in selected_reference_items
        ],
    )

    include_reference = mode in {'generate', 'generate_test'}
    if include_reference:
        for item in selected_reference_items:
            content = item.content
            content_chars = len(content)
            reference_artifacts.append({
                'artifact_id': item.artifact_id,
                'title': item.title,
                'artifact_type': item.artifact_type,
                'usage_mode': item.usage_mode,
                'content_mode': item.content_mode,
                'why_selected': item.why_selected,
                'source_path': item.source_path,
                'content': content,
                'selected_span': item.selected_span,
                'truncated': False,
            })
            reference_chars += content_chars
            LOGGER.info(
                'Selected reference artifact %s content_chars=%s running_total_chars=%s',
                item.artifact_id,
                content_chars,
                reference_chars,
            )
    else:
        if selected_reference_items:
            LOGGER.info(
                'Skipping reference artifacts for mode=%s by structural policy',
                mode,
            )

    if not reference_artifacts and mode == 'generate':
        LOGGER.info('No reference artifacts selected for request payload')
    if not related_tests:
        LOGGER.info('No related tests selected for request payload')

    estimated_context_chars = len(target_source) + related_test_chars + reference_chars + len(full_file_source)
    LOGGER.info(
        'Context assembly mode=%s estimated_context_chars=%s related_test_limit=%s reference_limit=%s related_test_chars=%s reference_chars=%s full_file_chars=%s',
        mode,
        estimated_context_chars,
        related_test_limit,
        max(0, int(reference_limits.get(mode, 0) or 0)),
        related_test_chars,
        reference_chars,
        len(full_file_source),
    )

    project_context = {
        'module_outline': [
            {
                'qualname': item.qualname,
                'kind': item.kind,
                'name': item.name,
                'docstring': item.docstring,
            }
            for item in context_pack.neighbors
        ],
        'full_file_source': full_file_source,
        'target_symbol': {
            'qualname': target.qualname,
            'name': target.name,
            'kind': target.kind,
            'docstring': target.docstring,
            'source': target_source,
            'truncated': target_truncated,
        },
        'related_tests': related_tests,
        'recommended_tests': list(context_pack.recommended_tests),
    }
    context_pack.reference_summary = {
        'count': len(reference_artifacts),
        'titles': [item.get('title', '') for item in reference_artifacts],
        'content_modes': [item.get('content_mode', '') for item in reference_artifacts],
    }
    context_pack.reference_artifacts = list(selected_reference_items[:len(reference_artifacts)])
    reference_context = {
        'reference_artifacts': reference_artifacts,
    }
    normalized_operation = _validate_operation(operation)

    request = {
        'request_id': f'generate-{target_qualname.split(".")[-1]}',
        'mode': mode,
        'change_request': {
            'title': change_request.title,
            'description': change_request.description,
            'constraints': list(change_request.constraints),
            'notes': list(change_request.notes),
        },
        'target': {
            'qualname': target_qualname,
            'file_path': target.file_path,
            'operation': normalized_operation,
        },
        'project_context': project_context,
        'reference_context': reference_context,
        'generated_code_artifact': generated_code_artifact or {},
        'options': {
            'generate_test_mode': config.codegenerator_test_generation_mode,
        },
    }
    metrics = {
        'target_source_chars': len(target_source),
        'target_source_truncated': target_truncated,
        'full_file_chars': len(full_file_source),
        'full_file_included': full_file_included,
        'related_tests_count': len(related_tests),
        'related_test_chars': related_test_chars,
        'reference_artifacts_count': len(reference_artifacts),
        'reference_chars': reference_chars,
        'request_chars': _json_size(request),
        'estimated_context_chars': estimated_context_chars,
        'related_test_limit': related_test_limit,
        'reference_limit': max(0, int(reference_limits.get(mode, 0) or 0)),
    }
    LOGGER.info(
        'Prepared generation request: mode=%s request_chars=%s target_source_chars=%s full_file_included=%s full_file_chars=%s related_tests=%s related_test_chars=%s reference_artifacts=%s reference_chars=%s related_test_limit=%s reference_limit=%s estimated_context_chars=%s',
        mode,
        metrics['request_chars'],
        metrics['target_source_chars'],
        full_file_included,
        len(full_file_source),
        len(request['project_context']['related_tests']),
        metrics['related_test_chars'],
        len(request['reference_context']['reference_artifacts']),
        metrics['reference_chars'],
        related_test_limit,
        max(0, int(reference_limits.get(mode, 0) or 0)),
        estimated_context_chars,
    )
    return request



def invoke_generate(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    if request_format not in {'json', 'yaml'}:
        raise ValueError(f'Unsupported codegenerator request format: {request_format}')
    request_path = run_dir / f'generation_request.{request_format}'
    result_path = run_dir / 'generation_result.json'
    stdout_path = run_dir / 'codegenerator_stdout.txt'
    stderr_path = run_dir / 'codegenerator_stderr.txt'
    _write_payload(request_path, request_payload, request_format)
    command = [
        config.codegenerator_python,
        '-m',
        'codegenerator',
        'generate',
        '--request-file',
        str(request_path),
        '--config',
        str((codegen_root / config.codegenerator_config_path).resolve()),
    ]
    LOGGER.info('Invoking codegenerator: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if stderr.strip():
        LOGGER.info('codegenerator stderr saved to %s', stderr_path)
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator generate failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator generate returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(
        request_path=str(request_path),
        result_path=str(result_path),
        command=command,
        request_payload=request_payload,
        result_payload=result_payload,
        trace_path=result_payload.get('trace_path'),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )





def invoke_generate_test(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    if request_format not in {'json', 'yaml'}:
        raise ValueError(f'Unsupported codegenerator request format: {request_format}')
    request_path = run_dir / f'generation_test_request.{request_format}'
    result_path = run_dir / 'generation_test_result.json'
    stdout_path = run_dir / 'codegenerator_test_stdout.txt'
    stderr_path = run_dir / 'codegenerator_test_stderr.txt'

    request_payload = dict(request_payload)
    if request_payload.get('mode') != 'generate_test':
        LOGGER.info(
            'Normalizing test-generation request mode from %s to generate_test before sending to codegenerator',
            request_payload.get('mode'),
        )
        request_payload['mode'] = 'generate_test'

    request_chars = _json_size(request_payload)

    LOGGER.info(
        'Prepared generation request for test generation: mode=%s request_chars=%s related_tests=%s reference_artifacts=%s',
        request_payload.get('mode'),
        request_chars,
        len(((request_payload.get('project_context') or {}).get('related_tests') or [])),
        len(((request_payload.get('reference_context') or {}).get('reference_artifacts') or [])),
    )

    _write_payload(request_path, request_payload, request_format)
    command = [
        config.codegenerator_python,
        '-m',
        'codegenerator',
        'generate-test',
        '--request-file',
        str(request_path),
        '--config',
        str((codegen_root / config.codegenerator_config_path).resolve()),
    ]
    LOGGER.info('Invoking codegenerator test generation: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if stderr.strip():
        LOGGER.info('codegenerator test stderr saved to %s', stderr_path)
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator generate-test failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator generate-test returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator generate-test returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(
        request_path=str(request_path),
        result_path=str(result_path),
        command=command,
        request_payload=request_payload,
        result_payload=result_payload,
        trace_path=result_payload.get('trace_path'),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )

def build_repair_request(
    change_request: ChangeRequest,
    target_qualname: str,
    previous_result_payload: dict[str, Any],
    context_pack: ContextPack,
    verification_summary: dict[str, Any],
    config: AppConfig | None = None,
    requested_operation: str = 'replace_symbol',
) -> dict[str, Any]:
    target = context_pack.target
    failure_summary = verification_summary.get('failure_summary', {}) if isinstance(verification_summary, dict) else {}
    stage = str(failure_summary.get('stage', 'verification'))
    summary_text = 'Apply failed before verification' if stage == 'apply' else 'Verification failed after apply'
    error_type = 'apply_failed' if stage == 'apply' else 'verification_failed'
    normalized_operation = _validate_operation(requested_operation)

    related_tests_limit = max(
        0,
        int((config.codegenerator_repair_related_tests_max_items if config is not None else 1) or 0),
    )
    repair_reference_limit = max(
        0,
        int((config.codegenerator_repair_reference_max_items if config is not None else 1) or 0),
    )

    related_tests = _select_related_tests(
        context_pack,
        limit=related_tests_limit,
    )[0]

    selected_reference_artifacts = list(context_pack.reference_artifacts[:repair_reference_limit])

    previous_artifact = dict(previous_result_payload.get('code_artifact') or {})
    previous_artifact['operation'] = normalized_operation
    previous_artifact.setdefault('target_qualname', target_qualname)
    previous_artifact.setdefault('target_file', target.file_path)
    if normalized_operation == 'insert_after_symbol':
        previous_artifact['insert_after'] = previous_artifact.get('insert_after') or target_qualname

    LOGGER.info(
        'Prepared repair request: target=%s requested_operation=%s stage=%s related_tests=%s reference_artifacts=%s previous_artifact_has_code=%s',
        target_qualname,
        normalized_operation,
        stage,
        len(related_tests),
        len(selected_reference_artifacts),
        bool(previous_artifact.get('code')),
    )

    return {
        'request_id': f'repair-{target_qualname.split(".")[-1]}',
        'mode': 'repair',
        'previous_generation_request_id': previous_result_payload.get('request_id', ''),
        'change_request': {
            'title': change_request.title,
            'description': change_request.description,
            'constraints': list(change_request.constraints),
            'notes': list(change_request.notes),
        },
        'error_context': {
            'type': error_type,
            'summary': summary_text,
            'verification_summary': verification_summary,
        },
        'previous_artifact': previous_artifact,
        'project_context': {
            'module_outline': [
                {
                    'qualname': item.qualname,
                    'kind': item.kind,
                    'name': item.name,
                    'docstring': item.docstring,
                }
                for item in context_pack.neighbors
            ],
            'full_file_source': '',
            'target_symbol': {
                'qualname': target.qualname,
                'name': target.name,
                'kind': target.kind,
                'docstring': target.docstring,
                'source': target.source_code,
                'truncated': False,
            },
            'related_tests': related_tests,
            'recommended_tests': list(context_pack.recommended_tests),
        },
        'reference_context': {
            'reference_summary': {
                'count': len(selected_reference_artifacts),
                'titles': [item.title for item in selected_reference_artifacts],
                'content_modes': [item.content_mode for item in selected_reference_artifacts],
            },
            'reference_artifacts': [
                {
                    'artifact_id': item.artifact_id,
                    'title': item.title,
                    'artifact_type': item.artifact_type,
                    'usage_mode': item.usage_mode,
                    'content_mode': item.content_mode,
                    'why_selected': item.why_selected,
                    'source_path': item.source_path,
                    'content': item.content,
                    'selected_span': item.selected_span,
                    'truncated': False,
                }
                for item in selected_reference_artifacts
            ],
        },
        'options': {
            'requested_operation': normalized_operation,
        },
    }


def invoke_repair(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    request_path = run_dir / f'repair_request.{request_format}'
    result_path = run_dir / 'repair_result.json'
    stdout_path = run_dir / 'codegenerator_repair_stdout.txt'
    stderr_path = run_dir / 'codegenerator_repair_stderr.txt'
    _write_payload(request_path, request_payload, request_format)
    command = [config.codegenerator_python, '-m', 'codegenerator', 'repair', '--request-file', str(request_path), '--config', str((codegen_root / config.codegenerator_config_path).resolve())]
    LOGGER.info('Invoking codegenerator repair: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator repair failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator repair returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator repair returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(request_path=str(request_path), result_path=str(result_path), command=command, request_payload=request_payload, result_payload=result_payload, trace_path=result_payload.get('trace_path'), stdout_path=str(stdout_path), stderr_path=str(stderr_path))


def patch_artifact_from_result(result_payload: dict[str, Any], fallback_target_qualname: str) -> PatchArtifact:
    artifact = result_payload.get('code_artifact') or {}
    operation = _validate_operation(str(artifact.get('operation', 'replace_symbol')))
    code = artifact.get('code')
    if not code:
        raise ValueError('codegenerator result does not contain code_artifact.code')
    if result_payload.get('status') != 'ok':
        raise ValueError(f"codegenerator returned non-ok status: {result_payload.get('status')} ({result_payload.get('message')})")
    return PatchArtifact(
        target_qualname=str(artifact.get('target_qualname') or fallback_target_qualname),
        replacement_code=str(code),
        operation=operation,
    )

_ALLOWED_OPERATIONS = {'replace_symbol', 'insert_after_symbol'}

def _validate_operation(operation: str) -> str:
    value = operation.strip().lower()
    if value not in _ALLOWED_OPERATIONS:
        raise ValueError(f'Unsupported patch operation from codegenerator: {operation}')
    return value

def ensure_expected_operation(
    result_payload: dict[str, Any],
    expected_operation: str,
) -> str:
    artifact = result_payload.get('code_artifact') or {}
    actual_operation = _validate_operation(str(artifact.get('operation', 'replace_symbol')))
    expected = _validate_operation(expected_operation)
    if actual_operation != expected:
        raise ValueError(
            f'Generator returned operation {actual_operation}, expected {expected}'
        )
    return actual_operation

def _write_payload(path: Path, payload: dict[str, Any], request_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if request_format == 'yaml':
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
