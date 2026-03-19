from __future__ import annotations

from collections import deque
from pathlib import Path

from codecollector.domain.models import ContextPack, SymbolRecord
from codecollector.indexing.base import IndexStore
from codecollector.overlays.service import OverlayService
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class ContextService:
    def __init__(self, project_root: Path, store: IndexStore, overlays: OverlayService) -> None:
        self.project_root = project_root.resolve()
        self.project_key = str(self.project_root)
        self.store = store
        self.overlays = overlays

    def build_context(self, qualname: str) -> ContextPack:
        LOGGER.info('Building context pack for %s', qualname)
        target = self.store.get_symbol(self.project_key, qualname)
        if target is None:
            raise ValueError(f'Unknown qualname: {qualname}')

        file_symbols = self.store.list_symbols_in_file(self.project_key, target.file_path)
        neighbors = [symbol for symbol in file_symbols if symbol.qualname != qualname]
        outbound = [relation for relation in self.store.list_outbound_relations(self.project_key, qualname) if relation.relation_kind != 'contains']
        inbound = [relation for relation in self.store.list_inbound_relations_for_qualname(self.project_key, qualname) if relation.relation_kind != 'contains']
        if not inbound:
            inbound = [relation for relation in self.store.list_inbound_relations_for_name(self.project_key, target.name) if relation.relation_kind != 'contains']
        related_tests = self._collect_related_tests(qualname, inbound)
        requirements = self.overlays.requirement_details_for_symbol(qualname)
        recommended_tests = [item.qualname for item in related_tests]
        relation_confidence_summary = self._relation_confidence_summary(inbound, outbound)
        return ContextPack(
            target=target,
            neighbors=neighbors[:8],
            inbound_relations=inbound[:16],
            outbound_relations=outbound[:16],
            related_tests=related_tests[:8],
            requirement_ids=[item['id'] for item in requirements],
            requirement_titles=[item['title'] for item in requirements],
            knowledge_title=self.overlays.symbol_title(qualname),
            knowledge_description=self.overlays.symbol_description(qualname),
            recommended_tests=recommended_tests[:8],
            relation_confidence_summary=relation_confidence_summary,
        )

    def _collect_related_tests(self, target_qualname: str, inbound_relations) -> list[SymbolRecord]:
        seen: set[str] = set()
        related_tests: list[SymbolRecord] = []
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(target_qualname, 0)])

        while queue:
            current_qualname, depth = queue.popleft()
            if current_qualname in visited or depth > 2:
                continue
            visited.add(current_qualname)

            for relation in self.store.list_inbound_relations_for_qualname(self.project_key, current_qualname):
                if relation.relation_kind == 'contains':
                    continue
                symbol = self.store.get_symbol(self.project_key, relation.source_qualname)
                if symbol is None:
                    continue
                if self._is_test_symbol(symbol) and symbol.qualname not in seen:
                    seen.add(symbol.qualname)
                    related_tests.append(symbol)
                elif relation.relation_kind in {'calls', 'covered_by_test', 'exposed_by_controller'} and depth < 2:
                    queue.append((symbol.qualname, depth + 1))

        for relation in inbound_relations:
            symbol = self.store.get_symbol(self.project_key, relation.source_qualname)
            if symbol and self._is_test_symbol(symbol) and symbol.qualname not in seen:
                seen.add(symbol.qualname)
                related_tests.append(symbol)

        related_tests.sort(key=lambda item: (item.file_path, item.qualname))
        return related_tests

    def _is_test_symbol(self, symbol: SymbolRecord) -> bool:
        return '/tests/' in f'/{symbol.file_path}' or symbol.file_path.startswith('tests/')

    def _relation_confidence_summary(self, inbound_relations, outbound_relations) -> dict[str, dict[str, int]]:
        def summarize(items):
            summary = {'high': 0, 'medium': 0, 'low': 0}
            for relation in items:
                summary[str(relation.relation_confidence)] = summary.get(str(relation.relation_confidence), 0) + 1
            return summary

        return {
            'inbound': summarize(inbound_relations),
            'outbound': summarize(outbound_relations),
        }
