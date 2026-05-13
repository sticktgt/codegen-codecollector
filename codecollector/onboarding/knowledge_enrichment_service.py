from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from string import Template
from typing import Any

from codecollector.analysis.llm_client import AnalysisLlmClient
from codecollector.config import AppConfig
from codecollector.domain.models import SymbolRecord
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)
_JSON_OBJECT_RE = re.compile(r'\{.*\}', re.DOTALL)
_JSON_FENCE_RE = re.compile(r'^```(?:json)?\s*|\s*```$', re.IGNORECASE)


@dataclass(slots=True)
class KnowledgeEnrichmentResult:
    status: str
    source_path: str = ''
    payload: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    unmatched_mentions: list[dict[str, Any]] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)
    prompt_budget: dict[str, Any] = field(default_factory=dict)
    trace_path: str = ''

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _PromptCandidate:
    system_prompt: str
    user_prompt: str
    prompt_chars: int
    budget: dict[str, Any]


class _PromptBudgetError(ValueError):
    def __init__(self, message: str, budget: dict[str, Any]) -> None:
        super().__init__(message)
        self.budget = budget


class KnowledgeEnrichmentService:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.client = AnalysisLlmClient(config)

    def enrich_from_architect_file(
        self,
        *,
        architect_path: Path | None,
        project_root: Path,
        symbols: list[SymbolRecord],
    ) -> KnowledgeEnrichmentResult:
        if architect_path is None:
            return KnowledgeEnrichmentResult(status='skipped', warnings=['Файл архитектурного описания не передан для onboarding enrichment.'])

        resolved_architect_path = architect_path.resolve()
        if not resolved_architect_path.exists() or not resolved_architect_path.is_file():
            return KnowledgeEnrichmentResult(
                status='skipped',
                source_path=str(resolved_architect_path),
                warnings=[f'Файл архитектурного описания не найден: {resolved_architect_path}'],
            )

        if not self.config.analysis_llm_enabled:
            return KnowledgeEnrichmentResult(
                status='disabled',
                source_path=str(resolved_architect_path),
                warnings=['LLM-assisted enrichment отключен через analysis.llm_assist.enabled=false.'],
            )

        try:
            raw_text = resolved_architect_path.read_text(encoding='utf-8')
        except Exception as exc:
            LOGGER.warning('Failed to read architecture document %s: %s', resolved_architect_path, exc)
            return KnowledgeEnrichmentResult(
                status='failed',
                source_path=str(resolved_architect_path),
                warnings=[f'Не удалось прочитать архитектурное описание: {exc}'],
            )

        if not raw_text.strip():
            return KnowledgeEnrichmentResult(
                status='skipped',
                source_path=str(resolved_architect_path),
                warnings=['Файл архитектурного описания пуст.'],
            )

        try:
            prepared = self._prepare_prompt(
                architect_text=raw_text,
                project_root=project_root.resolve(),
                symbols=symbols,
            )
        except ValueError as exc:
            LOGGER.warning('knowledge enrichment prompt preparation failed for %s: %s', resolved_architect_path, exc)
            return KnowledgeEnrichmentResult(
                status='failed',
                source_path=str(resolved_architect_path),
                warnings=[str(exc)],
                prompt_budget=dict(getattr(exc, 'budget', {}) or {}),
            )

        max_prompt_chars = int(prepared.budget['max_prompt_chars'])
        soft_max_prompt_chars = int(prepared.budget['soft_max_prompt_chars'])
        warnings: list[str] = []
        if prepared.prompt_chars > max_prompt_chars:
            warnings.append(
                f'Prompt enrichment превышает целевой лимит, но укладывается в soft-limit: '
                f'{prepared.prompt_chars} > {max_prompt_chars} символов.'
            )

        call = None
        trace_path = ''
        try:
            LOGGER.info(
                'knowledge enrichment llm call prepared: prompt_chars=%s max_prompt_chars=%s soft_max_prompt_chars=%s num_predict=%s trim_steps=%s context_mode=%s',
                prepared.prompt_chars,
                max_prompt_chars,
                soft_max_prompt_chars,
                self.config.onboarding_architecture_enrichment_num_predict,
                prepared.budget.get('trim_steps', []),
                prepared.budget.get('context_mode', {}),
            )
            call = self.client.chat_json(
                step='knowledge_architecture_enrichment',
                system_prompt=prepared.system_prompt,
                user_prompt=prepared.user_prompt,
                max_prompt_chars=soft_max_prompt_chars,
                num_predict=self.config.onboarding_architecture_enrichment_num_predict,
            )
            LOGGER.info(
                'knowledge enrichment llm usage: prompt_chars=%s prompt_tokens=%s output_tokens=%s total_tokens=%s duration=%.2fs',
                call.prompt_chars,
                call.prompt_tokens,
                call.output_tokens,
                call.prompt_tokens + call.output_tokens,
                call.duration_sec,
            )
            incomplete_reason = self._incomplete_done_reason(call.done_reason)
            if incomplete_reason:
                raise RuntimeError(
                    'LLM ответ для knowledge enrichment не завершен корректно: '
                    f'done_reason={call.done_reason or "unknown"} output_tokens={call.output_tokens} '
                    f'num_predict={call.num_predict}. {incomplete_reason}'
                )
            parsed = self._parse_json_object(call.content)
            normalized = self._normalize_llm_payload(parsed, symbols)
            status = 'applied' if normalized.get('project') or normalized.get('modules') or normalized.get('symbols') or normalized.get('architecture') else 'empty'
            warnings.extend(str(item) for item in parsed.get('warnings') or [] if str(item).strip())
            warnings.extend(normalized.pop('_warnings', []))
            unmatched_mentions = normalized.pop('_unmatched_mentions', [])
            LOGGER.info(
                'knowledge enrichment finished: status=%s modules=%s symbols=%s warnings=%s unmatched=%s prompt_tokens=%s output_tokens=%s',
                status,
                len(normalized.get('modules') or {}),
                len(normalized.get('symbols') or {}),
                len(warnings),
                len(unmatched_mentions),
                call.prompt_tokens,
                call.output_tokens,
            )
            return KnowledgeEnrichmentResult(
                status=status,
                source_path=str(resolved_architect_path),
                payload=normalized,
                warnings=self._dedupe_strings(warnings),
                unmatched_mentions=unmatched_mentions,
                llm_usage=call.usage_dict(),
                prompt_budget={**prepared.budget, 'prompt_chars': call.prompt_chars},
                trace_path=trace_path,
            )
        except Exception as exc:
            if call is not None:
                trace_path = self._write_trace_file(
                    project_root=project_root.resolve(),
                    architect_path=resolved_architect_path,
                    content=call.content,
                    raw=call.raw,
                    error=str(exc),
                )
            LOGGER.warning('knowledge enrichment failed for %s: %s trace_path=%s', resolved_architect_path, exc, trace_path)
            usage = call.usage_dict() if call is not None else {}
            budget = {**prepared.budget, 'prompt_chars': call.prompt_chars} if call is not None else dict(prepared.budget)
            if trace_path:
                budget['trace_path'] = trace_path
            return KnowledgeEnrichmentResult(
                status='failed',
                source_path=str(resolved_architect_path),
                warnings=[f'LLM-enrichment knowledge завершился ошибкой: {exc}'],
                llm_usage=usage,
                prompt_budget=budget,
                trace_path=trace_path,
            )

    def _prepare_prompt(self, *, architect_text: str, project_root: Path, symbols: list[SymbolRecord]) -> _PromptCandidate:
        system_prompt = self._read_template(self.config.onboarding_architecture_enrichment_system_template)
        template = self._read_template(self.config.onboarding_architecture_enrichment_template)
        max_prompt_chars = self.config.onboarding_architecture_enrichment_max_prompt_chars
        soft_ratio = max(1.0, self.config.onboarding_architecture_enrichment_soft_overflow_ratio)
        soft_max_prompt_chars = int(max_prompt_chars * soft_ratio)
        trim_steps: list[str] = []

        doc_limit = min(len(architect_text), self.config.onboarding_architecture_doc_max_chars)
        min_doc_chars = self._minimum_architecture_doc_chars(doc_limit)
        variants = [
            {
                'module_docstrings': True,
                'symbol_docstrings': True,
                'symbols_mode': 'full',
                'architect_chars': doc_limit,
                'step': '',
            },
            {
                'module_docstrings': True,
                'symbol_docstrings': False,
                'symbols_mode': 'full',
                'architect_chars': doc_limit,
                'step': 'drop_symbol_docstrings',
            },
            {
                'module_docstrings': False,
                'symbol_docstrings': False,
                'symbols_mode': 'full',
                'architect_chars': doc_limit,
                'step': 'drop_module_docstrings',
            },
            {
                'module_docstrings': False,
                'symbol_docstrings': False,
                'symbols_mode': 'minimal',
                'architect_chars': doc_limit,
                'step': 'compact_symbol_fields',
            },
        ]

        seen_limits = {doc_limit}
        for ratio, step in (
            (0.75, 'truncate_architecture_document_75'),
            (0.50, 'truncate_architecture_document_50'),
            (0.35, 'truncate_architecture_document_35'),
            (0.25, 'truncate_architecture_document_25'),
        ):
            limit = max(min_doc_chars, int(doc_limit * ratio))
            if limit in seen_limits:
                continue
            seen_limits.add(limit)
            variants.append(
                {
                    'module_docstrings': False,
                    'symbol_docstrings': False,
                    'symbols_mode': 'minimal',
                    'architect_chars': limit,
                    'step': step,
                }
            )

        last_candidate: _PromptCandidate | None = None
        for variant in variants:
            step = str(variant['step'])
            active_steps = trim_steps + ([step] if step else [])
            payload = self._build_payload(
                architect_text=self._truncate(architect_text, int(variant['architect_chars'])),
                project_root=project_root,
                symbols=symbols,
                include_module_docstrings=bool(variant['module_docstrings']),
                include_symbol_docstrings=bool(variant['symbol_docstrings']),
                symbols_mode=str(variant['symbols_mode']),
                trim_steps=active_steps,
            )
            prompt_json = self._json(payload, indent=self.config.onboarding_architecture_enrichment_json_indent)
            user_prompt = Template(template).safe_substitute({'payload_json': prompt_json})
            prompt_chars = len(system_prompt) + len(user_prompt)
            candidate = _PromptCandidate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_chars=prompt_chars,
                budget={
                    'prompt_chars': prompt_chars,
                    'max_prompt_chars': max_prompt_chars,
                    'soft_max_prompt_chars': soft_max_prompt_chars,
                    'soft_overflow_ratio': soft_ratio,
                    'trim_steps': active_steps,
                    'context_mode': payload.get('context_completeness', {}),
                },
            )
            last_candidate = candidate
            if prompt_chars <= soft_max_prompt_chars:
                return candidate
            if step:
                trim_steps.append(step)

        last_budget = last_candidate.budget if last_candidate else {'max_prompt_chars': max_prompt_chars, 'soft_max_prompt_chars': soft_max_prompt_chars}
        raise _PromptBudgetError(
            'Prompt enrichment слишком большой после безопасного сжатия контекста: '
            f"{last_budget.get('prompt_chars', 0)} > {soft_max_prompt_chars} символов. "
            'Нужно увеличить лимит, уменьшить архитектурный документ или запустить onboarding с --skip-architecture-enrichment. Полный список symbols не был отброшен.',
            dict(last_budget),
        )

    def _minimum_architecture_doc_chars(self, doc_limit: int) -> int:
        if doc_limit <= 0:
            return 0
        configured_chars = max(0, int(self.config.onboarding_architecture_doc_min_chars))
        configured_ratio = max(0.0, min(1.0, float(self.config.onboarding_architecture_doc_min_ratio)))
        ratio_chars = int(doc_limit * configured_ratio)
        return min(doc_limit, max(configured_chars, ratio_chars))

    def _build_payload(
        self,
        *,
        architect_text: str,
        project_root: Path,
        symbols: list[SymbolRecord],
        include_module_docstrings: bool = True,
        include_symbol_docstrings: bool = True,
        symbols_mode: str = 'full',
        trim_steps: list[str] | None = None,
    ) -> dict[str, Any]:
        modules = sorted((item for item in symbols if item.kind == 'module'), key=lambda symbol: symbol.qualname)
        non_modules = sorted((item for item in symbols if item.kind != 'module'), key=lambda symbol: symbol.qualname)
        files = [
            self._without_empty({
                'file_path': item.file_path,
                'module': item.qualname,
                'docstring': self._truncate(item.docstring, self.config.onboarding_architecture_enrichment_docstring_chars) if include_module_docstrings else '',
            })
            for item in modules
        ]
        module_entries = [
            {'module': item.qualname, 'file_path': item.file_path}
            for item in modules
        ]
        symbol_entries: list[dict[str, Any]] = []
        for item in non_modules:
            if symbols_mode == 'minimal':
                entry = {
                    'qualname': item.qualname,
                    'kind': item.kind,
                    'module': item.module_name,
                }
            else:
                entry = {
                    'qualname': item.qualname,
                    'kind': item.kind,
                    'module': item.module_name,
                    'file_path': item.file_path,
                    'parent_qualname': item.parent_qualname,
                }
                if symbols_mode == 'full' and include_symbol_docstrings:
                    entry['docstring'] = self._truncate(item.docstring, self.config.onboarding_architecture_enrichment_docstring_chars)
            symbol_entries.append(self._without_empty(entry))

        return {
            'project_root_name': project_root.name,
            'architecture_document': architect_text,
            'known_files': files,
            'known_modules': module_entries,
            'known_symbols': symbol_entries,
            'context_completeness': {
                'known_files': 'complete',
                'known_modules': 'complete',
                'known_symbols': 'complete',
                'known_symbols_count': len(non_modules),
                'included_symbols_count': len(symbol_entries),
                'symbol_fields': 'minimal_qualname_kind_module' if symbols_mode == 'minimal' else symbols_mode,
                'module_docstrings': 'included' if include_module_docstrings else 'omitted',
                'symbol_docstrings': 'included' if include_symbol_docstrings and symbols_mode == 'full' else 'omitted',
                'trim_steps': trim_steps or [],
            },
            'output_contract': {
                'format': 'strict_json_object_only',
                'project': 'title, description',
                'modules': 'mapping by known module qualname: title, description, layer, keywords',
                'symbols': 'optional compact mapping by known symbol qualname only for key classes/functions explicitly described in architecture document: title, description, keywords',
                'architecture': 'style, patterns, layers, components, flows',
                'warnings': 'list of strings',
                'diagnostics': 'do not return unmatched_mentions; codecollector validates unknown references deterministically',
            },
        }

    def _normalize_llm_payload(self, payload: dict[str, Any], symbols: list[SymbolRecord]) -> dict[str, Any]:
        known_modules = {item.qualname for item in symbols if item.kind == 'module'}
        known_symbols = {item.qualname for item in symbols if item.kind != 'module'}
        module_by_file = {self._normalize_ref(item.file_path): item.qualname for item in symbols if item.kind == 'module'}
        module_by_tail = {item.qualname.split('.')[-1]: item.qualname for item in symbols if item.kind == 'module'}
        symbol_aliases = self._build_symbol_aliases(known_symbols)
        warnings: list[str] = []
        unmatched: list[dict[str, Any]] = []

        result: dict[str, Any] = {}

        project = payload.get('project') if isinstance(payload.get('project'), dict) else {}
        normalized_project = self._clean_mapping(project, allowed_keys={'title', 'description'})
        if normalized_project:
            result['project'] = normalized_project

        modules = payload.get('modules') if isinstance(payload.get('modules'), dict) else {}
        normalized_modules: dict[str, Any] = {}
        for raw_key, raw_value in modules.items():
            module_name = self._resolve_module_ref(str(raw_key), known_modules, module_by_file, module_by_tail)
            if not module_name:
                warnings.append(f'ARCHITECT.md содержит неизвестный модуль: {raw_key}')
                unmatched.append({'name': str(raw_key), 'kind': 'module', 'reason': 'module_not_found_in_index'})
                continue
            if not isinstance(raw_value, dict):
                continue
            entry = self._clean_mapping(raw_value, allowed_keys={'title', 'description', 'layer', 'keywords'})
            if entry:
                normalized_modules[module_name] = entry
        if normalized_modules:
            result['modules'] = normalized_modules

        raw_symbols = payload.get('symbols') if isinstance(payload.get('symbols'), dict) else {}
        normalized_symbols: dict[str, Any] = {}
        for raw_key, raw_value in raw_symbols.items():
            qualname = str(raw_key)
            if qualname not in known_symbols:
                warnings.append(f'ARCHITECT.md содержит неизвестный symbol: {raw_key}')
                unmatched.append({'name': str(raw_key), 'kind': 'symbol', 'reason': 'symbol_not_found_in_index'})
                continue
            if not isinstance(raw_value, dict):
                continue
            entry = self._clean_mapping(raw_value, allowed_keys={'title', 'description', 'keywords', 'requirements'})
            if entry:
                normalized_symbols[qualname] = entry
        if normalized_symbols:
            result['symbols'] = normalized_symbols

        architecture = payload.get('architecture') if isinstance(payload.get('architecture'), dict) else {}
        normalized_arch = self._normalize_architecture(
            architecture,
            known_modules=known_modules,
            known_symbols=known_symbols,
            module_by_file=module_by_file,
            module_by_tail=module_by_tail,
            symbol_aliases=symbol_aliases,
            warnings=warnings,
            unmatched=unmatched,
        )
        if normalized_arch:
            result['architecture'] = normalized_arch

        # Do not trust LLM-reported unmatched_mentions as project diagnostics.
        # Unknown modules, symbols and flow references are validated deterministically
        # against the real project index above.
        result['_warnings'] = self._dedupe_strings(warnings)
        result['_unmatched_mentions'] = self._dedupe_dicts(unmatched)
        return result

    def _normalize_architecture(
        self,
        architecture: dict[str, Any],
        *,
        known_modules: set[str],
        known_symbols: set[str],
        module_by_file: dict[str, str],
        module_by_tail: dict[str, str],
        symbol_aliases: dict[str, str],
        warnings: list[str],
        unmatched: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        style = str(architecture.get('style') or '').strip()
        if style:
            result['style'] = style
        patterns = self._string_list(architecture.get('patterns'))
        if patterns:
            result['patterns'] = patterns

        layers = architecture.get('layers') if isinstance(architecture.get('layers'), dict) else {}
        normalized_layers: dict[str, list[str]] = {}
        for raw_layer, raw_items in layers.items():
            layer = str(raw_layer).strip()
            if not layer:
                continue
            modules: list[str] = []
            for raw_ref in raw_items or []:
                module_name = self._resolve_module_ref(str(raw_ref), known_modules, module_by_file, module_by_tail)
                if module_name:
                    modules.append(module_name)
                    continue
                warnings.append(f'ARCHITECT.md содержит неизвестный модуль в слое {layer}: {raw_ref}')
                unmatched.append({'name': str(raw_ref), 'kind': 'module', 'layer': layer, 'reason': 'module_not_found_in_index'})
            if modules:
                normalized_layers[layer] = self._dedupe_strings(modules)
        if normalized_layers:
            result['layers'] = normalized_layers

        components = []
        for raw_component in architecture.get('components') or []:
            if not isinstance(raw_component, dict):
                continue
            module_name = self._resolve_module_ref(str(raw_component.get('module') or ''), known_modules, module_by_file, module_by_tail)
            qualname = str(raw_component.get('qualname') or '').strip()
            if qualname and qualname not in known_symbols:
                warnings.append(f'ARCHITECT.md содержит неизвестный symbol компонента: {qualname}')
                unmatched.append({'name': qualname, 'kind': 'symbol', 'reason': 'component_symbol_not_found_in_index'})
                qualname = ''
            if not module_name:
                raw_name = str(raw_component.get('name') or raw_component.get('module') or '').strip()
                if raw_name:
                    unmatched.append({'name': raw_name, 'kind': 'component', 'reason': 'component_module_not_found_in_index'})
                continue
            component = {
                'name': str(raw_component.get('name') or '').strip(),
                'module': module_name,
                'qualname': qualname,
                'layer': str(raw_component.get('layer') or '').strip(),
                'responsibilities': self._string_list(raw_component.get('responsibilities')),
            }
            components.append({key: value for key, value in component.items() if value not in ('', [])})
        if components:
            result['components'] = components

        flows = []
        for raw_flow in architecture.get('flows') or []:
            if not isinstance(raw_flow, dict):
                continue
            flow = {
                'name': str(raw_flow.get('name') or '').strip(),
                'steps': self._string_list(raw_flow.get('steps')),
            }
            if flow['name'] or flow['steps']:
                normalized_flow = {key: value for key, value in flow.items() if value not in ('', [])}
                self._validate_flow_step_references(
                    flow_name=flow['name'],
                    steps=flow['steps'],
                    known_modules=known_modules,
                    known_symbols=known_symbols,
                    module_by_file=module_by_file,
                    module_by_tail=module_by_tail,
                    symbol_aliases=symbol_aliases,
                    warnings=warnings,
                    unmatched=unmatched,
                )
                flows.append(normalized_flow)
        if flows:
            result['flows'] = flows
        return result

    def _validate_flow_step_references(
        self,
        *,
        flow_name: str,
        steps: list[str],
        known_modules: set[str],
        known_symbols: set[str],
        module_by_file: dict[str, str],
        module_by_tail: dict[str, str],
        symbol_aliases: dict[str, str],
        warnings: list[str],
        unmatched: list[dict[str, Any]],
    ) -> None:
        for step_index, step in enumerate(steps, start=1):
            for ref in self._extract_flow_reference_candidates(step):
                resolution = self._resolve_flow_reference(
                    ref,
                    known_modules=known_modules,
                    known_symbols=known_symbols,
                    module_by_file=module_by_file,
                    module_by_tail=module_by_tail,
                    symbol_aliases=symbol_aliases,
                )
                if resolution:
                    continue
                suffix_matches = self._suffix_symbol_matches(ref, known_symbols)
                if len(suffix_matches) == 1:
                    continue
                if len(suffix_matches) > 1:
                    warnings.append(f'ARCHITECT.md содержит неоднозначную ссылку в architecture.flows: {ref}')
                    unmatched.append({
                        'name': ref,
                        'kind': 'flow_step_reference',
                        'reason': 'ambiguous_reference',
                        'source': 'architecture.flows',
                        'flow': flow_name,
                        'step_index': step_index,
                        'step': step,
                        'suggested_matches': suffix_matches[:5],
                    })
                    continue
                suggested = self._suggest_symbol_matches(ref, known_modules, known_symbols)
                reason = 'reference_not_found_exactly_but_similar_symbols_exist' if suggested else 'reference_not_found_in_index'
                warnings.append(f'ARCHITECT.md содержит неизвестную ссылку в architecture.flows: {ref}')
                unmatched.append({
                    'name': ref,
                    'kind': 'flow_step_reference',
                    'reason': reason,
                    'source': 'architecture.flows',
                    'flow': flow_name,
                    'step_index': step_index,
                    'step': step,
                    'suggested_matches': suggested,
                })

    def _extract_flow_reference_candidates(self, step: str) -> list[str]:
        text = str(step or '')
        candidates: list[str] = []
        for raw_ref in re.findall(r'`([^`]+)`', text):
            cleaned = raw_ref.strip()
            if self._should_validate_flow_reference(cleaned):
                candidates.append(cleaned)
        for raw_ref in re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b', text):
            cleaned = raw_ref.strip()
            if self._should_validate_flow_reference(cleaned):
                candidates.append(cleaned)
        return self._dedupe_strings(candidates)

    def _should_validate_flow_reference(self, ref: str) -> bool:
        value = ref.strip().strip('`')
        if not value or '.' not in value:
            return False
        if value.startswith(('self.', 'cls.')):
            return False
        parts = value.split('.')
        if len(parts) >= 3:
            return True
        if value.endswith('.py'):
            return True
        return bool(parts[0] and parts[0][0].isupper())

    def _resolve_flow_reference(
        self,
        ref: str,
        *,
        known_modules: set[str],
        known_symbols: set[str],
        module_by_file: dict[str, str],
        module_by_tail: dict[str, str],
        symbol_aliases: dict[str, str],
    ) -> str:
        value = ref.strip().strip('`')
        if not value:
            return ''
        if value in known_symbols:
            return value
        if value in symbol_aliases:
            return symbol_aliases[value]
        module_ref = self._resolve_module_ref(value, known_modules, module_by_file, module_by_tail)
        if module_ref:
            return module_ref
        return ''

    def _build_symbol_aliases(self, known_symbols: set[str]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        conflicts: set[str] = set()
        for qualname in known_symbols:
            parts = qualname.split('.')
            for size in range(1, min(4, len(parts)) + 1):
                alias = '.'.join(parts[-size:])
                if alias in aliases and aliases[alias] != qualname:
                    conflicts.add(alias)
                    continue
                aliases[alias] = qualname
        for alias in conflicts:
            aliases.pop(alias, None)
        return aliases

    def _suffix_symbol_matches(self, ref: str, known_symbols: set[str]) -> list[str]:
        value = ref.strip().strip('`')
        if not value:
            return []
        return sorted(candidate for candidate in known_symbols if candidate.endswith(f'.{value}'))

    def _suggest_symbol_matches(self, ref: str, known_modules: set[str], known_symbols: set[str]) -> list[str]:
        import difflib

        value = ref.strip().strip('`')
        tail = value.split('.')[-1]
        candidates = sorted(known_symbols | known_modules)
        direct_tail_matches = [candidate for candidate in candidates if candidate.endswith(f'.{tail}') or candidate == tail]
        close_matches = difflib.get_close_matches(value, candidates, n=5, cutoff=0.55)
        result = []
        for candidate in direct_tail_matches + close_matches:
            if candidate not in result:
                result.append(candidate)
        return result[:5]

    def _resolve_module_ref(
        self,
        raw_ref: str,
        known_modules: set[str],
        module_by_file: dict[str, str],
        module_by_tail: dict[str, str],
    ) -> str:
        ref = raw_ref.strip().strip('`')
        if not ref:
            return ''
        if ref in known_modules:
            return ref
        normalized = self._normalize_ref(ref)
        if normalized in module_by_file:
            return module_by_file[normalized]
        if normalized.endswith('.py'):
            as_module = normalized[:-3].replace('/', '.').replace('\\', '.')
            if as_module in known_modules:
                return as_module
        basename = Path(normalized).stem if ('.' in normalized or '/' in normalized or '\\' in normalized) else normalized
        if basename in module_by_tail:
            return module_by_tail[basename]
        return ''

    def _clean_mapping(self, payload: dict[str, Any], *, allowed_keys: set[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in allowed_keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if key in {'keywords', 'requirements'}:
                values = self._string_list(value)
                if values:
                    result[key] = values
            else:
                text = str(value or '').strip()
                if text:
                    result[key] = text
        return result

    def _read_template(self, relative_path: str) -> str:
        path = (self.tool_root / relative_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f'Knowledge enrichment prompt template not found: {path}')
        return path.read_text(encoding='utf-8')

    def _incomplete_done_reason(self, done_reason: str) -> str:
        reason = str(done_reason or '').strip().lower()
        if not reason or reason == 'stop':
            return ''
        if reason == 'length':
            return (
                'Ответ оборван по лимиту генерации. Увеличьте '
                'onboarding.knowledge.architecture_enrichment_num_predict или сократите ожидаемый ответ в prompt.'
            )
        if reason == 'abort':
            return (
                'Модель или endpoint прервали генерацию до полного JSON-ответа. '
                'Повторите onboarding; если ошибка повторяется, попробуйте увеличить '
                'onboarding.knowledge.architecture_enrichment_num_predict, уменьшить prompt budget или запустить '
                'onboarding с --skip-architecture-enrichment.'
            )
        return (
            'Endpoint вернул нестандартную причину завершения генерации. Проверьте trace-файл и настройки LLM.'
        )

    def _parse_json_object(self, content: str) -> dict[str, Any]:
        cleaned = self._strip_json_fence(content)
        candidates = [cleaned]
        balanced = self._extract_balanced_json_object(cleaned)
        if balanced and balanced != cleaned:
            candidates.append(balanced)
        match = _JSON_OBJECT_RE.search(cleaned)
        if match and match.group(0) not in candidates:
            candidates.append(match.group(0))

        last_error: json.JSONDecodeError | None = None
        for candidate in candidates:
            for variant in self._json_variants(candidate):
                try:
                    payload = json.loads(variant)
                    return payload if isinstance(payload, dict) else {'raw_value': payload}
                except json.JSONDecodeError as exc:
                    last_error = exc
        if last_error:
            raise last_error
        raise json.JSONDecodeError('No JSON object found', cleaned, 0)

    def _strip_json_fence(self, content: str) -> str:
        text = str(content or '').strip()
        if text.startswith('```'):
            text = _JSON_FENCE_RE.sub('', text).strip()
        return text

    def _json_variants(self, text: str) -> list[str]:
        variants = [text]
        queue = [text]
        seen = {text}
        while queue:
            current = queue.pop(0)
            for variant in (
                self._remove_trailing_commas(current),
                self._insert_missing_object_commas(current),
                self._normalize_json_quotes(current),
            ):
                if variant != current and variant not in seen:
                    seen.add(variant)
                    variants.append(variant)
                    queue.append(variant)
        return variants[:12]

    def _remove_trailing_commas(self, text: str) -> str:
        return re.sub(r',\s*([}\]])', r'\1', text)

    def _insert_missing_object_commas(self, text: str) -> str:
        return re.sub(
            r'([}\]"0-9eE]|true|false|null)(\s*\n\s*)("[A-Za-z_][^"\n]*"\s*:)',
            r'\1,\2\3',
            text,
        )

    def _normalize_json_quotes(self, text: str) -> str:
        return text.replace('“', '"').replace('”', '"').replace('’', "'")

    def _extract_balanced_json_object(self, text: str) -> str | None:
        start = text.find('{')
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    return text[start:pos + 1]
        return None

    def _write_trace_file(self, *, project_root: Path, architect_path: Path, content: str, raw: dict[str, Any], error: str) -> str:
        try:
            trace_dir = self.tool_root / self.config.runs_root_dirname / 'knowledge_enrichment_traces'
            trace_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
            path = trace_dir / f'knowledge_enrichment_failed_{stamp}.json'
            payload = {
                'source_path': str(architect_path),
                'project_root': str(project_root),
                'error': error,
                'content': content,
                'raw_response': raw,
                'done_reason': str(raw.get('done_reason') or ''),
                'prompt_eval_count': int(raw.get('prompt_eval_count') or 0),
                'eval_count': int(raw.get('eval_count') or 0),
            }
            path.write_text(self._json(payload, indent=2), encoding='utf-8')
            return str(path)
        except Exception as trace_exc:  # pragma: no cover - diagnostic fallback only
            LOGGER.warning('Failed to write knowledge enrichment trace: %s', trace_exc)
            return ''

    def _normalize_ref(self, value: str) -> str:
        return value.strip().strip('`').replace('\\', '/')

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            raw_values = value
        elif isinstance(value, str):
            raw_values = [value]
        else:
            raw_values = []
        return self._dedupe_strings(str(item).strip() for item in raw_values if str(item).strip())

    def _string_or_list(self, value: Any) -> Any:
        if isinstance(value, list):
            return self._string_list(value)
        if isinstance(value, dict):
            return {str(k): self._string_or_list(v) for k, v in value.items()}
        return str(value).strip()

    def _dedupe_strings(self, values: Any) -> list[str]:
        result: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _dedupe_dicts(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            key = self._json(value, indent=0)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    def _truncate(self, value: str, limit: int) -> str:
        text = str(value or '')
        if limit <= 0 or len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + '...'

    def _without_empty(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if value not in ('', None, [])}

    def _json(self, payload: Any, *, indent: int | None = 2) -> str:
        effective_indent = None if indent is not None and indent <= 0 else indent
        return json.dumps(payload, ensure_ascii=False, indent=effective_indent, sort_keys=False)
