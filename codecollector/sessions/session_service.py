
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.domain.models import (
    ChangeRequest,
    GenerateApiResultSummary,
    PipelineRunResult,
    SelectTargetApiResultSummary,
    VerificationBlock,
    VerificationIssue,
    VerificationReport,
)
from codecollector.logger import get_logger
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_registry import SessionRegistry
from codecollector.state.ids import new_session_id

from codecollector.orchestration.pipeline_service import PipelineRunFailed

import json

LOGGER = get_logger(__name__)
INSERT_SCOPES = {'module_body', 'class_body'}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.registry = SessionRegistry(self.tool_root, self.config)
        self.project_service = ProjectService(self.tool_root, self.config)

    def _repair_outcome(self, result: PipelineRunResult) -> str:
        verification = result.verification_report
        if verification and verification.passed:
            return 'repaired'

        if verification:
            for block in verification.blocks:
                if block.name == 'repair_intent' and not block.ok:
                    return 'no_effective_change'

        if verification:
            return verification.verdict
        return 'verification_failed'

    def _run_bundle_path(self, run_id: str) -> Path:
        run_dir = (self.tool_root / self.config.runs_root_dirname / run_id).resolve()
        candidates = sorted(run_dir.glob('pipeline_run_*.json'))
        if not candidates:
            raise FileNotFoundError(f'Run bundle not found for run_id={run_id}')
        return candidates[0]

    def _load_run_bundle(self, run_id: str) -> dict[str, Any]:
        return json.loads(self._run_bundle_path(run_id).read_text(encoding='utf-8'))

    def _select_previous_result_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        repair_generation = bundle.get('repair_generation') or {}
        repair_payload = repair_generation.get('result_payload') or {}
        if (repair_payload.get('code_artifact') or {}).get('code'):
            return repair_payload
        external = bundle.get('external_code_generation') or {}
        external_payload = external.get('result_payload') or {}
        if (external_payload.get('code_artifact') or {}).get('code'):
            return external_payload
        raise ValueError('Previous run does not contain a reusable code artifact for repair')

    def _build_manual_repair_report(self, note: str | None = None) -> VerificationReport:
        message = (note or 'Пользователь запросил дополнительный repair после review.').strip()
        return VerificationReport(
           verdict='verification_failed',
            passed=False,
            blocks=[
                VerificationBlock(
                    name='manual_repair_request',
                    ok=False,
                    severity='error',
                    issues=[
                        VerificationIssue(
                            code='manual_repair_request',
                            message=message,
                            severity='error',
                            file_path=None,
                            symbol=None,
                        )
                    ],
                    details={},
                )
            ],
            summary={
                'production_failed': True,
                'generated_test_failed': False,
            },
       )

    def create_session(
        self,
        *,
        project_id: str,
        input_requirements: list[dict[str, Any]],
        requested_operation: str = 'replace_symbol',
    ) -> dict[str, Any]:
        self.project_service.get_project(project_id)
        session_id = new_session_id()
        now = _utc_now()
        payload: dict[str, Any] = {
            "session_id": session_id,
            "project_id": project_id,
            "input_requirements": input_requirements,
            "requested_operation": requested_operation,
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

    def mark_analyzed(
        self,
        session_id: str,
        *,
        recommended_target: str | None,
        requested_operation: str | None = None,
        analysis_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self.registry.get(session_id)
        payload["recommended_target"] = recommended_target
        if requested_operation:
            payload["requested_operation"] = requested_operation
        if analysis_metadata:
            payload["analysis"] = analysis_metadata
        payload["status"] = "needs_user_decision" if bool((analysis_metadata or {}).get("manual_review_required")) else "analyzed"
        payload["updated_at"] = _utc_now()
        self.registry.save(payload)
        return payload

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.registry.list()

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self.registry.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        return self.registry.delete(session_id)

    def _request_quality_status(self, session_payload: dict[str, Any]) -> str:
        analysis = session_payload.get('analysis') or {}
        if not isinstance(analysis, dict):
            return ''

        request_quality = analysis.get('request_quality') or {}
        if not isinstance(request_quality, dict):
            return ''

        return str(request_quality.get('status') or '').strip().lower()

    def _is_insufficient_request(self, session_payload: dict[str, Any]) -> bool:
        return self._request_quality_status(session_payload) == 'insufficient'

    def _normalize_insert_scope(self, value: str | None) -> str | None:
        scope = str(value or '').strip()
        return scope if scope in INSERT_SCOPES else None

    def _target_symbol_payload(self, session_payload: dict[str, Any], selected_target: str | None) -> dict[str, Any]:
        project_id = str(session_payload.get('project_id') or '').strip()
        if not project_id or not selected_target:
            return {}
        project = self.project_service.get_project(project_id)
        services = ProjectServices(Path(project.project_root), config=self.config)
        symbol = services.store.get_symbol(str(services.project_root), selected_target)
        if symbol is None:
            return {}
        return asdict(symbol)

    def _build_blocked_generation_payload(
        self,
        session_payload: dict[str, Any],
        *,
        block_reason: str,
        message: str,
        recommended_action: str,
        selected_target: str | None = None,
        selected_target_source: str | None = None,
        requested_operation: str | None = None,
        insert_scope: str | None = None,
        target_symbol: dict[str, Any] | None = None,
        missing_information: list[Any] | None = None,
    ) -> dict[str, Any]:
        session_id = str(session_payload.get('session_id') or '').strip()
        project_id = str(session_payload.get('project_id') or '').strip()
        status = str(session_payload.get('status') or '').strip() or 'needs_user_decision'
        request_quality_status = self._request_quality_status(session_payload) or None
        return {
            'session_id': session_id,
            'project_id': project_id,
            'selected_target': selected_target if selected_target is not None else session_payload.get('selected_target'),
            'selected_target_source': selected_target_source,
            'run_id': None,
            'workspace_id': None,
            'workspace_path': None,
            'session': session_payload,
            'pipeline_result': None,
            'result_summary': {
                'status': status,
                'generation_blocked': True,
                'block_reason': block_reason,
                'message': message,
                'request_quality_status': request_quality_status,
                'missing_information': list(missing_information or []),
                'recommended_action': recommended_action,
                'selected_target': selected_target if selected_target is not None else session_payload.get('selected_target'),
                'requested_operation': requested_operation if requested_operation is not None else session_payload.get('requested_operation'),
                'insert_scope': insert_scope if insert_scope is not None else session_payload.get('insert_scope'),
                'target_symbol': target_symbol or {},
            },
            'requested_operation': requested_operation if requested_operation is not None else session_payload.get('requested_operation'),
            'insert_scope': insert_scope if insert_scope is not None else session_payload.get('insert_scope'),
        }

    def _build_generate_blocked_result(self, session_payload: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(session_payload.get('session_id') or '').strip()
        project_id = str(session_payload.get('project_id') or '').strip()
        status = str(session_payload.get('status') or '').strip()

        analysis = session_payload.get('analysis') or {}
        request_quality = analysis.get('request_quality') or {}
        request_quality_status = self._request_quality_status(session_payload)

        if self._is_insufficient_request(session_payload):
            missing_information = request_quality.get('missing_information') or []
            message = (
                'Запрос недостаточно конкретен для генерации. '
                'Переформулируйте запрос и запустите analyze заново.'
            )

            return {
                'session_id': session_id,
                'project_id': project_id,
                'selected_target': session_payload.get('selected_target'),
                'selected_target_source': 'session_selected'
                if session_payload.get('selected_target')
                else None,
                'run_id': None,
                'workspace_id': None,
                'workspace_path': None,
                'session': session_payload,
                'pipeline_result': None,
                'result_summary': {
                    'status': 'needs_user_decision',
                    'generation_blocked': True,
                    'block_reason': 'insufficient_request',
                    'message': message,
                    'request_quality_status': request_quality_status,
                    'missing_information': missing_information,
                    'recommended_action': 'rewrite_request_and_run_analyze_again',
                    'selected_target': session_payload.get('selected_target'),
                    'requested_operation': session_payload.get('requested_operation'),
                    'insert_scope': session_payload.get('insert_scope'),
                },
                'requested_operation': session_payload.get('requested_operation'),
                'insert_scope': session_payload.get('insert_scope'),
            }

        if status not in {'analyzed', 'target_selected'}:
            message = f'Session is not ready for generation: status={status}'

            return {
                'session_id': session_id,
                'project_id': project_id,
                'selected_target': session_payload.get('selected_target'),
                'selected_target_source': 'session_selected'
                if session_payload.get('selected_target')
                else None,
                'run_id': None,
                'workspace_id': None,
                'workspace_path': None,
                'session': session_payload,
                'pipeline_result': None,
                'result_summary': {
                    'status': status or 'not_ready',
                    'generation_blocked': True,
                    'block_reason': 'session_not_ready',
                    'message': message,
                    'request_quality_status': request_quality_status or None,
                    'recommended_action': 'select_target_or_run_analyze_again',
                    'selected_target': session_payload.get('selected_target'),
                    'requested_operation': session_payload.get('requested_operation'),
                    'insert_scope': session_payload.get('insert_scope'),
                },
                'requested_operation': session_payload.get('requested_operation'),
                'insert_scope': session_payload.get('insert_scope'),
            }

        return None 

    def select_target(self, session_id: str, selected_qualname: str, requested_operation: str | None = None, insert_scope: str | None = None) -> dict[str, Any]:
        payload = self.registry.get(session_id)
        previous_selected_target = str(payload.get('selected_target') or '').strip() or None
        recommended_target = str(payload.get('recommended_target') or '').strip() or None
        new_selected_target = str(selected_qualname or '').strip() or None

        effective_previous_target = previous_selected_target or recommended_target
        selection_changed = new_selected_target != effective_previous_target

        payload['selected_target'] = selected_qualname
        if requested_operation:
            payload['requested_operation'] = requested_operation
        normalized_insert_scope = self._normalize_insert_scope(insert_scope)
        if normalized_insert_scope:
            payload['insert_scope'] = normalized_insert_scope

        if self._is_insufficient_request(payload):
            payload['status'] = 'needs_user_decision'
        else:
            payload['status'] = 'target_selected'

        payload['updated_at'] = _utc_now()
        self.registry.save(payload)

        return {
            'session_id': session_id,
            'project_id': payload['project_id'],
            'selected_target': payload['selected_target'],
            'session': payload,
            'result_summary': self._select_target_result_summary_payload(
                payload,
                selection_changed=selection_changed,
            ),
        }

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
        requested_operation: str | None = None,
        insert_scope: str | None = None,
    ) -> dict[str, Any]:
        session_payload = self.registry.get(session_id)
        blocked_result = self._build_generate_blocked_result(session_payload)
        if blocked_result is not None:
            LOGGER.info(
                'Session generate blocked: session_id=%s reason=%s status=%s',
                session_id,
                blocked_result.get('result_summary', {}).get('block_reason'),
                blocked_result.get('result_summary', {}).get('status'),
            )
            return blocked_result

        project = self.project_service.get_project(str(session_payload['project_id']))
        resolved_target, target_source = self.resolve_target(session_id, selected_target)

        incoming_requested_operation = requested_operation
        stored_requested_operation = str(session_payload.get('requested_operation') or '').strip()

        LOGGER.info(
            "Session generate input: session_id=%s selected_target=%s target_source=%s incoming_requested_operation=%s stored_requested_operation=%s",
            session_id,
            resolved_target,
            target_source,
            incoming_requested_operation,
            stored_requested_operation,
        )

        requested_operation = (
            incoming_requested_operation
            or stored_requested_operation
            or 'replace_symbol'
        )
        analysis = session_payload.get('analysis') or {}
        target_recommendation = analysis.get('target_recommendation') or {}
        target_insert_scope = target_recommendation.get('insert_scope') or {}
        search_plan = analysis.get('search_plan') or {}
        search_insert_scope = search_plan.get('insert_scope') or {}

        effective_insert_scope = (
            self._normalize_insert_scope(insert_scope)
            or self._normalize_insert_scope(str(session_payload.get('insert_scope') or ''))
            or self._normalize_insert_scope(str((target_insert_scope or {}).get('value') or ''))
            or self._normalize_insert_scope(str((search_insert_scope or {}).get('value') or ''))
        
        )
        if effective_insert_scope:
            session_payload['insert_scope'] = effective_insert_scope

        LOGGER.info(
            "Session generate resolved operation: session_id=%s effective_requested_operation=%s insert_scope=%s",
            session_id,
            requested_operation,
            effective_insert_scope,
        )

        target_symbol_payload = self._target_symbol_payload(session_payload, resolved_target)
        target_kind = str(target_symbol_payload.get('kind') or '').strip()
        if requested_operation == 'insert_after_symbol' and target_kind == 'method':
            if not effective_insert_scope:
                return self._build_blocked_generation_payload(
                    session_payload,
                    block_reason='ambiguous_insert_scope',
                    message=(
                        'Выбран метод внутри класса как anchor для insert_after_symbol. '
                        'Укажите insert_scope=class_body для добавления метода класса или выберите module-level anchor.'
                    ),
                    recommended_action='select_target_with_insert_scope_or_choose_module_anchor',
                    selected_target=resolved_target,
                    selected_target_source=target_source,
                    requested_operation=requested_operation,
                    insert_scope=effective_insert_scope,
                    target_symbol=target_symbol_payload,
                )
            if effective_insert_scope == 'module_body':
                return self._build_blocked_generation_payload(
                    session_payload,
                    block_reason='unsafe_method_anchor_for_module_insert',
                    message=(
                        'Для module_body вставки нельзя использовать method-anchor внутри класса. '
                        'Выберите top-level anchor или измените insert_scope на class_body.'
                    ),
                    recommended_action='choose_module_anchor_or_use_class_body_scope',
                    selected_target=resolved_target,
                    selected_target_source=target_source,
                    requested_operation=requested_operation,
                    insert_scope=effective_insert_scope,
                    target_symbol=target_symbol_payload,
                )
        run_id = ""
        workspace_path = ""
        workspace_id = ""

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

        LOGGER.info(
            "Session generate calling pipeline_generate: session_id=%s selected_target=%s requested_operation=%s skip_search=%s",
            session_id,
            resolved_target,
            requested_operation,
            target_source in {'request', 'session_selected'},
        )
        
        try:
            result = services.pipeline_generate(
                change_request=change_request,
                selected_target=resolved_target,
                limit=limit or self.config.search_default_limit,
                use_vector_search=use_vector_search,
                skip_search=target_source in {'request', 'session_selected'},
                requested_operation=requested_operation,
                insert_scope=effective_insert_scope,
           )
        except PipelineRunFailed as exc:
            result = exc.result
            run_id = result.run_id if result else ""
            workspace_path = str(result.apply_result.workspace_path) if result and result.apply_result else ""
            workspace_id = Path(workspace_path).name if workspace_path else ""

            run_ids = [str(item) for item in session_payload.get('run_ids', [])]
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)

            workspace_ids = [str(item) for item in session_payload.get('workspace_ids', [])]
            if workspace_id and workspace_id not in workspace_ids:
                workspace_ids.append(workspace_id)

            result_summary = self._result_summary_payload(result) if result else None
            result_status = str((result_summary or {}).get('status') or '').strip()

            if not result_status and result and result.verification_report is not None:
                result_status = result.verification_report.verdict

            if not result_status:
                result_status = 'generate_failed'

            session_payload['selected_target'] = resolved_target
            session_payload['requested_operation'] = requested_operation
            if effective_insert_scope:
                session_payload['insert_scope'] = effective_insert_scope
            session_payload['status'] = result_status
            session_payload['updated_at'] = _utc_now()
            session_payload['run_ids'] = run_ids
            session_payload['workspace_ids'] = workspace_ids
            session_payload['last_run_id'] = run_id or session_payload.get('last_run_id')
            if workspace_id:
                session_payload['last_workspace_id'] = workspace_id
            self.registry.save(session_payload)

            return {
                'status': 'failed',
                'message': str(exc),
                'session_id': session_id,
                'project_id': session_payload['project_id'],
                'selected_target': resolved_target,
                'selected_target_source': target_source,
                'run_id': run_id or None,
                'workspace_id': workspace_id or None,
                'workspace_path': workspace_path or None,
                'session': session_payload,
                'pipeline_result': self._pipeline_payload(result) if result else None,
                'result_summary': result_summary,
                'requested_operation': requested_operation,
                'insert_scope': effective_insert_scope,
            }

        run_id = result.run_id
        workspace_path = str(result.apply_result.workspace_path) if result.apply_result else ''
        workspace_id = Path(workspace_path).name if workspace_path else ''

        run_ids = [str(item) for item in session_payload.get('run_ids', [])]
        if run_id not in run_ids:
            run_ids.append(run_id)
        workspace_ids = [str(item) for item in session_payload.get('workspace_ids', [])]
        if workspace_id and workspace_id not in workspace_ids:
            workspace_ids.append(workspace_id)

        result_summary = self._result_summary_payload(result)
        result_status = str((result_summary or {}).get('status') or '').strip()

        if not result_status and result.verification_report is not None:
            result_status = result.verification_report.verdict

        if not result_status:
            result_status = 'generated'

        session_payload['selected_target'] = resolved_target
        session_payload['requested_operation'] = requested_operation
        if effective_insert_scope:
            session_payload['insert_scope'] = effective_insert_scope
        session_payload['status'] = result_status
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
            'result_summary': result_summary,
            'requested_operation': requested_operation,
        }

    def repair(
        self,
        session_id: str,
        selected_target: str | None = None,
        note: str | None = None,
        use_vector_search: bool | None = None,
    ) -> dict[str, Any]:
        session_payload = self.registry.get(session_id)
        project = self.project_service.get_project(str(session_payload['project_id']))
        resolved_target, target_source = self.resolve_target(session_id, selected_target)

        requested_operation = (
            str(session_payload.get('requested_operation') or '').strip()
            or 'replace_symbol'
        )       

        workspace_id = str(session_payload.get('last_workspace_id') or '').strip()
        if not workspace_id:
            raise ValueError(f'Session {session_id} does not have an active workspace for repair')
        previous_run_id = str(session_payload.get('last_run_id') or '').strip()
        if not previous_run_id:
            raise ValueError(f'Session {session_id} does not have a previous run for repair')

        requirements = session_payload.get('input_requirements') or []
        if not requirements:
            raise ValueError(f'Session {session_id} does not contain input requirements')
        first_req = requirements[0]
        notes = [str(item) for item in first_req.get('notes', []) or []]
        if note:
            notes.append(str(note))
        change_request = ChangeRequest(
            title=str(first_req.get('title', '')).strip(),
            description=str(first_req.get('description', '')).strip(),
            constraints=[str(item) for item in first_req.get('constraints', []) or []],
            notes=notes,
            project=str(project.project_name),
        )
        if not change_request.title or not change_request.description:
            raise ValueError(f'Session {session_id} contains invalid change request payload')

        previous_bundle = self._load_run_bundle(previous_run_id)
        previous_result_payload = self._select_previous_result_payload(previous_bundle)
        verification_report = self._build_manual_repair_report(note)

        workspace_path = self.tool_root / self.config.workspace_root_dirname / workspace_id
        services = ProjectServices(workspace_path, config=self.config)
        result = services.pipeline_repair(
            change_request=change_request,
            selected_target=resolved_target,
            previous_result_payload=previous_result_payload,
            verification_report=verification_report,
            requested_operation=requested_operation,
        )

        run_id = result.run_id
        new_workspace_path = str(result.apply_result.workspace_path) if result.apply_result else ''
        new_workspace_id = Path(new_workspace_path).name if new_workspace_path else ''

        run_ids = [str(item) for item in session_payload.get('run_ids', [])]
        if run_id not in run_ids:
            run_ids.append(run_id)

        workspace_ids = [str(item) for item in session_payload.get('workspace_ids', [])]
        if new_workspace_id and new_workspace_id not in workspace_ids:
            workspace_ids.append(new_workspace_id)

        outcome = self._repair_outcome(result)
        repair_effective = outcome != 'no_effective_change'

        from codecollector.workspace.workspace_service import WorkspaceService
        workspace_service = WorkspaceService(self.tool_root, self.config)

        old_workspace_id = workspace_id

        if outcome == 'no_effective_change':
            if new_workspace_id:
                try:
                    workspace_service.delete_workspace(new_workspace_id)
                except FileNotFoundError:
                    pass
            workspace_ids = [item for item in workspace_ids if item != new_workspace_id]

            session_payload['selected_target'] = resolved_target
            session_payload['status'] = 'repair_no_effective_change'
            session_payload['updated_at'] = _utc_now()
            session_payload['run_ids'] = run_ids
            session_payload['workspace_ids'] = workspace_ids
            session_payload['last_run_id'] = run_id
            session_payload['last_workspace_id'] = old_workspace_id
            self.registry.save(session_payload)

            return {
                'session_id': session_id,
                'project_id': session_payload['project_id'],
                'selected_target': resolved_target,
                'selected_target_source': target_source,
                'run_id': run_id,
                'workspace_id': old_workspace_id or None,
                'workspace_path': str(self.tool_root / self.config.workspace_root_dirname / old_workspace_id) if old_workspace_id else None,
                'repair_outcome': 'no_effective_change',
                'repair_effective': False,
                'message': 'Repair was executed but produced no effective change.',
                'session': session_payload,
                'pipeline_result': self._pipeline_payload(result),
                'result_summary': self._result_summary_payload(result),
            }

        if outcome == 'verification_failed':
            session_payload['selected_target'] = resolved_target
            session_payload['status'] = 'repair_verification_failed'
            session_payload['updated_at'] = _utc_now()
            session_payload['run_ids'] = run_ids
            session_payload['workspace_ids'] = workspace_ids
            session_payload['last_run_id'] = run_id
            session_payload['last_workspace_id'] = new_workspace_id or session_payload.get('last_workspace_id')
            self.registry.save(session_payload)

            return {
                'session_id': session_id,
                'project_id': session_payload['project_id'],
                'selected_target': resolved_target,
                'selected_target_source': target_source,
                'run_id': run_id,
                'workspace_id': new_workspace_id or None,
                'workspace_path': new_workspace_path or None,
                'repair_outcome': 'verification_failed',
                'repair_effective': True,
                'message': 'Repair changed the workspace, but verification failed.',
                'session': session_payload,
                'pipeline_result': self._pipeline_payload(result),
                'result_summary': self._result_summary_payload(result),
            }

        if new_workspace_id and old_workspace_id and new_workspace_id != old_workspace_id:
            try:
                workspace_service.delete_workspace(old_workspace_id)
            except FileNotFoundError:
                pass
            workspace_ids = [item for item in workspace_ids if item != old_workspace_id]

        session_payload['selected_target'] = resolved_target
        session_payload['status'] = 'repaired'
        session_payload['updated_at'] = _utc_now()
        session_payload['run_ids'] = run_ids
        session_payload['workspace_ids'] = workspace_ids
        session_payload['last_run_id'] = run_id
        session_payload['last_workspace_id'] = new_workspace_id or session_payload.get('last_workspace_id')
        self.registry.save(session_payload)

        return {
            'session_id': session_id,
            'project_id': session_payload['project_id'],
            'selected_target': resolved_target,
            'selected_target_source': target_source,
            'run_id': run_id,
            'workspace_id': new_workspace_id or None,
            'workspace_path': new_workspace_path or None,
            'repair_outcome': 'repaired',
            'repair_effective': True,
            'message': 'Repair completed successfully.',
            'session': session_payload,
            'pipeline_result': self._pipeline_payload(result),
            'result_summary': self._result_summary_payload(result),
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
            'verification_report': asdict(result.verification_report) if result.verification_report else None,
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

    def _result_summary_payload(self, result: PipelineRunResult | None) -> dict[str, Any] | None:
        if result is None or result.execution_summary is None:
            return None

        execution = result.execution_summary
        summary = GenerateApiResultSummary(
            status=execution.status,
            selected_target=execution.selected_target,
            requested_operation=execution.requested_operation,
            final_operation=execution.final_operation,
            insert_scope=getattr(execution, 'insert_scope', None),
            workspace_path=execution.workspace_path,
            changed_files=list(execution.changed_files),
            symbols_in_changed_files=list(execution.symbols_in_changed_files),
            verification_passed=execution.verification_passed,
            merge_mode=execution.merge_mode,
            merge_ready=execution.merge_ready,
            has_generated_test=execution.has_generated_test,
            generated_test_files=list(execution.generated_test_files),
            repair_used=execution.repair_used,
            linked_requirements=list(execution.linked_requirements),
            recommended_tests=list(execution.recommended_tests),
            recommended_test_commands=list(execution.recommended_test_commands),
            excluded_files=list(getattr(execution, 'excluded_files', []) or []),            
        )
        return asdict(summary)
    
    def _select_target_result_summary_payload(
        self,
        session_payload: dict[str, Any],
    selection_changed: bool,
    ) -> dict[str, Any]:
        selected_target = str(session_payload.get('selected_target') or '').strip() or None
        recommended = str(session_payload.get('recommended_target') or '').strip() or None

        summary = SelectTargetApiResultSummary(
            status=str(session_payload.get('status') or ''),
            project_id=str(session_payload.get('project_id') or ''),
            requested_operation=str(session_payload.get('requested_operation') or '').strip() or None,
            insert_scope=str(session_payload.get('insert_scope') or '').strip() or None,
            recommended_target=recommended,
            selected_target=selected_target,
            selection_changed=selection_changed,
        )
        return asdict(summary)