from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.logger import get_logger
from codecollector.overlays.service import OverlayService

LOGGER = get_logger(__name__)


class KnowledgeTraceabilityService:
    """Обновляет связи symbols/modules с требованиями и примененными CR.

    Сервис не меняет тело кода и не использует LLM. Он только детерминированно
    добавляет идентификаторы требований и CR в knowledge.yaml после фактического
    применения workspace.
    """

    def __init__(self, project_root: Path, config: AppConfig) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.overlays = OverlayService(self.project_root, overlay_dirname=self.config.overlay_dirname)

    def update_for_applied_workspace(
        self,
        *,
        symbol_qualnames: list[str],
        module_names: list[str] | None = None,
        change_request_id: str | None = None,
        requirement_ids: list[str] | None = None,
        applied_at: str | None = None,
    ) -> dict[str, Any]:
        cr_id = str(change_request_id or '').strip()
        req_ids = self._normalize_string_list(requirement_ids or [])
        symbols = self._normalize_string_list(symbol_qualnames)
        modules = self._normalize_string_list(module_names or [])

        if not cr_id and not req_ids:
            return {
                'updated': False,
                'skipped_reason': 'missing_traceability_ids',
                'symbols': [],
                'modules': [],
                'requirements': [],
                'change_request_id': '',
                'knowledge_path': str(self.overlays.knowledge_path),
            }
        if not symbols and not modules:
            return {
                'updated': False,
                'skipped_reason': 'missing_changed_targets',
                'symbols': [],
                'modules': [],
                'requirements': req_ids,
                'change_request_id': cr_id,
                'knowledge_path': str(self.overlays.knowledge_path),
            }

        payload = self._load_knowledge()
        payload.setdefault('version', 1)
        payload.setdefault('modules', {})
        payload.setdefault('symbols', {})
        if not isinstance(payload.get('modules'), dict):
            payload['modules'] = {}
        if not isinstance(payload.get('symbols'), dict):
            payload['symbols'] = {}

        changed = False
        updated_symbols: list[str] = []
        updated_modules: list[str] = []

        for qualname in symbols:
            target = payload['symbols'].setdefault(qualname, {})
            if not isinstance(target, dict):
                target = {}
                payload['symbols'][qualname] = target
            if self._merge_entry(target, requirement_ids=req_ids, change_request_id=cr_id, applied_at=applied_at):
                changed = True
            updated_symbols.append(qualname)

        for module_name in modules:
            target = payload['modules'].setdefault(module_name, {})
            if not isinstance(target, dict):
                target = {}
                payload['modules'][module_name] = target
            if self._merge_entry(target, requirement_ids=req_ids, change_request_id=cr_id, applied_at=applied_at):
                changed = True
            updated_modules.append(module_name)

        if changed:
            self.overlays.knowledge_path.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                encoding='utf-8',
            )
            self.overlays.refresh()
            LOGGER.info(
                'Knowledge traceability updated: project=%s symbols=%s modules=%s cr_id=%s requirements=%s',
                self.project_root,
                updated_symbols,
                updated_modules,
                cr_id,
                req_ids,
            )

        return {
            'updated': changed,
            'skipped_reason': '' if changed else 'already_up_to_date',
            'symbols': updated_symbols,
            'modules': updated_modules,
            'requirements': req_ids,
            'change_request_id': cr_id,
            'applied_at': str(applied_at or ''),
            'knowledge_path': str(self.overlays.knowledge_path),
        }

    def _load_knowledge(self) -> dict[str, Any]:
        if not self.overlays.knowledge_path.exists():
            return {'version': 1, 'modules': {}, 'symbols': {}, 'requirements': {}, 'architecture': {'layers': {}}}
        try:
            payload = yaml.safe_load(self.overlays.knowledge_path.read_text(encoding='utf-8')) or {}
        except Exception as exc:  # noqa: BLE001 - caller should get a structured skip/error payload from workspace apply.
            raise ValueError(f'Не удалось прочитать knowledge.yaml: {self.overlays.knowledge_path}') from exc
        return payload if isinstance(payload, dict) else {'version': 1, 'modules': {}, 'symbols': {}}

    def _merge_entry(
        self,
        entry: dict[str, Any],
        *,
        requirement_ids: list[str],
        change_request_id: str,
        applied_at: str | None,
    ) -> bool:
        changed = False
        if requirement_ids:
            merged_requirements = self._merge_string_list(entry.get('requirements'), requirement_ids)
            if merged_requirements != entry.get('requirements'):
                entry['requirements'] = merged_requirements
                changed = True
        if change_request_id:
            merged_crs, cr_changed = self._merge_change_requests(entry.get('change_requests'), change_request_id, applied_at)
            if cr_changed:
                entry['change_requests'] = merged_crs
                changed = True
        return changed

    def _merge_change_requests(self, existing: Any, change_request_id: str, applied_at: str | None) -> tuple[list[Any], bool]:
        items = existing if isinstance(existing, list) else []
        result: list[Any] = []
        seen: set[str] = set()
        changed = not isinstance(existing, list) and bool(existing)

        for item in items:
            item_id = self._change_request_item_id(item)
            if not item_id:
                changed = True
                continue
            if item_id in seen:
                changed = True
                continue
            seen.add(item_id)
            result.append(item)

        if change_request_id not in seen:
            entry: dict[str, str] = {'id': change_request_id}
            timestamp = str(applied_at or '').strip()
            if timestamp:
                entry['applied_at'] = timestamp
            result.append(entry)
            changed = True
        return result, changed

    def _merge_string_list(self, existing: Any, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in existing if isinstance(existing, list) else []:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        for item in values:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _normalize_string_list(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for item in values:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    def _change_request_item_id(self, item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            return str(item.get('id') or '').strip()
        return ''
