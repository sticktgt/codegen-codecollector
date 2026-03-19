from __future__ import annotations

import re
from pathlib import Path

from codecollector.config import AppConfig, load_config
from codecollector.domain.models import SearchCandidate, SymbolRecord
from codecollector.indexing.base import IndexStore
from codecollector.overlays.service import OverlayService
from codecollector.logger import get_logger
from codecollector.vector_search.service import DescriptionVectorSearchService

LOGGER = get_logger(__name__)
TERM_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ_]{2,}")


class SearchService:
    def __init__(
        self,
        project_root: Path,
        store: IndexStore,
        overlays: OverlayService,
        vector_search: DescriptionVectorSearchService | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.project_key = str(self.project_root)
        self.store = store
        self.overlays = overlays
        self.vector_search = vector_search
        self.config = config or load_config()

    def search(self, query: str, limit: int = 5, use_vector_search: bool | None = None) -> list[SearchCandidate]:
        LOGGER.info("Executing search query=%r limit=%s for %s", query, limit, self.project_root)
        normalized_query = self._normalize_text(query)
        query_terms = self._expand_terms(normalized_query)
        vector_enabled = self.config.search_vector_enabled if use_vector_search is None else use_vector_search
        vector_scores = self.vector_search.search(query, limit=max(limit * 3, 10)) if vector_enabled and self.vector_search else {}

        symbols = [item for item in self.store.list_symbols(self.project_key) if item.kind != 'module']
        candidates: list[SearchCandidate] = []
        for symbol in symbols:
            candidate = self._score_symbol(symbol, query_terms, query, vector_scores.get(symbol.qualname, 0.0))
            if candidate.score > 0:
                candidates.append(candidate)

        candidates.sort(key=lambda item: (-item.score, -item.confidence, item.file_path, item.qualname))
        return candidates[:limit]

    def _expand_terms(self, normalized_query: str) -> list[str]:
        terms = set(TERM_RE.findall(normalized_query))
        stop_words = {
            'и', 'в', 'во', 'на', 'по', 'для', 'к', 'ко', 'с', 'со', 'о', 'об', 'от', 'до', 'из', 'за', 'при', 'или',
            'изменить', 'изменения', 'изменение', 'добавить', 'сделать', 'обновить', 'заменить', 'создать',
            'поменять', 'нужно', 'нужен', 'нужна', 'должен', 'должна', 'надо', 'русский', 'русском', 'русскоязычным', 'язык'
        }
        term_list = sorted(term for term in terms if term and term not in stop_words)
        stems = {self._rough_stem(term) for term in term_list if len(term) >= 4}
        return sorted(set(term_list) | {item for item in stems if item})

    def _score_symbol(self, symbol: SymbolRecord, query_terms: list[str], raw_query: str, vector_score: float) -> SearchCandidate:
        bundle = self.overlays.candidate_text_bundle(symbol.module_name, symbol.qualname)
        knowledge_title = str(bundle.get('symbol_title', ''))
        field_bag = {
            'name': symbol.name,
            'qualname': symbol.qualname,
            'file_path': symbol.file_path,
            'docstring': symbol.docstring,
            'module_title': str(bundle.get('module_title', '')),
            'module_description': str(bundle.get('module_description', '')),
            'symbol_title': knowledge_title,
            'symbol_description': str(bundle.get('symbol_description', '')),
            'keywords': ' '.join(bundle.get('keywords', []) or []),
            'requirement_text': ' '.join(
                f"{item.get('id', '')} {item.get('title', '')} {item.get('description', '')}" for item in bundle.get('requirements', [])
            ),
            'layer': str(bundle.get('layer', '')),
        }
        weights = {
            'name': 3.4,
            'qualname': 2.0,
            'file_path': 1.0,
            'docstring': 2.8,
            'module_title': 1.4,
            'module_description': 1.8,
            'symbol_title': 3.0,
            'symbol_description': 3.2,
            'keywords': 2.6,
            'requirement_text': 2.4,
            'layer': 0.8,
        }
        score = 0.0
        reasons: list[str] = []
        matched_terms: set[str] = set()
        normalized_fields = {key: self._normalize_text(value) for key, value in field_bag.items() if value}
        field_tokens = {key: set(TERM_RE.findall(value)) for key, value in normalized_fields.items()}

        for term in query_terms:
            best_match: tuple[float, str, str] | None = None
            for field_name, field_value in normalized_fields.items():
                match_type = self._match_term(term, field_value, field_tokens.get(field_name, set()))
                if match_type is None:
                    continue
                weight = weights[field_name]
                multiplier = 1.0 if match_type == 'exact_token' else 0.75 if match_type == 'substring' else 0.55
                points = weight * multiplier
                if best_match is None or points > best_match[0]:
                    best_match = (points, field_name, match_type)
            if best_match is None:
                continue
            points, field_name, match_type = best_match
            score += points
            matched_terms.add(term)
            reasons.append(self._reason_for_field(term, field_name, match_type))

        if vector_score > 0:
            score += vector_score * self.config.search_vector_weight
            reasons.insert(0, f'векторное сходство по human-readable описаниям: {vector_score:.2f}')

        if symbol.kind in {'function', 'method'}:
            score += 0.8
            reasons.append('это исполняемая точка логики: функция или метод')
        if bundle.get('layer') == 'services':
            score += 0.75
            reasons.append('символ находится в service-слое, который обычно является основной точкой бизнес-логики')
        elif bundle.get('layer') == 'storage':
            score -= 0.35
            reasons.append('domain/storage-слой чаще хранит данные или инфраструктуру, а не основной use-case')
        if knowledge_title or field_bag['symbol_description']:
            score += 0.55
            reasons.append('для символа есть knowledge-описание в overlay')
        requirements = [item['id'] for item in bundle.get('requirements', [])]
        if requirements:
            score += 0.65
            reasons.append('символ явно связан с requirement-артефактами')
            requirement_rank = bundle.get('requirement_rank')
            if requirement_rank == 0:
                score += 0.45
                reasons.append('symbol указан как первичная точка реализации требования')
        if len(matched_terms) >= 2:
            score += min(len(matched_terms), 5) * 0.2
        score += self._change_request_intent_adjustment(raw_query, symbol, bundle)
        confidence = min(1.0, round(0.12 + score / 22.0, 2)) if score > 0 else 0.0
        relevance_category = self._relevance_category(score, confidence)
        return SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=round(score, 2),
            confidence=confidence,
            relevance_category=relevance_category,
            reasons=self._dedupe_reasons(reasons)[:8],
            docstring=symbol.docstring,
            knowledge_title=knowledge_title,
            requirements=requirements,
        )

    def _change_request_intent_adjustment(self, query: str, symbol: SymbolRecord, bundle: dict) -> float:
        normalized_query = self._normalize_text(query)
        adjustment = 0.0
        symbol_text = ' '.join(
            [symbol.name, symbol.docstring, str(bundle.get('symbol_title', '')), str(bundle.get('symbol_description', ''))]
        )
        normalized_symbol_text = self._normalize_text(symbol_text)
        if any(term in normalized_query for term in ('текст', 'уведомл', 'формулиров', 'сообщен')):
            if any(term in normalized_symbol_text for term in ('message', 'уведомл', 'текст', 'сообщен', 'формулиров')):
                adjustment += 2.0
            if bundle.get('layer') == 'services' and symbol.name.startswith('build_'):
                adjustment += 1.4
            if symbol.name.startswith('assign_'):
                adjustment -= 1.6
            if '.notification_service.' in symbol.qualname:
                adjustment += 1.2
        return adjustment

    def _reason_for_field(self, term: str, field_name: str, match_type: str) -> str:
        field_labels = {
            'name': 'имени символа',
            'qualname': 'полном имени символа',
            'file_path': 'пути к файлу',
            'docstring': 'docstring в коде',
            'module_title': 'названии модуля из knowledge',
            'module_description': 'описании модуля из knowledge',
            'symbol_title': 'названии символа из knowledge',
            'symbol_description': 'описании символа из knowledge',
            'keywords': 'keywords из knowledge',
            'requirement_text': 'связанных требованиях',
            'layer': 'архитектурном слое',
        }
        match_labels = {
            'exact_token': 'точное совпадение',
            'substring': 'совпадение',
            'stem': 'совпадение по основе слова',
        }
        return f'термин «{term}» дал {match_labels[match_type]} в {field_labels[field_name]}'

    def _match_term(self, term: str, field_value: str, field_tokens: set[str]) -> str | None:
        if term in field_tokens:
            return 'exact_token'
        if term in field_value:
            return 'substring'
        stem = self._rough_stem(term)
        if stem and stem in field_value:
            return 'stem'
        return None

    def _rough_stem(self, term: str) -> str:
        normalized = term.casefold().replace('ё', 'е')
        for suffix in ('иями', 'ями', 'ами', 'ями', 'ение', 'ений', 'ению', 'ения', 'ениям', 'овать', 'овать', 'ание', 'ании', 'ания', 'ения', 'ить', 'ать', 'ять', 'ться', 'ться', 'ости', 'ость', 'ого', 'ому', 'ыми', 'ими', 'ой', 'ий', 'ый', 'ая', 'ое', 'ые', 'ых', 'ую', 'ам', 'ом', 'ем', 'ов', 'ев', 'ие', 'ия', 'ию', 'ей', 'ям', 'ах', 'ях', 'ам', 'ям', 'ет', 'ют', 'ут', 'ан', 'ен', 'ти', 'ть', 'ия', 'ие', 'ий', 'иям', 'иях', 'а', 'я', 'ы', 'и', 'у', 'ю', 'о', 'е'):
            if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 4:
                return normalized[: -len(suffix)]
        return normalized

    def _normalize_text(self, text: str) -> str:
        return text.casefold().replace('ё', 'е')

    def _relevance_category(self, score: float, confidence: float) -> str:
        if confidence >= 0.7 and score >= 9.0:
            return 'высокая'
        if confidence >= 0.4 and score >= 4.0:
            return 'средняя'
        return 'низкая'

    def _dedupe_reasons(self, reasons: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                result.append(reason)
        return result
