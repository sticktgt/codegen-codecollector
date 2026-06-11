from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.domain.models import SymbolRecord
from codecollector.overlays.service import OverlayService


class KnowledgeBuilder:
    def __init__(self, project_root: Path, config: AppConfig) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.overlays = OverlayService(self.project_root, overlay_dirname=self.config.overlay_dirname)

    def rebuild(self, symbols: list[SymbolRecord], enrichment: dict[str, Any] | None = None) -> dict[str, Any]:
        existing = self._load_existing()
        payload = {
            'version': 1,
            'project': self._build_project_section(existing),
            'modules': self._build_modules(symbols, existing),
            'symbols': self._build_symbols(symbols, existing),
            'requirements': existing.get('requirements', {}) if isinstance(existing.get('requirements', {}), dict) else {},
            'architecture': self._build_architecture(symbols, existing),
        }
        enrichment_warnings: list[str] = []
        if enrichment:
            enrichment_warnings = self._apply_enrichment(payload, symbols, enrichment)
        self.overlays.knowledge_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding='utf-8',
        )
        self.overlays.refresh()
        return {
            'knowledge_path': str(self.overlays.knowledge_path),
            'knowledge_updated': True,
            'module_entries': len(payload['modules']),
            'symbol_entries': len(payload['symbols']),
            'requirements_count': len(payload['requirements']),
            'enrichment_applied': bool(enrichment),
            'enrichment_warnings': enrichment_warnings,
        }

    def _apply_enrichment(self, payload: dict[str, Any], symbols: list[SymbolRecord], enrichment: dict[str, Any]) -> list[str]:
        known_modules = {symbol.qualname for symbol in symbols if symbol.kind == 'module'}
        known_symbols = {symbol.qualname for symbol in symbols if symbol.kind != 'module'}
        warnings: list[str] = []

        project = enrichment.get('project') if isinstance(enrichment.get('project'), dict) else {}
        for key in ('title', 'description'):
            value = str(project.get(key) or '').strip()
            if value:
                payload['project'][key] = value

        modules = enrichment.get('modules') if isinstance(enrichment.get('modules'), dict) else {}
        for module_name, raw_entry in modules.items():
            if module_name not in known_modules:
                warnings.append(f'enrichment ignored unknown module: {module_name}')
                continue
            if not isinstance(raw_entry, dict):
                continue
            target = payload['modules'].setdefault(module_name, {})
            self._merge_entry(target, raw_entry, allowed_keys={'title', 'description', 'layer', 'keywords', 'requirements'})

        symbol_entries = enrichment.get('symbols') if isinstance(enrichment.get('symbols'), dict) else {}
        for qualname, raw_entry in symbol_entries.items():
            if qualname not in known_symbols:
                warnings.append(f'enrichment ignored unknown symbol: {qualname}')
                continue
            if not isinstance(raw_entry, dict):
                continue
            target = payload['symbols'].setdefault(qualname, {})
            self._merge_entry(target, raw_entry, allowed_keys={'title', 'description', 'keywords', 'requirements'})

        architecture = enrichment.get('architecture') if isinstance(enrichment.get('architecture'), dict) else {}
        if architecture:
            payload['architecture'] = self._merge_architecture(payload.get('architecture', {}), architecture, known_modules, warnings)
        return warnings

    def _merge_entry(self, target: dict[str, Any], source: dict[str, Any], *, allowed_keys: set[str]) -> None:
        for key in allowed_keys:
            if key not in source:
                continue
            value = source.get(key)
            if key in {'keywords', 'requirements'}:
                values = self._string_list(value)
                if values:
                    target[key] = values
                continue
            text = str(value or '').strip()
            if text:
                target[key] = text

    def _merge_architecture(
        self,
        current: dict[str, Any],
        enrichment: dict[str, Any],
        known_modules: set[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        result = dict(current) if isinstance(current, dict) else {}
        for key in ('style',):
            value = str(enrichment.get(key) or '').strip()
            if value:
                result[key] = value
        patterns = self._string_list(enrichment.get('patterns'))
        if patterns:
            result['patterns'] = patterns

        raw_layers = enrichment.get('layers') if isinstance(enrichment.get('layers'), dict) else {}
        if raw_layers:
            layers: dict[str, list[str]] = {}
            for raw_layer, raw_items in raw_layers.items():
                layer = str(raw_layer).strip()
                if not layer:
                    continue
                values: list[str] = []
                for item in raw_items or []:
                    module_name = str(item).strip()
                    if module_name in known_modules:
                        values.append(module_name)
                    elif module_name:
                        warnings.append(f'enrichment ignored unknown module in architecture.layers.{layer}: {module_name}')
                if values:
                    layers[layer] = sorted(set(values))
            if layers:
                result['layers'] = layers

        components = self._dict_list(enrichment.get('components'))
        if components:
            result['components'] = components
        flows = self._dict_list(enrichment.get('flows'))
        if flows:
            result['flows'] = flows
        # Diagnostics from enrichment are returned in the onboarding response.
        # Do not persist unresolved/unmatched references into knowledge.yaml, because
        # knowledge.yaml is used as project knowledge and should not expose stale or
        # non-existent symbols as machine-readable project facts.
        result.pop('unmatched_mentions', None)
        result.pop('unresolved_steps', None)
        return result

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            raw_values = value
        elif isinstance(value, str):
            raw_values = [value]
        else:
            raw_values = []
        result: list[str] = []
        for item in raw_values:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _dict_list(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _change_request_list(self, value: Any) -> list[Any]:
        if not isinstance(value, list):
            return []
        result: list[Any] = []
        seen: set[str] = set()
        for item in value:
            item_id = ''
            if isinstance(item, str):
                item_id = item.strip()
                normalized: Any = item_id
            elif isinstance(item, dict):
                item_id = str(item.get('id') or '').strip()
                normalized = {str(key): val for key, val in item.items()}
            else:
                continue
            if item_id and item_id not in seen:
                seen.add(item_id)
                result.append(normalized)
        return result

    def _copy_traceability_fields(self, entry: dict[str, Any], current: dict[str, Any]) -> None:
        requirements = self._string_list(current.get('requirements'))
        if requirements:
            entry['requirements'] = requirements
        change_requests = self._change_request_list(current.get('change_requests'))
        if change_requests:
            entry['change_requests'] = change_requests

    def _load_existing(self) -> dict[str, Any]:
        if not self.overlays.knowledge_path.exists():
            return {}
        try:
            payload = yaml.safe_load(self.overlays.knowledge_path.read_text(encoding='utf-8')) or {}
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _build_project_section(self, existing: dict[str, Any]) -> dict[str, Any]:
        project = existing.get('project', {}) if isinstance(existing.get('project', {}), dict) else {}
        return {
            'title': str(project.get('title') or self.project_root.name),
            'description': str(project.get('description') or f'Knowledge-слой проекта {self.project_root.name}, автоматически пересобранный во время onboarding.'),
        }

    def _build_modules(self, symbols: list[SymbolRecord], existing: dict[str, Any]) -> dict[str, Any]:
        existing_modules = existing.get('modules', {}) if isinstance(existing.get('modules', {}), dict) else {}
        modules: dict[str, Any] = {}
        for symbol in sorted((item for item in symbols if item.kind == 'module'), key=lambda item: item.qualname):
            current = existing_modules.get(symbol.qualname, {}) if isinstance(existing_modules.get(symbol.qualname, {}), dict) else {}
            entry: dict[str, Any] = {
                'title': str(current.get('title') or self._module_title(symbol)),
                'description': str(current.get('description') or self._normalize_docstring(symbol.docstring) or f'Модуль {symbol.module_name}.'),
                'layer': str(current.get('layer') or self._infer_layer(symbol.module_name) or ''),
            }
            self._copy_traceability_fields(entry, current)
            modules[symbol.qualname] = entry
        return modules

    def _build_symbols(self, symbols: list[SymbolRecord], existing: dict[str, Any]) -> dict[str, Any]:
        existing_symbols = existing.get('symbols', {}) if isinstance(existing.get('symbols', {}), dict) else {}
        items: dict[str, Any] = {}
        for symbol in sorted((item for item in symbols if item.kind != 'module'), key=lambda item: item.qualname):
            current = existing_symbols.get(symbol.qualname, {}) if isinstance(existing_symbols.get(symbol.qualname, {}), dict) else {}
            entry: dict[str, Any] = {
                'title': str(current.get('title') or self._symbol_title(symbol)),
                'description': str(current.get('description') or self._normalize_docstring(symbol.docstring) or self._fallback_symbol_description(symbol)),
            }
            keywords = current.get('keywords')
            if isinstance(keywords, list) and keywords:
                entry['keywords'] = [str(item) for item in keywords]
            else:
                entry['keywords'] = self._default_keywords(symbol)
            self._copy_traceability_fields(entry, current)
            items[symbol.qualname] = entry
        return items

    def _build_architecture(self, symbols: list[SymbolRecord], existing: dict[str, Any]) -> dict[str, Any]:
        existing_arch = existing.get('architecture', {}) if isinstance(existing.get('architecture', {}), dict) else {}
        existing_layers = existing_arch.get('layers', {}) if isinstance(existing_arch.get('layers', {}), dict) else {}
        layers: dict[str, list[str]] = {str(k): [str(i) for i in v] for k, v in existing_layers.items() if isinstance(v, list)}
        for symbol in (item for item in symbols if item.kind == 'module'):
            layer = self._infer_layer(symbol.module_name)
            if not layer:
                continue
            bucket = layers.setdefault(layer, [])
            if symbol.qualname not in bucket:
                bucket.append(symbol.qualname)
        normalized = {layer: sorted(set(items)) for layer, items in layers.items()}
        return {'layers': normalized}

    def _module_title(self, symbol: SymbolRecord) -> str:
        return self._humanize_name(symbol.name or symbol.module_name.split('.')[-1])

    def _symbol_title(self, symbol: SymbolRecord) -> str:
        if symbol.kind == 'class':
            return self._humanize_name(symbol.name)
        return self._humanize_name(symbol.name).capitalize()

    def _fallback_symbol_description(self, symbol: SymbolRecord) -> str:
        label = {
            'function': 'Функция',
            'method': 'Метод',
            'class': 'Класс',
        }.get(symbol.kind, 'Символ')
        return f'{label} {symbol.qualname}.'

    def _default_keywords(self, symbol: SymbolRecord) -> list[str]:
        values = [self._humanize_name(symbol.name)]
        if symbol.kind == 'method' and symbol.parent_qualname:
            values.append(self._humanize_name(symbol.parent_qualname.split('.')[-1]))
        seen: list[str] = []
        for item in values:
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
        return seen

    def _infer_layer(self, module_name: str) -> str | None:
        parts = module_name.split('.')
        for candidate in ('api', 'services', 'storage', 'domain'):
            if candidate in parts:
                return candidate
        return None

    def _normalize_docstring(self, value: str) -> str:
        return ' '.join((value or '').strip().split())

    def _humanize_name(self, value: str) -> str:
        return value.replace('_', ' ').replace('-', ' ').strip()
