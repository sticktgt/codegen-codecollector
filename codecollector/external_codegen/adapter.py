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


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = f'\n\n# ... truncated, original_chars={len(text)}'
    allowed = max(0, limit - len(marker))
    return text[:allowed] + marker, True


def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


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
) -> dict[str, Any]:
    target = context_pack.target
    full_file_source = (project_root / target.file_path).read_text(encoding='utf-8')
    full_file_included = target.kind not in {'function', 'method'}
    if full_file_included and len(full_file_source) > config.codegenerator_max_full_file_chars:
        full_file_source = ''
        full_file_included = False
    if not full_file_included:
        full_file_source = ''
    target_source, target_truncated = _truncate_text(target.source_code, max(800, config.codegenerator_max_full_file_chars // 2))
    related_tests = []
    related_test_chars = 0
    for item in context_pack.related_tests[:1]:
        src, truncated = _truncate_text(item.source_code, config.codegenerator_max_related_test_chars)
        related_tests.append({
            'qualname': item.qualname,
            'file_path': item.file_path,
            'source': src,
            'truncated': truncated,
        })
        related_test_chars += len(src)
    selected_reference_items = list(context_pack.reference_artifacts[:1])
    context_pack.reference_artifacts = selected_reference_items
    reference_artifacts = []
    reference_chars = 0
    for item in selected_reference_items:
        content, truncated = _truncate_text(item.content, config.codegenerator_max_reference_chars)
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
            'truncated': truncated,
        })
        reference_chars += len(content)
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
    reference_summary = {
        'count': len(reference_artifacts),
        'titles': [item.get('title', '') for item in reference_artifacts],
        'content_modes': [item.get('content_mode', '') for item in reference_artifacts],
    }
    context_pack.reference_summary = dict(reference_summary)
    context_pack.reference_artifacts = list(selected_reference_items[:len(reference_artifacts)])
    reference_context = {
        'reference_summary': reference_summary,
        'reference_artifacts': reference_artifacts,
    }
    request = {
        'request_id': f'generate-{target_qualname.split(".")[-1]}',
        'mode': 'generate',
        'change_request': {
            'title': change_request.title,
            'description': change_request.description,
            'constraints': list(change_request.constraints),
            'notes': list(change_request.notes),
        },
        'target': {
            'qualname': target_qualname,
            'file_path': target.file_path,
            'operation': 'replace_symbol',
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
    }
    request['context_metrics'] = metrics
    current_size = _json_size(request)
    if current_size > config.codegenerator_max_request_chars and reference_artifacts:
        # drop the second reference artifact first
        request['reference_context']['reference_artifacts'] = reference_artifacts[:1]
        request['reference_context']['reference_summary'] = {
            'count': 1,
            'titles': [item.get('title', '') for item in request['reference_context']['reference_artifacts'][:1]],
            'content_modes': [item.get('content_mode', '') for item in request['reference_context']['reference_artifacts'][:1]],
        }
        context_pack.reference_summary = dict(request['reference_context']['reference_summary'])
        request['context_metrics']['reference_artifacts_count'] = 1
        request['context_metrics']['reference_chars'] = len(request['reference_context']['reference_artifacts'][0]['content']) if request['reference_context']['reference_artifacts'] else 0
        current_size = _json_size(request)
    if current_size > config.codegenerator_max_request_chars and related_tests:
        request['project_context']['related_tests'] = []
        request['context_metrics']['related_tests_count'] = 0
        request['context_metrics']['related_test_chars'] = 0
        current_size = _json_size(request)
    request['context_metrics']['request_chars'] = current_size
    request['context_metrics']['request_chars_limit'] = config.codegenerator_max_request_chars
    LOGGER.info('Prepared generation request: request_chars=%s target_source_chars=%s full_file_included=%s full_file_chars=%s related_tests=%s related_test_chars=%s reference_artifacts=%s reference_chars=%s', current_size, request['context_metrics']['target_source_chars'], full_file_included, len(full_file_source), len(request['project_context']['related_tests']), request['context_metrics']['related_test_chars'], len(request['reference_context']['reference_artifacts']), request['context_metrics']['reference_chars'])
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

def build_repair_request(change_request: ChangeRequest, target_qualname: str, previous_result_payload: dict[str, Any], context_pack: ContextPack, verification_summary: dict[str, Any]) -> dict[str, Any]:
    target = context_pack.target
    failure_summary = verification_summary.get('failure_summary', {}) if isinstance(verification_summary, dict) else {}
    stage = str(failure_summary.get('stage', 'verification'))
    summary_text = 'Apply failed before verification' if stage == 'apply' else 'Verification failed after apply'
    error_type = 'apply_failed' if stage == 'apply' else 'verification_failed'
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
        'previous_artifact': previous_result_payload.get('code_artifact') or {},
        'project_context': {
            'module_outline': [
                {'qualname': item.qualname, 'kind': item.kind, 'name': item.name, 'docstring': item.docstring}
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
            'related_tests': [
                {'qualname': item.qualname, 'file_path': item.file_path, 'source': item.source_code, 'truncated': False}
                for item in context_pack.related_tests[:1]
            ],
            'recommended_tests': list(context_pack.recommended_tests),
        },
        'reference_context': {
            'reference_summary': {'count': min(1, len(context_pack.reference_artifacts)), 'titles': [item.title for item in context_pack.reference_artifacts[:1]], 'content_modes': [item.content_mode for item in context_pack.reference_artifacts[:1]]},
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
                for item in context_pack.reference_artifacts[:1]
            ],
        },
        'options': {},
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
    return CodeGeneratorCallResult(request_path=str(request_path), result_path=str(result_path), command=command, request_payload=request_payload, result_payload=result_payload, trace_path=result_payload.get('trace_path'), stdout_path=str(stdout_path), stderr_path=str(stderr_path))


def patch_artifact_from_result(result_payload: dict[str, Any], fallback_target_qualname: str) -> PatchArtifact:
    artifact = result_payload.get('code_artifact') or {}
    operation = _normalize_operation(str(artifact.get('operation', 'replace_symbol')))
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


def _normalize_operation(operation: str) -> str:
    value = operation.strip().lower()
    mapping = {
        'replace_function': 'replace_symbol',
        'replace_symbol': 'replace_symbol',
        'add_function': 'add_symbol',
        'add_symbol': 'add_symbol',
        'insert_after_function': 'insert_after_symbol',
        'insert_after_symbol': 'insert_after_symbol',
    }
    return mapping.get(value, value)


def _write_payload(path: Path, payload: dict[str, Any], request_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if request_format == 'yaml':
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
