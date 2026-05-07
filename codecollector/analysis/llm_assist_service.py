from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import re
from pathlib import Path
from string import Template
from typing import Any

from codecollector.analysis.llm_client import AnalysisLlmClient
from codecollector.config import AppConfig
from codecollector.domain.models import ContextPack, SearchCandidate, SymbolRecord
from codecollector.logger import get_logger
from codecollector.orchestration.services import ProjectServices

LOGGER = get_logger(__name__)
_JSON_OBJECT_RE = re.compile(r'\{.*\}', re.DOTALL)


class AnalysisLlmAssistService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.prompts_dir = self.tool_root / self.config.analysis_prompt_dir
        self.client = AnalysisLlmClient(config)
        self.system_prompt = self._read_template(self.config.analysis_system_template)
        self.search_plan_template = self._read_template(self.config.analysis_search_plan_template)
        self.rerank_template = self._read_template(self.config.analysis_rerank_template)

    def build_search_plan(
        self,
        *,
        requirements: list[dict[str, Any]],
        user_operation: str | None,
        insert_scope: str | None = None,
        services: ProjectServices = None,
    ) -> dict[str, Any]:
        project_map = self._project_map(services)
        LOGGER.info(
            'analysis search plan context: project_files=%s project_symbols=%s user_operation=%s insert_scope=%s',
            len(project_map),
            sum(len(item.get('symbols') or []) for item in project_map),
            user_operation,
            insert_scope,
        )
        payload = {
            'request': self._request_payload(requirements),
            'user_operation': user_operation,
            'user_insert_scope': insert_scope,
            'operation_definitions': self._operation_definitions(),
            'project_map': project_map
        }
        user_prompt, prompt_budget = self._build_limited_search_plan_prompt(payload)
        call = self.client.chat_json(
            step='analyze_search_plan',
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            max_prompt_chars=int(prompt_budget.get('max_prompt_chars') or self.config.analysis_llm_search_plan_max_prompt_chars),
        )
        parsed = self._parse_json_object(call.content)
        parsed.setdefault('llm_usage', call.usage_dict())
        parsed.setdefault('prompt_budget', prompt_budget)
        return parsed

    def rerank_candidates(
        self,
        *,
        requirements: list[dict[str, Any]],
        user_operation: str | None,
        effective_operation: str,
        insert_scope: str | None = None,
        search_plan: dict[str, Any] | None = None,
        candidates: list[SearchCandidate],
        services: ProjectServices,
    ) -> dict[str, Any]:
        candidate_cards = self._candidate_cards(candidates, services)
        LOGGER.info(
            'analysis rerank context: recall_candidates=%s candidate_cards=%s effective_operation=%s',
            len(candidates),
            len(candidate_cards),
            effective_operation,
        )
        payload = {
            'request': self._request_payload(requirements),
            'user_operation': user_operation,
            'effective_operation': effective_operation,
            'effective_insert_scope': insert_scope,
            'search_plan': self._compact_search_plan(search_plan or {}),
            'operation_definitions': self._operation_definitions(),
            'candidate_cards': candidate_cards
        }
        user_prompt, prompt_budget = self._build_limited_rerank_prompt(payload)
        call = self.client.chat_json(
            step='analyze_candidate_rerank',
            system_prompt=self.system_prompt,
            user_prompt=user_prompt,
            max_prompt_chars=int(prompt_budget.get('max_prompt_chars') or self.config.analysis_llm_rerank_max_prompt_chars),
        )
        parsed = self._parse_json_object(call.content)
        parsed.setdefault('llm_usage', call.usage_dict())
        parsed.setdefault('prompt_budget', prompt_budget)
        return parsed


    def _build_limited_search_plan_prompt(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        mutable_payload = deepcopy(payload)
        limit = self.config.analysis_llm_search_plan_max_prompt_chars or self.config.analysis_llm_max_prompt_chars
        trim_steps: list[str] = []
        while True:
            user_prompt = self._render(self.search_plan_template, {'payload_json': self._json(mutable_payload)})
            prompt_chars = len(self.system_prompt) + len(user_prompt)
            if prompt_chars <= limit:
                self._log_prompt_budget('analyze_search_plan', prompt_chars, limit, trim_steps)
                return user_prompt, {'max_prompt_chars': limit, 'prompt_chars': prompt_chars, 'trim_steps': trim_steps}
            if not self._shrink_project_map(mutable_payload, trim_steps):
                self._log_prompt_budget('analyze_search_plan_too_large', prompt_chars, limit, trim_steps)
                raise ValueError(f'analyze search plan prompt is too large after trimming: {prompt_chars} chars > {limit} chars')

    def _build_limited_rerank_prompt(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        mutable_payload = deepcopy(payload)
        limit = self.config.analysis_llm_rerank_max_prompt_chars or self.config.analysis_llm_max_prompt_chars
        soft_limit = int(limit * 1.03)
        trim_steps: list[str] = []
        while True:
            user_prompt = self._render(self.rerank_template, {'payload_json': self._json(mutable_payload)})
            prompt_chars = len(self.system_prompt) + len(user_prompt)
            if prompt_chars <= limit:
                self._log_prompt_budget('analyze_candidate_rerank', prompt_chars, limit, trim_steps)
                return user_prompt, {'max_prompt_chars': limit, 'prompt_chars': prompt_chars, 'trim_steps': trim_steps}

            if self._shrink_candidate_cards(mutable_payload, trim_steps):
                continue

            if prompt_chars <= soft_limit:
                trim_steps.append(f'soft_overflow_allowed:{prompt_chars}>{limit}')
                self._log_prompt_budget('analyze_candidate_rerank_soft_overflow', prompt_chars, soft_limit, trim_steps)
                return user_prompt, {
                    'max_prompt_chars': soft_limit,
                    'prompt_chars': prompt_chars,
                    'trim_steps': trim_steps,
                }

            self._log_prompt_budget('analyze_candidate_rerank_too_large', prompt_chars, soft_limit, trim_steps)
            raise ValueError(
                f'analyze candidate rerank prompt is too large after trimming: '
                f'{prompt_chars} chars > {soft_limit} chars'
            )

    def _shrink_project_map(self, payload: dict[str, Any], trim_steps: list[str]) -> bool:
        project_map = payload.get('project_map') or []
        if not isinstance(project_map, list) or not project_map:
            return False

        max_symbols = max((len(item.get('symbols') or []) for item in project_map if isinstance(item, dict)), default=0)
        if max_symbols > self.config.analysis_project_map_min_symbols_per_file:
            next_count = max(self.config.analysis_project_map_min_symbols_per_file, max_symbols // 2)
            for item in project_map:
                if isinstance(item, dict) and isinstance(item.get('symbols'), list):
                    item['symbols'] = item['symbols'][:next_count]
            trim_steps.append(f'project_map_symbols_per_file:{max_symbols}->{next_count}')
            return True

        if len(project_map) > self.config.analysis_project_map_min_files:
            next_count = max(self.config.analysis_project_map_min_files, len(project_map) // 2)
            payload['project_map'] = project_map[:next_count]
            trim_steps.append(f'project_map_files:{len(project_map)}->{next_count}')
            return True

        removed_docstrings = False
        for item in project_map:
            if not isinstance(item, dict):
                continue
            if item.get('module_docstring'):
                item['module_docstring'] = ''
                removed_docstrings = True
            for symbol in item.get('symbols') or []:
                if isinstance(symbol, dict) and symbol.get('docstring'):
                    symbol['docstring'] = ''
                    removed_docstrings = True
        if removed_docstrings:
            trim_steps.append('project_map_docstrings:removed')
        return removed_docstrings

    def _shrink_candidate_cards(self, payload: dict[str, Any], trim_steps: list[str]) -> bool:
        cards = payload.get('candidate_cards') or []
        if not isinstance(cards, list) or not cards:
            return False

        if len(cards) > self.config.analysis_min_candidate_cards:
            next_count = max(self.config.analysis_min_candidate_cards, len(cards) // 2)
            payload['candidate_cards'] = cards[:next_count]
            trim_steps.append(f'candidate_cards:{len(cards)}->{next_count}')
            return True

        if self._truncate_card_field(cards, 'source_excerpt', self.config.analysis_candidate_source_min_chars):
            trim_steps.append(f'candidate_source_chars->{self.config.analysis_candidate_source_min_chars}')
            return True

        related_changed = False
        for card in cards:
            for test in card.get('related_tests') or []:
                if isinstance(test, dict):
                    before = str(test.get('source_excerpt') or '')
                    after = self._truncate(before, self.config.analysis_candidate_related_test_min_chars)
                    if after != before:
                        test['source_excerpt'] = after
                        related_changed = True
        if related_changed:
            trim_steps.append(f'related_test_chars->{self.config.analysis_candidate_related_test_min_chars}')
            return True

        siblings_changed = False
        for card in cards:
            siblings = card.get('siblings') or []
            if isinstance(siblings, list) and len(siblings) > self.config.analysis_candidate_min_siblings:
                card['siblings'] = siblings[: self.config.analysis_candidate_min_siblings]
                siblings_changed = True
        if siblings_changed:
            trim_steps.append(f'siblings->{self.config.analysis_candidate_min_siblings}')
            return True

        if self._drop_card_field(cards, 'search_reasons'):
            trim_steps.append('search_reasons:removed')
            return True
        if self._drop_card_field(cards, 'inbound_relations') or self._drop_card_field(cards, 'outbound_relations'):
            trim_steps.append('relations:removed')
            return True
        if self._drop_card_field(cards, 'related_tests'):
            trim_steps.append('related_tests:removed')
            return True
        return False

    def _truncate_card_field(self, cards: list[dict[str, Any]], field_name: str, limit: int) -> bool:
        changed = False
        for card in cards:
            before = str(card.get(field_name) or '')
            after = self._truncate(before, limit)
            if after != before:
                card[field_name] = after
                changed = True
        return changed

    def _drop_card_field(self, cards: list[dict[str, Any]], field_name: str) -> bool:
        changed = False
        for card in cards:
            if card.get(field_name):
                card[field_name] = []
                changed = True
        return changed

    def _log_prompt_budget(self, step: str, prompt_chars: int, limit: int, trim_steps: list[str]) -> None:
        LOGGER.info(
            'analysis prompt budget step=%s prompt_chars=%s max_prompt_chars=%s trim_steps=%s',
            step,
            prompt_chars,
            limit,
            trim_steps,
        )

    def reorder_candidates(
        self,
        candidates: list[SearchCandidate],
        recommendation: dict[str, Any] | None,
    ) -> list[SearchCandidate]:
        if not recommendation:
            return candidates

        ranked_items = self._ranked_candidate_items(recommendation)
        if not ranked_items:
            return self._move_recommended_candidate(candidates, recommendation)

        id_to_candidate = {f'c{index}': candidate for index, candidate in enumerate(candidates[: self.config.analysis_max_candidate_cards], start=1)}
        ranked: list[SearchCandidate] = []
        used_qualnames: set[str] = set()
        for rank, item in enumerate(ranked_items, start=1):
            candidate_id = str(item.get('candidate_id') or '').strip()
            candidate = id_to_candidate.get(candidate_id)
            if candidate is None or candidate.qualname in used_qualnames:
                continue
            reason = str(item.get('reason') or '').strip()
            recommended = candidate.qualname == str(recommendation.get('recommended_target') or '').strip() or rank == 1
            reasons = list(candidate.reasons)
            if recommended:
                target_reason = str(recommendation.get('target_reason') or reason or '').strip()
                reasons.insert(0, f'LLM-рекомендация: {target_reason}' if target_reason else 'LLM-рекомендация по расширенному analyze')
            elif reason:
                reasons.insert(0, f'LLM-rerank: {reason}')
            ranked.append(
                replace(
                    candidate,
                    ranked_by_llm=True,
                    llm_recommended=recommended,
                    llm_rank=rank,
                    llm_reason=reason or str(recommendation.get('target_reason') or '').strip(),
                    confidence=max(candidate.confidence, min(1.0, self._safe_float(recommendation.get('target_confidence')) if recommended else candidate.confidence)),
                    relevance_category='высокая' if recommended else candidate.relevance_category,
                    reasons=self._dedupe(reasons)[:8],
                )
            )
            used_qualnames.add(candidate.qualname)

        if not ranked:
            return self._move_recommended_candidate(candidates, recommendation)

        remainder = [candidate for candidate in candidates if candidate.qualname not in used_qualnames]
        return ranked + remainder

    def _ranked_candidate_items(self, recommendation: dict[str, Any]) -> list[dict[str, Any]]:
        ranked = recommendation.get('ranked_candidates') or []
        if isinstance(ranked, list):
            items = [item for item in ranked if isinstance(item, dict) and str(item.get('candidate_id') or '').strip()]
            if items:
                return items[: self.config.analysis_max_return_candidates]

        result: list[dict[str, Any]] = []
        recommended_id = str(recommendation.get('recommended_candidate_id') or '').strip()
        if recommended_id:
            result.append({'candidate_id': recommended_id, 'reason': str(recommendation.get('target_reason') or '').strip()})
        for alt in recommendation.get('alternatives') or []:
            if not isinstance(alt, dict):
                continue
            candidate_id = str(alt.get('candidate_id') or '').strip()
            if candidate_id and all(item.get('candidate_id') != candidate_id for item in result):
                result.append({'candidate_id': candidate_id, 'reason': str(alt.get('reason') or '').strip()})
        return result[: self.config.analysis_max_return_candidates]

    def _move_recommended_candidate(
        self,
        candidates: list[SearchCandidate],
        recommendation: dict[str, Any],
    ) -> list[SearchCandidate]:
        target = str(recommendation.get('recommended_target') or '').strip()
        confidence = self._safe_float(recommendation.get('target_confidence'))
        if not target or confidence < self.config.analysis_min_confidence_auto_recommend_target:
            return candidates
        result: list[SearchCandidate] = []
        moved: SearchCandidate | None = None
        for candidate in candidates:
            if candidate.qualname == target and moved is None:
                reasons = list(candidate.reasons)
                reason = str(recommendation.get('target_reason') or '').strip()
                reasons.insert(0, f'LLM-рекомендация: {reason}' if reason else 'LLM-рекомендация по расширенному analyze')
                moved = replace(
                    candidate,
                    ranked_by_llm=True,
                    llm_recommended=True,
                    llm_rank=1,
                    llm_reason=reason,
                    confidence=max(candidate.confidence, min(1.0, confidence)),
                    relevance_category='высокая' if confidence >= 0.7 else candidate.relevance_category,
                    reasons=self._dedupe(reasons)[:8],
                )
            else:
                result.append(candidate)
        if moved is None:
            return candidates
        return [moved] + result

    def _project_map(self, services: ProjectServices) -> list[dict[str, Any]]:
        project_key = str(services.project_root)
        modules = [symbol for symbol in services.store.list_symbols(project_key) if symbol.kind == 'module']
        modules.sort(key=lambda item: item.file_path)
        result: list[dict[str, Any]] = []
        for module in modules[: self.config.analysis_project_map_max_files]:
            symbols = [item for item in services.store.list_symbols_in_file(project_key, module.file_path) if item.kind != 'module']
            symbols.sort(key=lambda item: (item.start_line, item.qualname))
            result.append(
                {
                    'file_path': module.file_path,
                    'module_qualname': module.qualname,
                    'module_docstring': self._truncate(module.docstring, self.config.analysis_project_map_module_doc_chars),
                    'symbols': [
                        {
                            'qualname': symbol.qualname,
                            'kind': symbol.kind,
                            'signature': self._signature(symbol),
                            'docstring': self._truncate(symbol.docstring, self.config.analysis_project_map_symbol_doc_chars),
                        }
                        for symbol in symbols[: self.config.analysis_project_map_symbols_per_file]
                    ],
                }
            )
        return result

    def _candidate_cards(self, candidates: list[SearchCandidate], services: ProjectServices) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates[: self.config.analysis_max_candidate_cards], start=1):
            try:
                context = services.context(candidate.qualname)
                cards.append(self._candidate_card(f'c{index}', candidate, context, services))
            except Exception as exc:
                LOGGER.warning('Failed to build candidate card for %s: %s', candidate.qualname, exc)
                cards.append(
                    {
                        'candidate_id': f'c{index}',
                        'qualname': candidate.qualname,
                        'kind': candidate.kind,
                        'file_path': candidate.file_path,
                        'docstring': candidate.docstring,
                        'search_score': candidate.score,
                        'search_reasons': candidate.reasons[: self.config.analysis_candidate_search_reasons],
                    }
                )
        LOGGER.info('analysis candidate cards built: cards=%s', len(cards))
        return cards

    def _candidate_card(
        self,
        candidate_id: str,
        candidate: SearchCandidate,
        context: ContextPack,
        services: ProjectServices,
    ) -> dict[str, Any]:
        target = context.target
        file_symbols = services.store.list_symbols_in_file(str(services.project_root), target.file_path)
        siblings = [symbol for symbol in file_symbols if symbol.qualname != target.qualname and symbol.kind != 'module']
        siblings.sort(key=lambda item: (item.start_line, item.qualname))
        module_symbol = next((symbol for symbol in file_symbols if symbol.kind == 'module'), None)
        return {
            'candidate_id': candidate_id,
            'qualname': candidate.qualname,
            'name': candidate.name,
            'kind': candidate.kind,
            'file_path': candidate.file_path,
            'parent_qualname': getattr(context.target, 'parent_qualname', None),
            'module_docstring': self._truncate(module_symbol.docstring if module_symbol else '', self.config.analysis_candidate_module_doc_chars),
            'docstring': candidate.docstring,
            'knowledge_title': candidate.knowledge_title,
            'requirements': candidate.requirements,
            'source_excerpt': self._truncate(target.source_code, self.config.analysis_candidate_source_chars),
            'class_members': self._class_members(target, file_symbols),
            'siblings': [
                {
                    'qualname': symbol.qualname,
                    'kind': symbol.kind,
                    'signature': self._signature(symbol),
                    'docstring': self._truncate(symbol.docstring, self.config.analysis_candidate_sibling_doc_chars),
                }
                for symbol in siblings[: self.config.analysis_candidate_siblings]
            ],
            'inbound_relations': [self._relation_summary(item) for item in context.inbound_relations[: self.config.analysis_candidate_relation_limit]],
            'outbound_relations': [self._relation_summary(item) for item in context.outbound_relations[: self.config.analysis_candidate_relation_limit]],
            'related_tests': [
                {
                    'qualname': item.qualname,
                    'file_path': item.file_path,
                    'source_excerpt': self._truncate(item.source_code, self.config.analysis_candidate_related_test_chars),
                }
                for item in context.related_tests[: self.config.analysis_candidate_related_tests]
            ],
            'search_score': candidate.score,
            'search_confidence': candidate.confidence,
            'search_reasons': candidate.reasons[: self.config.analysis_candidate_search_reasons],
        }

    def _class_members(self, target: SymbolRecord, file_symbols: list[SymbolRecord]) -> list[dict[str, Any]]:
        if target.kind == 'class':
            parent_qualname = target.qualname
        else:
            parent_qualname = target.parent_qualname
        if not parent_qualname:
            return []
        members = [
            symbol
            for symbol in file_symbols
            if symbol.parent_qualname == parent_qualname and symbol.kind == 'method'
        ]
        members.sort(key=lambda item: (item.start_line, item.qualname))
        return [
            {
                'qualname': symbol.qualname,
                'name': symbol.name,
                'kind': symbol.kind,
                'signature': self._signature(symbol),
                'docstring': self._truncate(symbol.docstring, self.config.analysis_candidate_sibling_doc_chars),
            }
            for symbol in members[: self.config.analysis_candidate_siblings]
        ]

    def _compact_search_plan(self, search_plan: dict[str, Any]) -> dict[str, Any]:
        plan = search_plan.get('search_plan') or {}
        operation = search_plan.get('operation') or {}
        request_quality = search_plan.get('request_quality') or {}
        return {
            'request_quality': {
                'status': request_quality.get('status'),
                'missing_information': request_quality.get('missing_information') or [],
            },
            'operation': {
                'value': operation.get('value'),
                'confidence': operation.get('confidence'),
            },
            'insert_scope': search_plan.get('insert_scope') or {},
            'expected_new_symbols': search_plan.get('expected_new_symbols') or [],
            'search_plan': {
                'search_queries': (plan.get('search_queries') or [])[: self.config.analysis_max_search_plan_queries],
                'preferred_files': plan.get('preferred_files') or [],
                'preferred_qualnames': plan.get('preferred_qualnames') or [],
                'preferred_layers': plan.get('preferred_layers') or [],
                'avoid_layers': plan.get('avoid_layers') or [],
            },
            'multi_target_likely': bool(search_plan.get('multi_target_likely')),
            'manual_review_required': bool(search_plan.get('manual_review_required')),
        }

    def _relation_summary(self, relation: Any) -> dict[str, Any]:
        return {
            'source_qualname': getattr(relation, 'source_qualname', ''),
            'relation_kind': getattr(relation, 'relation_kind', ''),
            'target_ref': getattr(relation, 'target_ref', ''),
            'target_qualname': getattr(relation, 'target_qualname', None),
            'confidence': getattr(relation, 'relation_confidence', ''),
        }

    def _request_payload(self, requirements: list[dict[str, Any]]) -> dict[str, Any]:
        titles: list[str] = []
        descriptions: list[str] = []
        constraints: list[str] = []
        for item in requirements:
            title = str(item.get('title') or '').strip()
            description = str(item.get('description') or '').strip()
            if title:
                titles.append(title)
            if description:
                descriptions.append(description)
            constraints.extend(str(value).strip() for value in item.get('constraints', []) or [] if str(value).strip())
        return {
            'titles': titles,
            'descriptions': descriptions,
            'constraints': constraints,
        }

    def _operation_definitions(self) -> list[dict[str, str]]:
        return [
            {
                'name': 'replace_symbol',
                'meaning': 'заменить существующий symbol целиком; выбранный target является изменяемым symbol',
            },
            {
                'name': 'insert_after_symbol',
                'meaning': 'добавить новый class/function после существующего anchor-symbol; выбранный target является anchor, а не изменяемым symbol',
            },
        ]

    def _read_template(self, relative_path: str) -> str:
        path = (self.tool_root / relative_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f'Analysis prompt template not found: {path}')
        return path.read_text(encoding='utf-8')

    def _render(self, template: str, values: dict[str, str]) -> str:
        return Template(template).safe_substitute(values)

    def _parse_json_object(self, content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`')
            if cleaned.startswith('json'):
                cleaned = cleaned[4:].strip()
        try:
            payload = json.loads(cleaned)
            return payload if isinstance(payload, dict) else {'raw_value': payload}
        except json.JSONDecodeError:
            match = _JSON_OBJECT_RE.search(cleaned)
            if not match:
                raise
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else {'raw_value': payload}

    def _json(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _signature(self, symbol: SymbolRecord) -> str:
        for line in symbol.source_code.splitlines():
            stripped = line.strip()
            if stripped.startswith('def ') or stripped.startswith('class '):
                return stripped
        return symbol.name

    def _truncate(self, value: str, limit: int) -> str:
        text = str(value or '')
        if limit <= 0 or len(text) <= limit:
            return text
        return text[: max(limit - 16, 0)].rstrip() + '\n# ... truncated ...'

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
