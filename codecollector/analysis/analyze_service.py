from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.domain.models import ChangeRequest, ContextPack, SearchCandidate
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_service import SessionService
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)

@dataclass(slots=True)
class AnalyzeSessionResult:
    session_id: str
    project_id: str
    input_requirements: list[dict[str, Any]]
    requested_operation: str
    candidates: list[SearchCandidate]
    recommended_target: str | None
    context_summary: dict[str, Any] | None


class AnalyzeService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.projects = ProjectService(self.tool_root, config)
        self.sessions = SessionService(self.tool_root, config)

    def analyze(
        self,
        *,
        project_id: str,
        input_requirements: list[dict[str, Any]],
        requested_operation: str = 'replace_symbol',
        limit: int | None = None,
        use_vector_search: bool | None = None,
    ) -> AnalyzeSessionResult:
        project = self.projects.get_project(project_id)
        if project.status not in ('ready', 'registered'):
            raise ValueError(f'Project {project_id} has unsupported status for analyze: {project.status}')

        session = self.sessions.create_session(
            project_id=project_id,
            input_requirements=input_requirements,
            requested_operation=requested_operation,
        )

        LOGGER.info(
            "Analyze created session: session_id=%s project_id=%s requested_operation=%s",
            session["session_id"],
            project_id,
            requested_operation,
        )

        query = self._build_query(input_requirements)
        services = ProjectServices(Path(project.project_root), tool_root=self.tool_root, config=self.config)
        candidates = services.search(
            query=query,
            limit=limit or self.config.search_default_limit,
            use_vector_search=use_vector_search,
            requested_operation=requested_operation,
        )
        recommended_target = candidates[0].qualname if candidates else None
        context_summary = None
        if recommended_target:
            context_summary = self._context_summary(services.context(recommended_target))

        self.sessions.mark_analyzed(
            session["session_id"],
            recommended_target=recommended_target,
            requested_operation=requested_operation,
        )

        LOGGER.info(
            "Analyze marked session: session_id=%s recommended_target=%s requested_operation=%s candidates_count=%s",
            session["session_id"],
            recommended_target,
            requested_operation,
            len(candidates),
        )

        return AnalyzeSessionResult(
            session_id=session["session_id"],
            project_id=project_id,
            input_requirements=input_requirements,
            requested_operation=requested_operation,
            candidates=candidates,
            recommended_target=recommended_target,
            context_summary=context_summary,
        )

    def _build_query(self, requirements: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in requirements:
            title = str(item.get('title', '')).strip()
            description = str(item.get('description', '')).strip()
            constraints = [str(value).strip() for value in item.get('constraints', []) or [] if str(value).strip()]
            if title:
                parts.append(title)
            if description:
                parts.append(description)
            parts.extend(constraints)
        query = ' '.join(part for part in parts if part)
        if not query:
            raise ValueError('Analyze request must contain at least one non-empty title, description or constraint')
        return query

    def _context_summary(self, context: ContextPack) -> dict[str, Any]:
        return {
            'target': asdict(context.target),
            'related_tests': [asdict(item) for item in context.related_tests],
            'recommended_tests': list(context.recommended_tests),
            'knowledge_title': context.knowledge_title,
            'knowledge_description': context.knowledge_description,
            'requirement_ids': list(context.requirement_ids),
            'requirement_titles': list(context.requirement_titles),
            'reference_summary': dict(context.reference_summary),
            'reference_artifacts': [
                {
                    'artifact_id': item.artifact_id,
                    'title': item.title,
                    'language': item.language,
                    'usage_mode': item.usage_mode,
                    'content_mode': item.content_mode,
                    'relevance_score': item.relevance_score,
                    'why_selected': item.why_selected,
                }
                for item in context.reference_artifacts
            ],
            'neighbors_count': len(context.neighbors),
            'inbound_relations_count': len(context.inbound_relations),
            'outbound_relations_count': len(context.outbound_relations),
        }


def build_single_requirement_payload(
    *,
    title: str | None,
    description: str | None,
    constraints: list[str] | None = None,
    external_requirement_id: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'title': (title or '').strip(),
        'description': (description or '').strip(),
        'constraints': [str(item).strip() for item in (constraints or []) if str(item).strip()],
    }
    if external_requirement_id:
        payload['external_requirement_id'] = external_requirement_id
    if priority:
        payload['priority'] = priority
    return payload


def build_change_request_for_requirement(requirement: dict[str, Any], *, project: str) -> ChangeRequest:
    return ChangeRequest(
        title=str(requirement.get('title', '')).strip(),
        description=str(requirement.get('description', '')).strip(),
        project=project,
        constraints=[str(item).strip() for item in requirement.get('constraints', []) or [] if str(item).strip()],
        notes=[],
    )