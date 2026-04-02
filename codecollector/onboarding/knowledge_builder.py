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

    def rebuild(self, symbols: list[SymbolRecord]) -> dict[str, Any]:
        existing = self._load_existing()
        payload = {
            'version': 1,
            'project': self._build_project_section(existing),
            'modules': self._build_modules(symbols, existing),
            'symbols': self._build_symbols(symbols, existing),
            'requirements': existing.get('requirements', {}) if isinstance(existing.get('requirements', {}), dict) else {},
            'architecture': self._build_architecture(symbols, existing),
        }
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
        }

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
            modules[symbol.qualname] = {
                'title': str(current.get('title') or self._module_title(symbol)),
                'description': str(current.get('description') or self._normalize_docstring(symbol.docstring) or f'Модуль {symbol.module_name}.'),
                'layer': str(current.get('layer') or self._infer_layer(symbol.module_name) or ''),
            }
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
            requirements = current.get('requirements')
            if isinstance(requirements, list) and requirements:
                entry['requirements'] = [str(item) for item in requirements]
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
