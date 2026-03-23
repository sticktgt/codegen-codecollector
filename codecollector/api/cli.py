from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from codecollector.config import load_config
from codecollector.domain.models import ChangeRequest, PatchArtifact, PipelineRunResult
from codecollector.logger import configure_logging, get_logger
from codecollector.orchestration.services import ProjectServices

LOGGER = get_logger(__name__)
PATCH_OPERATIONS = ('replace_symbol', 'insert_after_symbol', 'add_symbol')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='codecollector')
    subparsers = parser.add_subparsers(dest='command', required=True)

    index_parser = subparsers.add_parser('index')
    index_sub = index_parser.add_subparsers(dest='index_command', required=True)
    index_build = index_sub.add_parser('build')
    index_build.add_argument('--project', required=True)
    index_build.add_argument('--full', action='store_true')

    search_parser = subparsers.add_parser('search')
    search_parser.add_argument('--project', required=True)
    search_parser.add_argument('--query', required=True)
    search_parser.add_argument('--limit', type=int)
    search_parser.add_argument('--disable-vector-search', action='store_true')

    context_parser = subparsers.add_parser('context')
    context_parser.add_argument('--project', required=True)
    context_parser.add_argument('--qualname', required=True)

    apply_parser = subparsers.add_parser('apply')
    apply_parser.add_argument('--project', required=True)
    apply_parser.add_argument('--qualname', required=True)
    apply_parser.add_argument('--artifact-file', required=True)
    apply_parser.add_argument('--operation', choices=PATCH_OPERATIONS, default='replace_symbol')

    pipeline_parser = subparsers.add_parser('pipeline')
    pipeline_sub = pipeline_parser.add_subparsers(dest='pipeline_command', required=True)
    pipeline_replay = pipeline_sub.add_parser('replay')
    pipeline_replay.add_argument('--project', required=True)
    pipeline_replay.add_argument('--selected-qualname', required=True)
    pipeline_replay.add_argument('--artifact-file', required=True)
    pipeline_replay.add_argument('--operation', choices=PATCH_OPERATIONS, default='replace_symbol')
    pipeline_replay.add_argument('--limit', type=int)
    pipeline_replay.add_argument('--change-request-file')
    pipeline_replay.add_argument('--title')
    pipeline_replay.add_argument('--description')
    pipeline_replay.add_argument('--constraint', action='append', default=[])
    pipeline_replay.add_argument('--note', action='append', default=[])
    pipeline_replay.add_argument('--disable-vector-search', action='store_true')

    pipeline_generate = pipeline_sub.add_parser('generate')
    pipeline_generate.add_argument('--project', required=True)
    pipeline_generate.add_argument('--selected-qualname', required=True)
    pipeline_generate.add_argument('--limit', type=int)
    pipeline_generate.add_argument('--change-request-file')
    pipeline_generate.add_argument('--title')
    pipeline_generate.add_argument('--description')
    pipeline_generate.add_argument('--constraint', action='append', default=[])
    pipeline_generate.add_argument('--note', action='append', default=[])
    pipeline_generate.add_argument('--disable-vector-search', action='store_true')

    ui_parser = subparsers.add_parser('ui')
    ui_parser.add_argument('--server-port', type=int)
    ui_parser.add_argument('--server-address')

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_config()
    configure_logging(config.log_level, config.log_format)

    try:
        if args.command == 'ui':
            launch_streamlit_ui(config.root_path, server_port=args.server_port, server_address=args.server_address)
            return

        services = ProjectServices(Path(args.project), config=config)
        if args.command == 'index' and args.index_command == 'build':
            report = services.build_index(full_rebuild=args.full)
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            return

        if args.command == 'search':
            limit = args.limit or config.search_default_limit
            candidates = services.search(args.query, limit=limit, use_vector_search=not args.disable_vector_search)
            print(json.dumps([asdict(candidate) for candidate in candidates], ensure_ascii=False, indent=2))
            return

        if args.command == 'context':
            context_pack = services.context(args.qualname)
            payload = {
                'target': asdict(context_pack.target),
                'neighbors': [asdict(item) for item in context_pack.neighbors],
                'inbound_relations': [asdict(item) for item in context_pack.inbound_relations],
                'outbound_relations': [asdict(item) for item in context_pack.outbound_relations],
                'related_tests': [asdict(item) for item in context_pack.related_tests],
                'recommended_tests': context_pack.recommended_tests,
                'knowledge_title': context_pack.knowledge_title,
                'knowledge_description': context_pack.knowledge_description,
                'requirement_ids': context_pack.requirement_ids,
                'requirement_titles': context_pack.requirement_titles,
                'reference_artifacts': [asdict(item) for item in context_pack.reference_artifacts],
                'reference_summary': context_pack.reference_summary,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if args.command == 'apply':
            replacement_code = Path(args.artifact_file).read_text(encoding='utf-8')
            artifact = PatchArtifact(target_qualname=args.qualname, replacement_code=replacement_code, operation=args.operation)
            result = services.apply(artifact)
            payload = {
                'workspace_path': str(result.workspace_path),
                'artifact': asdict(result.artifact),
                'diff': asdict(result.diff),
                'validation': {
                    'is_valid': result.validation.is_valid,
                    'issues': [asdict(item) for item in result.validation.issues],
                },
                'reindexed': result.reindexed,
                'impact': asdict(result.impact),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        if args.command == 'pipeline' and args.pipeline_command == 'replay':
            change_request = _load_change_request(args, Path(args.project).name)
            result = services.pipeline_replay(
                change_request=change_request,
                selected_target=args.selected_qualname,
                artifact_file=Path(args.artifact_file),
                operation=args.operation,
                limit=args.limit,
                use_vector_search=not args.disable_vector_search,
            )
            print(json.dumps(_pipeline_payload(result), ensure_ascii=False, indent=2))
            return

        if args.command == 'pipeline' and args.pipeline_command == 'generate':
            change_request = _load_change_request(args, Path(args.project).name)
            result = services.pipeline_generate(
                change_request=change_request,
                selected_target=args.selected_qualname,
                limit=args.limit,
                use_vector_search=not args.disable_vector_search,
            )
            print(json.dumps(_pipeline_payload(result), ensure_ascii=False, indent=2))
            return
    except Exception as exc:
        LOGGER.exception('CLI command failed: %s', exc)
        print(json.dumps({'error': str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1) from exc


def _load_change_request(args: argparse.Namespace, project_name: str) -> ChangeRequest:
    if args.change_request_file:
        payload = _load_structured_payload(Path(args.change_request_file))
        if 'change_request' in payload and isinstance(payload['change_request'], dict):
            payload = payload['change_request']
        title = str(payload.get('title', '')).strip()
        description = str(payload.get('description', '')).strip()
        if not title or not description:
            raise ValueError('change_request_file must contain title and description')
        constraints = [str(item) for item in payload.get('constraints', []) or []]
        notes = [str(item) for item in payload.get('notes', []) or []]
        project = str(payload.get('project', project_name))
        return ChangeRequest(title=title, description=description, constraints=constraints, notes=notes, project=project)

    if not args.title or not args.description:
        raise ValueError('Either --change-request-file or both --title/--description must be provided')
    return ChangeRequest(
        title=args.title,
        description=args.description,
        constraints=[str(item) for item in args.constraint],
        notes=[str(item) for item in args.note],
        project=project_name,
    )


def _load_structured_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding='utf-8')
    if path.suffix.lower() == '.json':
        payload = json.loads(text)
    else:
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f'Unsupported structured payload in {path}')
    return payload


def _pipeline_payload(result: PipelineRunResult) -> dict:
    return {
        'run_id': result.run_id,
        'run_label': result.run_label,
        'run_dir': str(result.run_dir),
        'change_request': asdict(result.change_request),
        'selected_target': result.selected_target,
        'build_report': result.build_report,
        'steps': [asdict(item) for item in result.steps],
        'search_candidates': [asdict(item) for item in result.search_candidates],
        'context_pack': {
            'target': asdict(result.context_pack.target),
            'related_tests': [asdict(item) for item in result.context_pack.related_tests],
            'recommended_tests': result.context_pack.recommended_tests,
            'knowledge_title': result.context_pack.knowledge_title,
            'knowledge_description': result.context_pack.knowledge_description,
            'requirement_ids': result.context_pack.requirement_ids,
            'inbound_relations': [asdict(item) for item in result.context_pack.inbound_relations],
            'outbound_relations': [asdict(item) for item in result.context_pack.outbound_relations],
            'reference_summary': result.context_pack.reference_summary,
            'reference_artifacts': [asdict(item) for item in result.context_pack.reference_artifacts],
        },
        'generation_replay': asdict(result.generation_replay) if result.generation_replay else None,
        'external_generation': asdict(result.external_generation) if result.external_generation else None,
        'generated_test_apply': result.generated_test_apply,
        'verification_report': result.verification_report,
        'repair_generation': asdict(result.repair_generation) if result.repair_generation else None,
        'apply_result': {
            'workspace_path': str(result.apply_result.workspace_path),
            'diff': asdict(result.apply_result.diff),
            'validation': {
                'is_valid': result.apply_result.validation.is_valid,
                'issues': [asdict(item) for item in result.apply_result.validation.issues],
            },
            'impact': asdict(result.apply_result.impact),
        },
        'merge_plan': asdict(result.merge_plan),
    }


def launch_streamlit_ui(root_path: Path, server_port: int | None = None, server_address: str | None = None) -> None:
    app_path = root_path / 'codecollector' / 'ui' / 'streamlit_app.py'
    command = [sys.executable, '-m', 'streamlit', 'run', str(app_path)]
    if server_port is not None:
        command.extend(['--server.port', str(server_port)])
    if server_address:
        command.extend(['--server.address', server_address])
    LOGGER.info('Launching Streamlit UI: %s', ' '.join(command))
    raise SystemExit(subprocess.call(command))


if __name__ == '__main__':
    main()
