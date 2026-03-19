from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from codecollector.domain.models import RelationRecord
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class OverlayService:
    def __init__(self, project_root: Path, overlay_dirname: str = '.codecollector') -> None:
        self.project_root = project_root.resolve()
        self.overlay_dir = self.project_root / overlay_dirname
        self.overlay_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_path = self.overlay_dir / 'knowledge.yaml'
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        if not self.knowledge_path.exists():
            default_knowledge = {
                'version': 1,
                'project': {
                    'title': self.project_root.name,
                    'description': 'Knowledge-слой проекта для human-readable поиска и связей с требованиями.',
                },
                'modules': {},
                'symbols': {},
                'requirements': {},
                'architecture': {'layers': {}},
            }
            self.knowledge_path.write_text(yaml.safe_dump(default_knowledge, allow_unicode=True, sort_keys=False), encoding='utf-8')

    @lru_cache(maxsize=1)
    def load_knowledge(self) -> dict[str, Any]:
        payload = yaml.safe_load(self.knowledge_path.read_text(encoding='utf-8')) or {}
        return payload if isinstance(payload, dict) else {}

    def refresh(self) -> None:
        self.load_knowledge.cache_clear()

    def project_title(self) -> str:
        return str(self.load_knowledge().get('project', {}).get('title', self.project_root.name))

    def project_description(self) -> str:
        return str(self.load_knowledge().get('project', {}).get('description', ''))

    def module_entry(self, module_name: str) -> dict[str, Any]:
        entry = self.load_knowledge().get('modules', {}).get(module_name, {})
        return entry if isinstance(entry, dict) else {}

    def symbol_entry(self, qualname: str) -> dict[str, Any]:
        entry = self.load_knowledge().get('symbols', {}).get(qualname, {})
        return entry if isinstance(entry, dict) else {}

    def requirement_entry(self, requirement_id: str) -> dict[str, Any]:
        entry = self.load_knowledge().get('requirements', {}).get(requirement_id, {})
        return entry if isinstance(entry, dict) else {}

    def architecture_layers(self) -> dict[str, list[str]]:
        layers = self.load_knowledge().get('architecture', {}).get('layers', {})
        if not isinstance(layers, dict):
            return {}
        return {str(key): [str(item) for item in (value or [])] for key, value in layers.items()}

    def symbol_title(self, qualname: str) -> str:
        return str(self.symbol_entry(qualname).get('title', ''))

    def symbol_description(self, qualname: str) -> str:
        return str(self.symbol_entry(qualname).get('description', ''))

    def symbol_keywords(self, qualname: str) -> list[str]:
        entry = self.symbol_entry(qualname)
        keywords = entry.get('keywords', [])
        return [str(item) for item in keywords] if isinstance(keywords, list) else []

    def requirements_for_symbol(self, qualname: str) -> list[str]:
        entry = self.symbol_entry(qualname)
        result: list[str] = []
        direct = entry.get('requirements', [])
        if isinstance(direct, list):
            result.extend(str(item) for item in direct)
        for requirement_id, requirement in self.load_knowledge().get('requirements', {}).items():
            linked = requirement.get('linked_symbols', [])
            if isinstance(linked, list) and qualname in linked and requirement_id not in result:
                result.append(str(requirement_id))
        return result

    def requirement_title(self, requirement_id: str) -> str:
        return str(self.requirement_entry(requirement_id).get('title', requirement_id))

    def requirement_description(self, requirement_id: str) -> str:
        return str(self.requirement_entry(requirement_id).get('description', ''))

    def requirement_rank_for_symbol(self, qualname: str) -> int | None:
        best_rank: int | None = None
        for requirement_id in self.requirements_for_symbol(qualname):
            linked = self.requirement_entry(requirement_id).get('linked_symbols', [])
            if isinstance(linked, list) and qualname in linked:
                rank = linked.index(qualname)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
        return best_rank

    def requirement_details_for_symbol(self, qualname: str) -> list[dict[str, str]]:
        details: list[dict[str, str]] = []
        for requirement_id in self.requirements_for_symbol(qualname):
            details.append({
                'id': requirement_id,
                'title': self.requirement_title(requirement_id),
                'description': self.requirement_description(requirement_id),
            })
        return details

    def module_layer(self, module_name: str) -> str | None:
        module_entry = self.module_entry(module_name)
        if module_entry.get('layer'):
            return str(module_entry['layer'])
        for layer_name, modules in self.architecture_layers().items():
            if module_name in modules:
                return str(layer_name)
        return None

    def annotations_for(self, qualname: str) -> list[str]:
        notes: list[str] = []
        title = self.symbol_title(qualname)
        description = self.symbol_description(qualname)
        if title:
            notes.append(f'Knowledge title: {title}')
        if description:
            notes.append(description)
        for requirement in self.requirement_details_for_symbol(qualname):
            notes.append(f"Связано с требованием {requirement['id']}: {requirement['title']}")
        return notes

    def candidate_text_bundle(self, module_name: str, qualname: str) -> dict[str, Any]:
        module_entry = self.module_entry(module_name)
        symbol_entry = self.symbol_entry(qualname)
        requirements = self.requirement_details_for_symbol(qualname)
        return {
            'module_title': str(module_entry.get('title', '')),
            'module_description': str(module_entry.get('description', '')),
            'symbol_title': str(symbol_entry.get('title', '')),
            'symbol_description': str(symbol_entry.get('description', '')),
            'keywords': self.symbol_keywords(qualname),
            'requirements': requirements,
            'requirement_rank': self.requirement_rank_for_symbol(qualname),
            'layer': self.module_layer(module_name),
        }

    def knowledge_relations(self) -> list[RelationRecord]:
        relations: list[RelationRecord] = []
        seen: set[tuple[str, str, str, str | None, str]] = set()

        def append_unique(relation: RelationRecord) -> None:
            key = (
                relation.source_qualname,
                relation.relation_kind,
                relation.target_ref,
                relation.target_qualname,
                relation.relation_source,
            )
            if key not in seen:
                seen.add(key)
                relations.append(relation)

        for module_name, module_entry in self.load_knowledge().get('modules', {}).items():
            layer = str(module_entry.get('layer', '')).strip()
            if layer:
                append_unique(RelationRecord(
                    source_qualname=str(module_name),
                    relation_kind='belongs_to_layer',
                    target_ref=layer,
                    target_qualname=None,
                    file_path='',
                    relation_source='knowledge',
                    relation_confidence='high',
                ))
        for requirement_id, requirement_entry in self.load_knowledge().get('requirements', {}).items():
            linked_symbols = requirement_entry.get('linked_symbols', [])
            if not isinstance(linked_symbols, list):
                continue
            for qualname in linked_symbols:
                append_unique(RelationRecord(
                    source_qualname=str(qualname),
                    relation_kind='implements_requirement',
                    target_ref=str(requirement_id),
                    target_qualname=None,
                    file_path='',
                    relation_source='knowledge',
                    relation_confidence='high',
                ))
        for qualname, symbol_entry in self.load_knowledge().get('symbols', {}).items():
            if not isinstance(symbol_entry, dict):
                continue
            for requirement_id in symbol_entry.get('requirements', []) or []:
                append_unique(RelationRecord(
                    source_qualname=str(qualname),
                    relation_kind='implements_requirement',
                    target_ref=str(requirement_id),
                    target_qualname=None,
                    file_path='',
                    relation_source='knowledge',
                    relation_confidence='high',
                ))
        return relations
