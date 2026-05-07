from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import time
from typing import Any

from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService
from codecollector.config import AppConfig
from codecollector.domain.models import AnalyzeApiResultSummary, ChangeRequest, ContextPack, SearchCandidate, SymbolRecord
from codecollector.orchestration.services import ProjectServices
from codecollector.projects.project_service import ProjectService
from codecollector.sessions.session_service import SessionService
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)
PATCH_OPERATIONS = {'replace_symbol', 'insert_after_symbol'}
INSERT_SCOPES = {'module_body', 'class_body'}


@dataclass(slots=True)
class AnalyzeSessionResult:
    session_id: str
    project_id: str
    input_requirements: list[dict[str, Any]]
    requested_operation: str
    insert_scope: str | None
    operation_source: str
    operation_confidence: float | None
    operation_reason: str | None
    request_quality: dict[str, Any]
    search_plan: dict[str, Any]
    target_recommendation: dict[str, Any]
    analysis_usage: dict[str, Any]
    manual_review_required: bool
    warnings: list[dict[str, Any]]
    candidates: list[SearchCandidate]
    recall_candidates_count: int
    recommended_target: str | None
    context_summary: dict[str, Any] | None
    result_summary: AnalyzeApiResultSummary | None = None


class AnalyzeService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.projects = ProjectService(self.tool_root, config)
        self.sessions = SessionService(self.tool_root, config)
        self.llm_assist = AnalysisLlmAssistService(self.tool_root, config) if config.analysis_llm_enabled else None

    def analyze(
        self,
        *,
        project_id: str,
        input_requirements: list[dict[str, Any]],
        requested_operation: str | None = None,
        insert_scope: str | None = None,
        limit: int | None = None,
        use_vector_search: bool | None = None,
    ) -> AnalyzeSessionResult:
        analyze_started_at = time.perf_counter()
        project = self.projects.get_project(project_id)
        if project.status not in ('ready', 'registered'):
            raise ValueError(f'Project {project_id} has unsupported status for analyze: {project.status}')

        query = self._build_query(input_requirements)
        services = ProjectServices(Path(project.project_root), tool_root=self.tool_root, config=self.config)

        search_plan: dict[str, Any] = {}
        operation_source = 'user' if requested_operation else 'fallback'
        operation_confidence: float | None = 1.0 if requested_operation else None
        operation_reason: str | None = 'Операция явно выбрана пользователем.' if requested_operation else None
        request_quality: dict[str, Any] = {'status': 'unknown', 'reason': '', 'missing_information': []}
        manual_review_required = False
        warnings: list[dict[str, Any]] = []
        llm_usage_steps: dict[str, dict[str, Any]] = {}
        analysis_timings: dict[str, float] = {}        

        def record_timing(name: str, started_at: float) -> None:
            analysis_timings[name] = round(time.perf_counter() - started_at, 6)

        effective_operation = self._normalize_operation(requested_operation) or 'replace_symbol'
        effective_insert_scope = self._normalize_insert_scope(insert_scope)
        if self.llm_assist is not None:
            search_plan_started_at = time.perf_counter()
            try:
                search_plan = self.llm_assist.build_search_plan(
                    requirements=input_requirements,
                    user_operation=requested_operation,
                    insert_scope=effective_insert_scope,
                    services=services,
                )
                record_timing('search_plan_total_sec', search_plan_started_at)
                llm_operation = self._operation_from_search_plan(search_plan)
                if requested_operation:
                    effective_operation = self._normalize_operation(requested_operation) or effective_operation
                    operation_source = 'user'
                elif llm_operation:
                    effective_operation = llm_operation
                    operation_source = 'llm_search_plan'
                operation_confidence = self._safe_float((search_plan.get('operation') or {}).get('confidence')) or operation_confidence
                operation_reason = str((search_plan.get('operation') or {}).get('reason') or operation_reason or '').strip() or None
                if effective_insert_scope is None:
                    effective_insert_scope = self._insert_scope_from_payload(search_plan)
                request_quality = self._request_quality_from_plan(search_plan)
                self._record_llm_usage(llm_usage_steps, 'search_plan', search_plan)
                manual_review_required = bool(search_plan.get('manual_review_required'))
                warnings.extend(self._warnings_from_plan(search_plan))
            except Exception as exc:
                record_timing('search_plan_total_sec', search_plan_started_at)                
                LOGGER.exception('LLM search plan failed; analyze will continue with fallback search')
                warnings.append(
                    {
                        'code': 'analysis_llm_search_plan_failed',
                        'message': str(exc),
                    }
                )

        session_create_started_at = time.perf_counter()
        session = self.sessions.create_session(
            project_id=project_id,
            input_requirements=input_requirements,
            requested_operation=effective_operation,
        )
        record_timing('session_create_sec', session_create_started_at)

        LOGGER.info(
            'Analyze created session: session_id=%s project_id=%s requested_operation=%s operation_source=%s',
            session['session_id'],
            project_id,
            effective_operation,
            operation_source,
        )

        recall_search_started_at = time.perf_counter()
        candidates = self._collect_recall_candidates(
            services=services,
            base_query=query,
            search_plan=search_plan,
            requested_operation=effective_operation,
            limit=limit,
            use_vector_search=use_vector_search,
        )
        record_timing('recall_search_sec', recall_search_started_at)

        target_recommendation: dict[str, Any] = {}
        skip_rerank_reason = self._candidate_rerank_skip_reason(search_plan)
        if self.llm_assist is not None and candidates and not skip_rerank_reason:
            try:
                candidate_rerank_started_at = time.perf_counter()
                target_recommendation = self.llm_assist.rerank_candidates(
                    requirements=input_requirements,
                    user_operation=requested_operation,
                    effective_operation=effective_operation,
                    insert_scope=effective_insert_scope,
                    search_plan=search_plan,
                    candidates=candidates,
                    services=services,
                )
                record_timing('candidate_rerank_total_sec', candidate_rerank_started_at)
                candidates = self.llm_assist.reorder_candidates(candidates, target_recommendation)
                request_quality = self._resolved_request_quality(request_quality, target_recommendation)
                warnings.extend(self._warnings_from_plan(target_recommendation))
                self._record_llm_usage(llm_usage_steps, 'candidate_rerank', target_recommendation)
                if effective_insert_scope is None:
                    effective_insert_scope = self._insert_scope_from_payload(target_recommendation)
                rerank_operation = self._normalize_operation(str(target_recommendation.get('recommended_operation') or ''))
                if requested_operation:
                    effective_operation = self._normalize_operation(requested_operation) or effective_operation
                elif rerank_operation and self._safe_float(target_recommendation.get('operation_confidence')) >= self.config.analysis_min_confidence_auto_operation:
                    effective_operation = rerank_operation
                    operation_source = 'llm_rerank'
                    operation_confidence = self._safe_float(target_recommendation.get('operation_confidence'))
                    operation_reason = str(target_recommendation.get('operation_reason') or operation_reason or '').strip() or None
            except Exception as exc:
                record_timing('candidate_rerank_total_sec', candidate_rerank_started_at)
                LOGGER.exception('LLM candidate rerank failed; analyze will keep search order')
                warnings.append(
                    {
                        'code': 'analysis_llm_rerank_failed',
                        'message': str(exc),
                    }
                )
        elif skip_rerank_reason:
            analysis_timings['candidate_rerank_total_sec'] = 0.0
            LOGGER.info('Analyze candidate rerank skipped: %s', skip_rerank_reason)
            warnings.append(
                {
                    'code': 'analysis_llm_rerank_skipped',
                    'message': skip_rerank_reason,
                }
            )

        target_resolution_started_at = time.perf_counter()
        recommended_target = self._recommended_target(candidates, target_recommendation)
        if recommended_target:
            recommended_target, candidates, target_recommendation = self._post_process_recommended_target(
                services=services,
                requested_operation=effective_operation,
                insert_scope=effective_insert_scope,
                recommended_target=recommended_target,
                candidates=candidates,
                target_recommendation=target_recommendation,
            )
        record_timing('target_resolution_sec', target_resolution_started_at)
        manual_review_required = self._manual_review_required(
            search_plan,
            target_recommendation,
            candidates,
            recommended_target=recommended_target,
        )
        warnings = self._final_top_level_warnings(
            warnings,
            manual_review_required=manual_review_required,
            recommended_target=recommended_target,
            target_recommendation=target_recommendation,
        )
        recall_candidates_count = len(candidates)
        api_candidates_started_at = time.perf_counter()
        api_candidates = self._api_candidates(candidates, recommended_target)
        record_timing('api_candidates_sec', api_candidates_started_at)
        context_summary_started_at = time.perf_counter()
        context_summary = None
        if recommended_target:
            context_summary = self._context_summary(services.context(recommended_target))
        record_timing('context_summary_sec', context_summary_started_at)

        analysis_timings['total_sec'] = round(time.perf_counter() - analyze_started_at, 6)
        analysis_usage = self._build_analysis_usage(
            llm_usage_steps,
            analysis_timings['total_sec'],
            timings=analysis_timings,
        )
        session_mark_started_at = time.perf_counter()
        self.sessions.mark_analyzed(
            session['session_id'],
            recommended_target=recommended_target,
            requested_operation=effective_operation,
            analysis_metadata={
                'operation_source': operation_source,
                'operation_confidence': operation_confidence,
                'operation_reason': operation_reason,
                'insert_scope': effective_insert_scope,
                'request_quality': request_quality,
                'search_plan': search_plan,
                'target_recommendation': target_recommendation,
                'analysis_usage': analysis_usage,
                'manual_review_required': manual_review_required,
                'warnings': warnings,
            },
        )
        record_timing('session_mark_analyzed_sec', session_mark_started_at)
        analysis_timings['total_sec'] = round(time.perf_counter() - analyze_started_at, 6)
        analysis_usage['duration_sec'] = analysis_timings['total_sec']
        analysis_usage['timings'] = dict(analysis_timings)        

        LOGGER.info(
            'Analyze marked session: session_id=%s recommended_target=%s requested_operation=%s returned_candidates=%s recall_candidates=%s manual_review_required=%s analysis_total_tokens=%s analysis_duration=%.2fs',
            session['session_id'],
            recommended_target,
            effective_operation,
            len(api_candidates),
            recall_candidates_count,
            manual_review_required,
            analysis_usage.get('total_tokens'),
            analysis_usage.get('duration_sec') or 0.0,
        )

        return AnalyzeSessionResult(
            session_id=session['session_id'],
            project_id=project_id,
            input_requirements=input_requirements,
            requested_operation=effective_operation,
            insert_scope=effective_insert_scope,
            operation_source=operation_source,
            operation_confidence=operation_confidence,
            operation_reason=operation_reason,
            request_quality=request_quality,
            search_plan=search_plan,
            target_recommendation=target_recommendation,
            analysis_usage=analysis_usage,
            manual_review_required=manual_review_required,
            warnings=warnings,
            candidates=api_candidates,
            recall_candidates_count=recall_candidates_count,
            recommended_target=recommended_target,
            context_summary=context_summary,
            result_summary=self._build_result_summary(
                project_id=project_id,
                requested_operation=effective_operation,
                insert_scope=effective_insert_scope,
                recommended_target=recommended_target,
                candidates=api_candidates,
                recall_candidates_count=recall_candidates_count,
                context_summary=context_summary,
                operation_source=operation_source,
                operation_confidence=operation_confidence,
                manual_review_required=manual_review_required,
                request_quality=request_quality,
                target_recommendation=target_recommendation,
                analysis_usage=analysis_usage,
            ),
        )

    def _collect_recall_candidates(
        self,
        *,
        services: ProjectServices,
        base_query: str,
        search_plan: dict[str, Any],
        requested_operation: str,
        limit: int | None,
        use_vector_search: bool | None,
    ) -> list[SearchCandidate]:
        query_limit = max(limit or self.config.search_default_limit, self.config.analysis_base_search_limit)
        queries = [base_query]
        for query in ((search_plan.get('search_plan') or {}).get('search_queries') or [])[: self.config.analysis_max_search_plan_queries]:
            query_text = str(query or '').strip()
            if query_text and query_text not in queries:
                queries.append(query_text)

        LOGGER.info(
            'Analyze recall search: base_query_chars=%s query_limit=%s requested_operation=%s planned_queries=%s',
            len(base_query),
            query_limit,
            requested_operation,
            queries,
        )
        by_qualname: dict[str, SearchCandidate] = {}
        search_results_by_query = services.search_many(
            queries=queries,
            limit=query_limit,
            use_vector_search=use_vector_search,
            requested_operation=requested_operation,
        )
        for query_candidates in search_results_by_query.values():
            for candidate in query_candidates:
                existing = by_qualname.get(candidate.qualname)
                if existing is None or candidate.score > existing.score:
                    by_qualname[candidate.qualname] = candidate

        self._add_hint_candidates(by_qualname, services, search_plan)
        candidates = list(by_qualname.values())
        LOGGER.info('Analyze recall collected candidates_count=%s before_limit=%s', len(candidates), max(limit or self.config.search_default_limit, self.config.analysis_max_recall_candidates))
        candidates.sort(key=lambda item: (-item.score, -item.confidence, item.file_path, item.qualname))
        return candidates[: max(limit or self.config.search_default_limit, self.config.analysis_max_recall_candidates)]

    def _add_hint_candidates(
        self,
        by_qualname: dict[str, SearchCandidate],
        services: ProjectServices,
        search_plan: dict[str, Any],
    ) -> None:
        plan = search_plan.get('search_plan') or {}
        preferred_qualnames = [str(item).strip() for item in plan.get('preferred_qualnames', []) or [] if str(item).strip()]
        preferred_files = {str(item).strip() for item in plan.get('preferred_files', []) or [] if str(item).strip()}
        project_key = str(services.project_root)
        for qualname in preferred_qualnames:
            symbol = services.store.get_symbol(project_key, qualname)
            if symbol and symbol.kind != 'module' and symbol.qualname not in by_qualname:
                by_qualname[symbol.qualname] = self._candidate_from_symbol(symbol, score=6.0, reason='symbol предложен LLM search plan')
        if preferred_files:
            for symbol in services.store.list_symbols(project_key):
                if symbol.kind == 'module':
                    continue
                if symbol.file_path in preferred_files and symbol.qualname not in by_qualname:
                    by_qualname[symbol.qualname] = self._candidate_from_symbol(symbol, score=5.0, reason='файл предложен LLM search plan')

    def _candidate_from_symbol(self, symbol: SymbolRecord, *, score: float, reason: str) -> SearchCandidate:
        return SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=score,
            confidence=min(1.0, score / 10.0),
            relevance_category='средняя',
            reasons=[reason],
            docstring=symbol.docstring,
            knowledge_title='',
            requirements=[],
        )

    def _manual_review_required(
        self,
        search_plan: dict[str, Any],
        target_recommendation: dict[str, Any],
        candidates: list[SearchCandidate],
        *,
        recommended_target: str | None = None,
    ) -> bool:
        if not target_recommendation:
            return bool(search_plan.get('manual_review_required')) or recommended_target is None
        if bool(target_recommendation.get('manual_review_required')):
            return True
        if self._has_accepted_target(candidates, target_recommendation, recommended_target=recommended_target):
            return False
        return True

    def _candidate_rerank_skip_reason(self, search_plan: dict[str, Any]) -> str | None:
        quality = search_plan.get('request_quality') or {}
        status = str(quality.get('status') or '').strip().lower()
        operation = self._operation_from_search_plan(search_plan)
        plan = search_plan.get('search_plan') or {}
        has_search_hints = any(
            plan.get(field)
            for field in ('search_queries', 'preferred_files', 'preferred_qualnames', 'preferred_layers')
        )
        if status == 'insufficient' and not operation and not has_search_hints:
            return (
                'Запрос признан недостаточным, search plan не содержит поисковых подсказок; '
                'дорогой LLM rerank пропущен до ручного уточнения.'
            )
        return None

    def _has_accepted_target(
        self,
        candidates: list[SearchCandidate],
        target_recommendation: dict[str, Any],
        *,
        recommended_target: str | None = None,
    ) -> bool:
        if bool(target_recommendation.get('manual_review_required')):
            return False
        target = (recommended_target or str(target_recommendation.get('recommended_target') or '').strip())
        confidence = self._safe_float(target_recommendation.get('target_confidence'))
        if not target or confidence < self.config.analysis_min_confidence_auto_recommend_target:
            return False
        return any(candidate.qualname == target for candidate in candidates)

    def _resolved_request_quality(
        self,
        request_quality: dict[str, Any],
        target_recommendation: dict[str, Any],
    ) -> dict[str, Any]:
        if not target_recommendation:
            return request_quality
        status = str(request_quality.get('status') or '').strip().lower()
        recommended_target = str(target_recommendation.get('recommended_target') or '').strip()
        target_confidence = self._safe_float(target_recommendation.get('target_confidence'))
        target_is_accepted = (
            not bool(target_recommendation.get('manual_review_required'))
            and bool(recommended_target)
            and target_confidence >= self.config.analysis_min_confidence_auto_recommend_target
        )
        if status in {'uncertain', 'unknown', 'insufficient'} and target_is_accepted:
            return {
                'status': 'processable',
                'reason': 'Первичный план поиска считал запрос неполным, но candidate rerank нашел уверенный target по коду и связям проекта.',
                'missing_information': [],
                'source_status': request_quality.get('status'),
                'source_reason': request_quality.get('reason'),
                'resolution': 'resolved_by_candidate_rerank',
            }
        return request_quality

    def _api_candidates(self, candidates: list[SearchCandidate], recommended_target: str | None) -> list[SearchCandidate]:
        max_items = max(1, self.config.analysis_max_return_candidates)
        if not candidates:
            return []

        llm_ranked = [candidate for candidate in candidates if candidate.ranked_by_llm]
        if llm_ranked:
            llm_ranked.sort(key=lambda item: (item.llm_rank if item.llm_rank is not None else 999, -item.confidence, item.qualname))
            return llm_ranked[:max_items]

        if not recommended_target:
            return candidates[:max_items]
        selected: list[SearchCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.qualname == recommended_target:
                selected.append(candidate)
                seen.add(candidate.qualname)
                break
        for candidate in candidates:
            if len(selected) >= max_items:
                break
            if candidate.qualname not in seen:
                selected.append(candidate)
                seen.add(candidate.qualname)
        return selected

    def _recommended_target(self, candidates: list[SearchCandidate], target_recommendation: dict[str, Any]) -> str | None:
        if not target_recommendation:
            return None
        recommended = str(target_recommendation.get('recommended_target') or '').strip()
        confidence = self._safe_float(target_recommendation.get('target_confidence'))
        if not recommended or bool(target_recommendation.get('manual_review_required')):
            return None
        if confidence < self.config.analysis_min_confidence_auto_recommend_target:
            return None
        if any(candidate.qualname == recommended for candidate in candidates):
            return recommended
        return None

    def _post_process_recommended_target(
        self,
        *,
        services: ProjectServices,
        requested_operation: str,
        insert_scope: str | None,
        recommended_target: str,
        candidates: list[SearchCandidate],
        target_recommendation: dict[str, Any],
    ) -> tuple[str, list[SearchCandidate], dict[str, Any]]:
        if requested_operation != 'insert_after_symbol':
            return recommended_target, candidates, target_recommendation

        anchor_symbol = self._last_top_level_anchor_in_target_file(services, recommended_target)
        if anchor_symbol is None or anchor_symbol.qualname == recommended_target:
            return recommended_target, candidates, target_recommendation

        old_target = recommended_target
        reason = (
            f'Для insert_after_symbol выбран последний top-level symbol в файле {anchor_symbol.file_path}: '
            f'{anchor_symbol.qualname}. Исходная LLM-рекомендация anchor была {old_target}.'
        )
        target_recommendation = dict(target_recommendation)
        target_recommendation['original_recommended_target'] = old_target
        target_recommendation['recommended_target'] = anchor_symbol.qualname
        target_recommendation['recommended_candidate_id'] = None
        target_recommendation['target_role'] = 'anchor'
        target_recommendation['target_reason'] = reason
        target_recommendation['post_processing'] = {
            'kind': 'insert_after_last_top_level_symbol_in_file',
            'original_recommended_target': old_target,
            'recommended_target': anchor_symbol.qualname,
            'file_path': anchor_symbol.file_path,
        }

        updated_candidates = self._promote_anchor_candidate(candidates, anchor_symbol, reason)
        LOGGER.info(
            'Analyze insert_after_symbol anchor post-processed: original=%s final=%s file=%s',
            old_target,
            anchor_symbol.qualname,
            anchor_symbol.file_path,
        )
        return anchor_symbol.qualname, updated_candidates, target_recommendation

    def _last_top_level_anchor_in_target_file(self, services: ProjectServices, qualname: str) -> SymbolRecord | None:
        project_key = str(services.project_root)
        target = services.store.get_symbol(project_key, qualname)
        if target is None:
            return None
        file_symbols = services.store.list_symbols_in_file(project_key, target.file_path)
        module_symbol = next((symbol for symbol in file_symbols if symbol.kind == 'module'), None)
        module_qualname = module_symbol.qualname if module_symbol else target.parent_qualname
        anchors = [
            symbol
            for symbol in file_symbols
            if symbol.kind in {'class', 'function'}
            and (module_qualname is None or symbol.parent_qualname == module_qualname)
        ]
        if not anchors:
            return None
        anchors.sort(key=lambda symbol: (symbol.end_line, symbol.start_line, symbol.qualname))
        return anchors[-1]

    def _promote_anchor_candidate(
        self,
        candidates: list[SearchCandidate],
        anchor: SymbolRecord,
        reason: str,
    ) -> list[SearchCandidate]:
        promoted: SearchCandidate | None = None
        remainder: list[SearchCandidate] = []
        for candidate in candidates:
            if candidate.qualname == anchor.qualname and promoted is None:
                reasons = [f'Anchor post-processing: {reason}', *candidate.reasons]
                promoted = SearchCandidate(
                    qualname=candidate.qualname,
                    name=candidate.name,
                    kind=candidate.kind,
                    file_path=candidate.file_path,
                    score=candidate.score,
                    confidence=max(candidate.confidence, self.config.analysis_min_confidence_auto_recommend_target),
                    relevance_category='высокая',
                    reasons=self._dedupe(reasons)[:8],
                    docstring=candidate.docstring,
                    knowledge_title=candidate.knowledge_title,
                    requirements=list(candidate.requirements),
                    ranked_by_llm=True,
                    llm_recommended=True,
                    llm_rank=1,
                    llm_reason=reason,
                )
            else:
                remainder.append(candidate)
        if promoted is None:
            promoted = self._candidate_from_symbol(anchor, score=7.0, reason=f'Anchor post-processing: {reason}')
            promoted = SearchCandidate(
                qualname=promoted.qualname,
                name=promoted.name,
                kind=promoted.kind,
                file_path=promoted.file_path,
                score=promoted.score,
                confidence=max(promoted.confidence, self.config.analysis_min_confidence_auto_recommend_target),
                relevance_category='высокая',
                reasons=promoted.reasons,
                docstring=promoted.docstring,
                knowledge_title=promoted.knowledge_title,
                requirements=promoted.requirements,
                ranked_by_llm=True,
                llm_recommended=True,
                llm_rank=1,
                llm_reason=reason,
            )
        adjusted: list[SearchCandidate] = []
        for index, candidate in enumerate(remainder, start=2):
            if candidate.ranked_by_llm and candidate.llm_rank is not None:
                adjusted.append(
                    SearchCandidate(
                        qualname=candidate.qualname,
                        name=candidate.name,
                        kind=candidate.kind,
                        file_path=candidate.file_path,
                        score=candidate.score,
                        confidence=candidate.confidence,
                        relevance_category=candidate.relevance_category,
                        reasons=candidate.reasons,
                        docstring=candidate.docstring,
                        knowledge_title=candidate.knowledge_title,
                        requirements=candidate.requirements,
                        ranked_by_llm=True,
                        llm_recommended=False,
                        llm_rank=index,
                        llm_reason=candidate.llm_reason,
                    )
                )
            else:
                adjusted.append(candidate)
        return [promoted, *adjusted]

    def _insert_scope_from_payload(self, payload: dict[str, Any]) -> str | None:
        scope_payload = payload.get('insert_scope') if isinstance(payload, dict) else None
        if isinstance(scope_payload, dict):
            return self._normalize_insert_scope(str(scope_payload.get('value') or ''))
        return self._normalize_insert_scope(str(scope_payload or ''))

    def _operation_from_search_plan(self, search_plan: dict[str, Any]) -> str | None:
        operation = search_plan.get('operation') or {}
        return self._normalize_operation(str(operation.get('value') or ''))

    def _normalize_operation(self, value: str | None) -> str | None:
        operation = str(value or '').strip()
        return operation if operation in PATCH_OPERATIONS else None

    def _normalize_insert_scope(self, value: str | None) -> str | None:
        scope = str(value or '').strip()
        return scope if scope in INSERT_SCOPES else None

    def _request_quality_from_plan(self, search_plan: dict[str, Any]) -> dict[str, Any]:
        quality = search_plan.get('request_quality') or {}
        if not isinstance(quality, dict):
            return {'status': 'unknown', 'reason': '', 'missing_information': []}
        return quality

    def _warnings_from_plan(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        warnings = payload.get('warnings') or []
        return [item for item in warnings if isinstance(item, dict)]

    def _final_top_level_warnings(
        self,
        warnings: list[dict[str, Any]],
        *,
        manual_review_required: bool,
        recommended_target: str | None,
        target_recommendation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if manual_review_required or not recommended_target or not target_recommendation:
            return warnings
        if bool(target_recommendation.get('manual_review_required')):
            return warnings
        confidence = self._safe_float(target_recommendation.get('target_confidence'))
        if confidence < self.config.analysis_min_confidence_auto_recommend_target:
            return warnings

        # Search-plan warnings are diagnostic after rerank resolved the request with
        # a confident target. Keep only real execution failures at the top level.
        result: list[dict[str, Any]] = []
        for warning in warnings:
            code = str(warning.get('code') or '')
            if code.startswith('analysis_llm_') and code.endswith('_failed'):
                result.append(warning)
        return result

    def _record_llm_usage(self, target: dict[str, dict[str, Any]], step: str, payload: dict[str, Any]) -> None:
        usage = payload.get('llm_usage') if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            target[step] = dict(usage)

    def _build_analysis_usage(
        self,
        steps: dict[str, dict[str, Any]],
        duration_sec: float,
        *,
        timings: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        prompt_tokens = sum(int(item.get('prompt_tokens') or 0) for item in steps.values())
        output_tokens = sum(int(item.get('output_tokens') or 0) for item in steps.values())
        prompt_chars = sum(int(item.get('prompt_chars') or 0) for item in steps.values())
        return {
            'calls': len(steps),
            'prompt_tokens': prompt_tokens,
            'output_tokens': output_tokens,
            'total_tokens': prompt_tokens + output_tokens,
            'prompt_chars': prompt_chars,
            'duration_sec': duration_sec,
            'steps': steps,
            'timings': dict(timings or {}),
        }

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    def _dedupe(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

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

    def _build_result_summary(
        self,
        *,
        project_id: str,
        requested_operation: str,
        insert_scope: str | None,
        recommended_target: str | None,
        candidates: list[SearchCandidate],
        recall_candidates_count: int,
        context_summary: dict[str, Any] | None,
        operation_source: str,
        operation_confidence: float | None,
        manual_review_required: bool,
        request_quality: dict[str, Any],
        target_recommendation: dict[str, Any],
        analysis_usage: dict[str, Any],
    ) -> AnalyzeApiResultSummary:
        return AnalyzeApiResultSummary(
            status='needs_user_decision' if manual_review_required else 'analyzed',
            project_id=project_id,
            requested_operation=requested_operation,
            insert_scope=insert_scope,
            recommended_target=recommended_target,
            candidates_count=recall_candidates_count,
            recall_candidates_count=recall_candidates_count,
            returned_candidates_count=len(candidates),
            top_candidates=[item.qualname for item in candidates[:3]],
            has_context_summary=context_summary is not None,
            operation_source=operation_source,
            operation_confidence=operation_confidence,
            request_quality_status=str(request_quality.get('status') or ''),
            manual_review_required=manual_review_required,
            target_selection_source=str(target_recommendation.get('source') or 'llm_rerank' if target_recommendation else 'search'),
            target_selection_confidence=self._safe_float(target_recommendation.get('target_confidence')) if target_recommendation else None,
            analysis_usage=analysis_usage,
        )


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
