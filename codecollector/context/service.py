from __future__ import annotations

from collections import deque
from pathlib import Path

from codecollector.domain.models import ContextPack, RelatedSymbolContext, RelationRecord, SymbolRecord
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
        child_relation_sources = self._child_relation_sources(target, file_symbols)
        related_symbols = self._collect_related_symbols(target, inbound, outbound, child_relation_sources)
        file_based_tests = self._collect_file_based_tests(target)
        for item in file_based_tests:
            if all(existing.qualname != item.qualname for existing in related_tests):
                related_tests.append(item)
        requirements = self.overlays.requirement_details_for_symbol(qualname)
        recommended_tests = [item.qualname for item in related_tests]
        relation_confidence_summary = self._relation_confidence_summary(inbound, outbound)
        return ContextPack(
            target=target,
            neighbors=neighbors[:8],
            inbound_relations=inbound[:16],
            outbound_relations=outbound[:16],
            related_tests=related_tests[:8],
            related_symbols=related_symbols[:12],
            requirement_ids=[item['id'] for item in requirements],
            requirement_titles=[item['title'] for item in requirements],
            knowledge_title=self.overlays.symbol_title(qualname),
            knowledge_description=self.overlays.symbol_description(qualname),
            recommended_tests=recommended_tests[:8],
            relation_confidence_summary=relation_confidence_summary,
        )


    def _collect_related_symbols(
        self,
        target: SymbolRecord,
        inbound_relations: list[RelationRecord],
        outbound_relations: list[RelationRecord],
        child_relation_sources: list[tuple[SymbolRecord, list[RelationRecord], list[RelationRecord]]] | None = None,
    ) -> list[RelatedSymbolContext]:
        related: list[RelatedSymbolContext] = []
        seen: set[str] = {target.qualname}

        def add_symbol(
            symbol: SymbolRecord | None,
            relation: RelationRecord,
            direction: str,
            origin_qualname: str,
        ) -> None:
            if symbol is None:
                return
            if symbol.qualname in seen:
                return
            if symbol.qualname == target.qualname:
                return
            if self._is_test_symbol(symbol):
                return
            seen.add(symbol.qualname)
            related.append(
                RelatedSymbolContext(
                    qualname=symbol.qualname,
                    file_path=symbol.file_path,
                    module_name=symbol.module_name,
                    name=symbol.name,
                    kind=symbol.kind,
                    parent_qualname=symbol.parent_qualname,
                    relation_kind=relation.relation_kind,
                    relation_direction=direction,
                    relation_source=relation.relation_source,
                    relation_confidence=relation.relation_confidence,
                    role=self._related_symbol_role(relation, direction, origin_qualname, target.qualname),
                    origin_qualname=origin_qualname,
                    signature=self._signature_for_symbol(symbol),
                    docstring=symbol.docstring,
                    source_code=symbol.source_code,
                )
            )

        for relation in outbound_relations:
            if relation.relation_kind in {'contains', 'covered_by_test', 'implements_requirement'}:
                continue
            symbol = None
            if relation.target_qualname:
                symbol = self.store.get_symbol(self.project_key, relation.target_qualname)
            if symbol is None and relation.target_ref:
                matches = self.store.list_symbols_by_short_name(self.project_key, relation.target_ref)
                if len(matches) == 1:
                    symbol = matches[0]
            add_symbol(symbol, relation, 'outbound', target.qualname)

        for relation in inbound_relations:
            if relation.relation_kind in {'contains', 'covered_by_test', 'implements_requirement'}:
                continue
            symbol = self.store.get_symbol(self.project_key, relation.source_qualname)
            add_symbol(symbol, relation, 'inbound', target.qualname)

        for child_symbol, child_inbound, child_outbound in child_relation_sources or []:
            for relation in child_outbound:
                if relation.relation_kind in {'contains', 'covered_by_test', 'implements_requirement'}:
                    continue
                symbol = None
                if relation.target_qualname:
                    symbol = self.store.get_symbol(self.project_key, relation.target_qualname)
                if symbol is None and relation.target_ref:
                    matches = self.store.list_symbols_by_short_name(self.project_key, relation.target_ref)
                    if len(matches) == 1:
                        symbol = matches[0]
                add_symbol(symbol, relation, 'outbound', child_symbol.qualname)

            for relation in child_inbound:
                if relation.relation_kind in {'contains', 'covered_by_test', 'implements_requirement'}:
                    continue
                symbol = self.store.get_symbol(self.project_key, relation.source_qualname)
                add_symbol(symbol, relation, 'inbound', child_symbol.qualname)

        confidence_rank = {'high': 0, 'medium': 1, 'low': 2}
        direction_rank = {'outbound': 0, 'inbound': 1}
        relation_rank = {
            'calls': 0,
            'exposed_by_controller': 1,
            'imports': 2,
            'belongs_to_layer': 3,
        }
        related.sort(
            key=lambda item: (
                confidence_rank.get(str(item.relation_confidence), 9),
                direction_rank.get(item.relation_direction, 9),
                relation_rank.get(str(item.relation_kind), 9),
                item.file_path,
                item.origin_qualname,
                item.qualname,
            )
        )
        LOGGER.info(
            'Collected related production symbols for %s: count=%s child_sources=%s qualnames=%s',
            target.qualname,
            len(related),
            len(child_relation_sources or []),
            [item.qualname for item in related[:12]],
        )
        return related

    def _child_relation_sources(
        self,
        target: SymbolRecord,
        file_symbols: list[SymbolRecord],
    ) -> list[tuple[SymbolRecord, list[RelationRecord], list[RelationRecord]]]:
        if target.kind != 'class':
            return []

        child_symbols = [
            symbol for symbol in file_symbols
            if symbol.parent_qualname == target.qualname and symbol.kind == 'method'
        ]
        result: list[tuple[SymbolRecord, list[RelationRecord], list[RelationRecord]]] = []
        for child in child_symbols:
            child_outbound = [
                relation for relation in self.store.list_outbound_relations(self.project_key, child.qualname)
                if relation.relation_kind != 'contains'
            ]
            child_inbound = [
                relation for relation in self.store.list_inbound_relations_for_qualname(self.project_key, child.qualname)
                if relation.relation_kind != 'contains'
            ]
            if child_inbound or child_outbound:
                result.append((child, child_inbound, child_outbound))

        if result:
            LOGGER.info(
                'Collected child relation sources for class target %s: count=%s qualnames=%s',
                target.qualname,
                len(result),
                [item[0].qualname for item in result[:8]],
            )
        return result

    def _related_symbol_role(self, relation: RelationRecord, direction: str, origin_qualname: str, target_qualname: str) -> str:
        from_child = origin_qualname != target_qualname
        if relation.relation_kind == 'calls' and direction == 'outbound':
            return 'called_by_class_member' if from_child else 'called_by_target'
        if relation.relation_kind == 'calls' and direction == 'inbound':
            return 'caller_of_class_member' if from_child else 'caller_of_target'
        if relation.relation_kind == 'imports':
            return 'imported_contract'
        if relation.relation_kind == 'exposed_by_controller':
            return 'controller_or_endpoint_contract'
        if relation.relation_kind == 'belongs_to_layer':
            return 'same_layer_contract'
        return f'{direction}_{relation.relation_kind}'

    def _signature_for_symbol(self, symbol: SymbolRecord) -> str:
        source = (symbol.source_code or '').strip()
        if not source:
            return symbol.name
        lines = source.splitlines()
        useful_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('@'):
                continue
            useful_lines.append(stripped)
            if stripped.endswith(':'):
                break
            if len(useful_lines) >= 4:
                break
        return ' '.join(useful_lines).strip() or symbol.name

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

    def _collect_file_based_tests(self, target: SymbolRecord) -> list[SymbolRecord]:
        test_symbols: list[SymbolRecord] = []
        seen: set[str] = set()

        module_stem = Path(target.file_path).stem
        expected_test_stems = {f"test_{module_stem}"}
        target_name = target.name.casefold()

        for symbol in self.store.list_symbols(self.project_key):
            if not self._is_test_symbol(symbol):
                continue

            symbol_stem = Path(symbol.file_path).stem
            qualname_cf = symbol.qualname.casefold()
            name_cf = symbol.name.casefold()
            if (
                symbol_stem in expected_test_stems
                or target_name in name_cf
                or target_name in qualname_cf
            ):
                if symbol.qualname not in seen:
                    seen.add(symbol.qualname)
                    test_symbols.append(symbol)

        test_symbols.sort(key=lambda item: (item.file_path, item.qualname))
        return test_symbols

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
