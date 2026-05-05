
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from codecollector.analysis.analyze_service import AnalyzeService
from codecollector.config import load_config
from codecollector.domain.models import ChangeRequest, PatchArtifact, PipelineRunResult
from codecollector.logger import configure_logging, get_logger
from codecollector.onboarding.onboarding_service import OnboardingService
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_service import SessionService
from codecollector.workspace.workspace_service import WorkspaceService
from codecollector.orchestration.pipeline_service import PipelineRunFailed

LOGGER = get_logger(__name__)
PATCH_OPERATIONS = ('replace_symbol', 'insert_after_symbol')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='codecollector')
    subparsers = parser.add_subparsers(dest='command', required=True)

    index_parser = subparsers.add_parser('index')
    index_sub = index_parser.add_subparsers(dest='index_command', required=True)
    index_build = index_sub.add_parser('build')
    index_build.add_argument('--project', required=True)
    index_build.add_argument('--full', action='store_true')

    projects_parser = subparsers.add_parser('projects')
    projects_sub = projects_parser.add_subparsers(dest='projects_command', required=True)

    project_register = projects_sub.add_parser('register')
    project_register.add_argument('--project-root', required=True)
    project_register.add_argument('--project-name')
    project_register.add_argument('--language', action='append', dest='languages', default=[])
    project_register.add_argument('--verification-command', action='append', default=[])
    project_register.add_argument('--index-exclude', action='append', default=[])
    project_register.add_argument('--reference-library-path', action='append', default=[])

    projects_sub.add_parser('list')

    project_get = projects_sub.add_parser('get')
    project_get.add_argument('--project-id', required=True)

    project_delete = projects_sub.add_parser('delete')
    project_delete.add_argument('--project-id', required=True)

    project_onboard = projects_sub.add_parser('onboard')
    project_onboard.add_argument('--project-id', required=True)
    project_onboard.add_argument('--full', action='store_true')

    sessions_parser = subparsers.add_parser('sessions')
    sessions_sub = sessions_parser.add_subparsers(dest='sessions_command', required=True)

    session_analyze = sessions_sub.add_parser('analyze')
    session_analyze.add_argument('--project-id', required=True)
    session_analyze.add_argument('--change-request-file')
    session_analyze.add_argument('--title')
    session_analyze.add_argument('--description')
    session_analyze.add_argument('--constraint', action='append', default=[])
    session_analyze.add_argument('--note', action='append', default=[])
    session_analyze.add_argument('--limit', type=int)
    session_analyze.add_argument('--operation', choices=PATCH_OPERATIONS, default=None)

    sessions_sub.add_parser('list')

    session_finalize = sessions_sub.add_parser('finalize')
    session_finalize.add_argument('--session-id', required=True)
    session_finalize.add_argument('--keep-workspace', action='store_true')

    session_get = sessions_sub.add_parser('get')
    session_get.add_argument('--session-id', required=True)

    session_delete = sessions_sub.add_parser('delete')
    session_delete.add_argument('--session-id', required=True)

    session_select = sessions_sub.add_parser('select-target')
    session_select.add_argument('--session-id', required=True)
    session_select.add_argument('--selected-qualname', required=True)
    session_select.add_argument("--operation", choices=PATCH_OPERATIONS, default=None)

    session_generate = sessions_sub.add_parser('generate')
    session_generate.add_argument('--session-id', required=True)
    session_generate.add_argument('--selected-qualname')
    session_generate.add_argument('--operation', choices=PATCH_OPERATIONS, default=None)
    session_generate.add_argument('--limit', type=int)
    session_generate.add_argument('--disable-vector-search', action='store_true')

    session_repair = sessions_sub.add_parser('repair')
    session_repair.add_argument('--session-id', required=True)
    session_repair.add_argument('--selected-qualname')
    session_repair.add_argument('--note')
    session_repair.add_argument('--disable-vector-search', action='store_true')

    workspaces_parser = subparsers.add_parser('workspaces')
    workspaces_sub = workspaces_parser.add_subparsers(dest='workspaces_command', required=True)

    workspace_get = workspaces_sub.add_parser('get')
    workspace_get.add_argument('--workspace-id', required=True)

    workspace_diff = workspaces_sub.add_parser('diff')
    workspace_diff.add_argument('--workspace-id', required=True)

    workspace_apply = workspaces_sub.add_parser('apply')
    workspace_apply.add_argument('--workspace-id', required=True)

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
    pipeline_generate.add_argument('--operation', choices=PATCH_OPERATIONS, default='replace_symbol')
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

    project_service = ProjectService(config.root_path, config)
    onboarding_service = OnboardingService(config.root_path, config)
    analyze_service = AnalyzeService(config.root_path, config)
    session_service = SessionService(config.root_path, config)
    workspace_service = WorkspaceService(config.root_path, config)

    try:
        if args.command == 'projects' and args.projects_command == 'register':
            project = project_service.register_project(
                project_name=args.project_name or Path(args.project_root).resolve().name,
                project_root=Path(args.project_root),
                languages=args.languages or ['python'],
                verification_commands=[str(item) for item in args.verification_command],
                index_excludes=[str(item) for item in args.index_exclude],
                reference_library_paths=[str(item) for item in args.reference_library_path],
            )
            print(json.dumps(asdict(project), ensure_ascii=False, indent=2))
            return

        if args.command == 'projects' and args.projects_command == 'list':
            print(json.dumps([asdict(item) for item in project_service.list_projects()], ensure_ascii=False, indent=2))
            return

        if args.command == 'projects' and args.projects_command == 'get':
            print(json.dumps(asdict(project_service.get_project(args.project_id)), ensure_ascii=False, indent=2))
            return

        if args.command == 'projects' and args.projects_command == 'delete':
            deleted = project_service.delete_project_registration(args.project_id)
            print(json.dumps({'project_id': args.project_id, 'deleted': deleted}, ensure_ascii=False, indent=2))
            return

        if args.command == 'projects' and args.projects_command == 'onboard':
            result = onboarding_service.onboard_project(args.project_id, full_rebuild=args.full)
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'analyze':
            project = project_service.get_project(args.project_id)
            change_request = _load_change_request(args, project.project_name)
            requirement_payload = {
                'title': change_request.title,
                'description': change_request.description,
                'constraints': list(change_request.constraints),
            }
            result = analyze_service.analyze(
                project_id=args.project_id,
                input_requirements=[requirement_payload],
                requested_operation=args.operation,
                limit=args.limit or config.search_default_limit,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'list':
            print(json.dumps(session_service.list_sessions(), ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'get':
            print(json.dumps(session_service.get_session(args.session_id), ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'delete':
            deleted = session_service.delete_session(args.session_id)
            print(json.dumps({'session_id': args.session_id, 'deleted': deleted}, ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'finalize':
            result = session_service.finalize(args.session_id, delete_workspace=not args.keep_workspace)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'select-target':
            result = session_service.select_target(args.session_id, args.selected_qualname, requested_operation=args.operation)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'generate':
            LOGGER.info(
                "CLI sessions generate: session_id=%s selected_qualname=%s operation=%s",
                args.session_id,
                args.selected_qualname,
                args.operation,
            )
            result = session_service.generate(
                session_id=args.session_id,
                selected_target=args.selected_qualname,
                requested_operation=args.operation,
                limit=args.limit or config.search_default_limit,
                use_vector_search=not args.disable_vector_search,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        if args.command == 'sessions' and args.sessions_command == 'repair':
            result = session_service.repair(
                session_id=args.session_id,
                selected_target=args.selected_qualname,
                note=args.note,
                use_vector_search=not args.disable_vector_search,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        if args.command == 'workspaces' and args.workspaces_command == 'get':
            print(json.dumps(workspace_service.get_workspace(args.workspace_id), ensure_ascii=False, indent=2))
            return

        if args.command == 'workspaces' and args.workspaces_command == 'diff':
            print(json.dumps(workspace_service.diff_workspace(args.workspace_id), ensure_ascii=False, indent=2))
            return

        if args.command == 'workspaces' and args.workspaces_command == 'apply':
            print(json.dumps(workspace_service.apply_workspace(args.workspace_id), ensure_ascii=False, indent=2))
            return

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
            try:
                result = services.pipeline_generate(
                    change_request=change_request,
                    selected_target=args.selected_qualname,
                    requested_operation=args.operation,
                    limit=args.limit,
                    use_vector_search=not args.disable_vector_search,
                )
                print(json.dumps(_pipeline_payload(result), ensure_ascii=False, indent=2))
            except PipelineRunFailed as exc:
                print(json.dumps({
                    'status': 'failed',
                    'message': str(exc),
                    'pipeline_result': _pipeline_payload(exc.result),
                }, ensure_ascii=False, indent=2))
                raise SystemExit(1) from exc
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

def _json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value

def _pipeline_payload(result: PipelineRunResult) -> dict:
    context_pack_payload = None
    if result.context_pack is not None:
        context_pack_payload = {
            'target': asdict(result.context_pack.target) if result.context_pack.target is not None else None,
            'related_tests': [asdict(item) for item in (result.context_pack.related_tests or [])],
            'recommended_tests': list(result.context_pack.recommended_tests or []),
            'knowledge_title': result.context_pack.knowledge_title,
            'knowledge_description': result.context_pack.knowledge_description,
            'requirement_ids': list(result.context_pack.requirement_ids or []),
            'requirement_titles': list(result.context_pack.requirement_titles or []),
            'reference_summary': result.context_pack.reference_summary or {},
            'reference_artifacts': [asdict(item) for item in (result.context_pack.reference_artifacts or [])],
        }

    payload = {
        'run_id': result.run_id,
        'run_label': result.run_label,
        'run_dir': str(result.run_dir),
        'change_request': asdict(result.change_request),
        'selected_target': result.selected_target,
        'build_report': result.build_report,
        'steps': [asdict(step) for step in result.steps],
        'search_candidates': [asdict(item) for item in (result.search_candidates or [])],
        'context_pack': context_pack_payload,
        'generation_replay': asdict(result.generation_replay) if result.generation_replay else None,
        'external_code_generation': asdict(result.external_code_generation) if result.external_code_generation else None,
        'external_test_generation': asdict(result.external_test_generation) if result.external_test_generation else None,
        'generated_test_apply': result.generated_test_apply,
        'verification_report': result.verification_report,
        'repair_generation': asdict(result.repair_generation) if result.repair_generation else None,
        'apply_result': asdict(result.apply_result) if result.apply_result else None,
        'merge_plan': asdict(result.merge_plan) if result.merge_plan else None,
        'warnings': list(result.warnings or []),
    }
    return _json_ready(payload)


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
