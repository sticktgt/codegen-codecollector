
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.domain.models import ChangeRequest, PipelineRunResult
from codecollector.logger import get_logger
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_registry import SessionRegistry
from codecollector.state.ids import new_session_id

LOGGER = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.registry = SessionRegistry(self.tool_root, self.config)
        self.project_service = ProjectService(self.tool_root, self.config)


    def create_session(self, *, project_id: str, input_requirements: list[dict[str, Any]]) -> dict[str, Any]:
        self.project_service.get_project(project_id)
        session_id = new_session_id()
        now = _utc_now()
        payload: dict[str, Any] = {
            "session_id": session_id,
            "project_id": project_id,
            "input_requirements": input_requirements,
            "status": "created",
            "created_at": now,
            "updated_at": now,
            "run_ids": [],
            "workspace_ids": [],
            "recommended_target": None,
            "selected_target": None,
        }
        self.registry.save(payload)
        return payload

    def mark_analyzed(self, session_id: str, *, recommended_target: str | None) -> dict[str, Any]:
        payload = self.registry.get(session_id)
        payload["recommended_target"] = recommended_target
        payload["status"] = "analyzed"
        payload["updated_at"] = _utc_now()
        self.registry.save(payload)
        return payload

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.registry.list()

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.registry.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.registry.delete(session_id)

    def select_target(self, session_id: str, selected_qualname: str) -> dict[str, Any]:
        payload = self.registry.get(session_id)
        payload['selected_target'] = selected_qualname
        payload['status'] = 'target_selected'
        payload['updated_at'] = _utc_now()
        self.registry.save(payload)
        return payload

    def resolve_target(self, session_id: str, selected_target: str | None = None) -> tuple[str, str]:
        payload = self.registry.get(session_id)
        if selected_target:
            return selected_target, 'request'
        session_selected = str(payload.get('selected_target') or '').strip()
        if session_selected:
            return session_selected, 'session_selected'
        recommended = str(payload.get('recommended_target') or '').strip()
        if recommended:
            return recommended, 'session_recommended'
        raise ValueError('No selected_target provided and no recommended target stored in session')

    def generate(
        self,
        session_id: str,
        selected_target: str | None = None,
        limit: int | None = None,
        use_vector_search: bool | None = None,
    ) -> dict[str, Any]:
        session_payload = self.registry.get(session_id)
        project = self.project_service.get_project(str(session_payload['project_id']))
        resolved_target, target_source = self.resolve_target(session_id, selected_target)

        requirements = session_payload.get('input_requirements') or []
        if not requirements:
            raise ValueError(f'Session {session_id} does not contain input requirements')
        first_req = requirements[0]
        change_request = ChangeRequest(
            title=str(first_req.get('title', '')).strip(),
            description=str(first_req.get('description', '')).strip(),
            constraints=[str(item) for item in first_req.get('constraints', []) or []],
            notes=[str(item) for item in first_req.get('notes', []) or []],
            project=str(project.project_name),
        )
        if not change_request.title or not change_request.description:
            raise ValueError(f'Session {session_id} contains invalid change request payload')

        services = ProjectServices(Path(project.project_root), config=self.config)
        result = services.pipeline_generate(
            change_request=change_request,
            selected_target=resolved_target,
            limit=limit or self.config.search_default_limit,
            use_vector_search=use_vector_search,
            skip_search=target_source in {'request', 'session_selected'},
        )

        run_id = result.run_id
        workspace_path = str(result.apply_result.workspace_path) if result.apply_result else ''
        workspace_id = Path(workspace_path).name if workspace_path else ''

        run_ids = [str(item) for item in session_payload.get('run_ids', [])]
        if run_id not in run_ids:
            run_ids.append(run_id)
        workspace_ids = [str(item) for item in session_payload.get('workspace_ids', [])]
        if workspace_id and workspace_id not in workspace_ids:
            workspace_ids.append(workspace_id)

        session_payload['selected_target'] = resolved_target
        session_payload['status'] = 'generated'
        session_payload['updated_at'] = _utc_now()
        session_payload['run_ids'] = run_ids
        session_payload['workspace_ids'] = workspace_ids
        session_payload['last_run_id'] = run_id
        if workspace_id:
            session_payload['last_workspace_id'] = workspace_id
        self.registry.save(session_payload)

        return {
            'session_id': session_id,
            'project_id': session_payload['project_id'],
            'selected_target': resolved_target,
            'selected_target_source': target_source,
            'run_id': run_id,
            'workspace_id': workspace_id or None,
            'workspace_path': workspace_path or None,
            'session': session_payload,
            'pipeline_result': self._pipeline_payload(result),
        }


    def finalize(self, session_id: str, *, delete_workspace: bool = True) -> dict[str, Any]:
        session_payload = self.registry.get(session_id)
        workspace_id = str(session_payload.get('last_workspace_id') or '').strip()
        deleted_workspace_id: str | None = None
        if delete_workspace and workspace_id:
            from codecollector.workspace.workspace_service import WorkspaceService
            try:
                WorkspaceService(self.tool_root, self.config).delete_workspace(workspace_id)
                deleted_workspace_id = workspace_id
            except FileNotFoundError:
                deleted_workspace_id = workspace_id
            session_payload['workspace_ids'] = [str(item) for item in session_payload.get('workspace_ids', []) if str(item) != workspace_id]
            session_payload['last_workspace_id'] = None
        session_payload['status'] = 'finalized'
        session_payload['updated_at'] = _utc_now()
        self.registry.save(session_payload)
        return {
            'session_id': session_id,
            'status': session_payload['status'],
            'deleted_workspace_id': deleted_workspace_id,
            'session': session_payload,
        }

    def _pipeline_payload(self, result: PipelineRunResult) -> dict[str, Any]:
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
            } if result.context_pack else None,
            'external_code_generation': asdict(result.external_code_generation) if result.external_code_generation else None,
            'external_test_generation': asdict(result.external_test_generation) if result.external_test_generation else None,
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
            } if result.apply_result else None,
            'merge_plan': asdict(result.merge_plan) if result.merge_plan else None,
        }
