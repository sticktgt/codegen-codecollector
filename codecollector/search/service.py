from __future__ import annotations

import re
from pathlib import Path

from codecollector.domain.models import SearchCandidate, SymbolRecord
from codecollector.indexing.storage_sqlite import SQLiteIndexStore
from codecollector.overlays.service import OverlayService
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)
TERM_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ_]{2,}")


class SearchService:
    def __init__(self, project_root: Path, store: SQLiteIndexStore, overlays: OverlayService) -> None:
        self.project_root = project_root.resolve()
        self.project_key = str(self.project_root)
        self.store = store
        self.overlays = overlays

    def search(self, query: str, limit: int = 5) -> list[SearchCandidate]:
        LOGGER.info("Executing search query=%r limit=%s for %s", query, limit, self.project_root)
        normalized_query = self._normalize_text(query)
        query_terms = self._expand_terms(normalized_query)
        symbols = [item for item in self.store.list_symbols(self.project_key) if item.kind != 'module']
        candidates: list[SearchCandidate] = []
        for symbol in symbols:
            candidate = self._score_symbol(symbol, query_terms)
            if candidate.score > 0:
                candidates.append(candidate)

        candidates.sort(key=lambda item: (-item.score, -item.confidence, item.file_path, item.qualname))
        return candidates[:limit]

    def _expand_terms(self, normalized_query: str) -> list[str]:
        terms = set(TERM_RE.findall(normalized_query))
        stop_words = {
            'и', 'в', 'во', 'на', 'по', 'для', 'к', 'ко', 'с', 'со', 'о', 'об', 'от', 'до', 'из', 'за', 'при', 'или',
            'изменить', 'изменения', 'изменение', 'добавить', 'сделать', 'обновить', 'заменить', 'создать',
            'поменять', 'нужно', 'нужен', 'нужна', 'должен', 'должна', 'нужно', 'надо', 'русский', 'русском', 'русскоязычным', 'язык'
        }
        term_list = sorted(term for term in terms if term and term not in stop_words)
        stems = {self._rough_stem(term) for term in term_list if len(term) >= 4}
        return sorted(set(term_list) | {item for item in stems if item})

    def _score_symbol(self, symbol: SymbolRecord, query_terms: list[str]) -> SearchCandidate:
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
        matched_fields: set[str] = set()

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
            matched_fields.add(field_name)
            reasons.append(self._reason_for_field(term, field_name, match_type))

        if symbol.kind in {'function', 'method'}:
            score += 0.8
            reasons.append('это исполняемая точка логики: функция или метод')
        if bundle.get('layer') == 'services':
            score += 0.75
            reasons.append('символ находится в service-слое, который обычно является основной точкой бизнес-логики')
        elif bundle.get('layer') == 'api':
            score -= 0.25
            reasons.append('API-слой обычно оборачивает бизнес-логику, а не реализует ее целиком')
        elif bundle.get('layer') in {'domain', 'storage'}:
            score -= 0.55
            reasons.append('domain/storage-слой чаще хранит данные или инфраструктуру, а не основной use-case')
        if knowledge_title:
            score += 0.6
            reasons.append('для символа есть knowledge-описание в overlay')
        if bundle.get('requirements'):
            score += 0.75
            reasons.append('символ явно связан с requirement-артефактами')
        requirement_rank = bundle.get('requirement_rank')
        if requirement_rank == 0:
            score += 1.2
            reasons.append('symbol указан как первичная точка реализации требования')
        elif isinstance(requirement_rank, int) and requirement_rank == 1:
            score += 0.45
            reasons.append('symbol входит в ближайшие точки реализации требования')
        semantic_focus_bonus = self._semantic_focus_bonus(query_terms, bundle)
        if semantic_focus_bonus > 0:
            score += semantic_focus_bonus
            reasons.append('knowledge-описание символа хорошо совпадает с фокусом изменения')
        granularity_bonus = self._target_granularity_bonus(query_terms, symbol, bundle)
        if granularity_bonus > 0:
            score += granularity_bonus
            reasons.append('символ похож на непосредственную точку локального изменения')
        if len(matched_terms) >= 2:
            score += 0.75
            reasons.append('совпало несколько терминов запроса, поэтому кандидат устойчивее')

        confidence = self._confidence(score, len(matched_terms), len(query_terms), len(matched_fields))
        category = self._relevance_category(score, confidence)
        return SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=round(score, 2),
            confidence=round(confidence, 2),
            relevance_category=category,
            reasons=self._deduplicate_reasons(reasons)[:8],
            docstring=symbol.docstring,
            knowledge_title=knowledge_title,
            requirements=[item.get('id', '') for item in bundle.get('requirements', [])],
        )

    def _match_term(self, term: str, field_value: str, tokens: set[str]) -> str | None:
        if term in tokens:
            return 'exact_token'
        if term in field_value:
            return 'substring'
        stem = self._rough_stem(term)
        if stem and any(token.startswith(stem) for token in tokens):
            return 'stem'
        return None

    def _reason_for_field(self, term: str, field_name: str, match_type: str) -> str:
        labels = {
            'name': 'имени символа',
            'qualname': 'полном имени',
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
        prefix = {
            'exact_token': 'дал точное совпадение в',
            'substring': 'дал совпадение в',
            'stem': 'дал совпадение по основе слова в',
        }[match_type]
        return f'термин «{term}» {prefix} {labels[field_name]}'

    def _semantic_focus_bonus(self, query_terms: list[str], bundle: dict) -> float:
        title = self._normalize_text(str(bundle.get('symbol_title', '')))
        description = self._normalize_text(str(bundle.get('symbol_description', '')))
        keywords = self._normalize_text(' '.join(bundle.get('keywords', []) or []))
        focus_score = 0.0
        focus_groups = {
            'notification_text': ({'текст', 'уведомл', 'уведомления', 'сообщени', 'message', 'формирован', 'формулировк'}, 1.4),
            'reporting': ({'сводк', 'summary', 'отчет', 'агент'}, 0.8),
        }
        combined = f"{title} {description} {keywords}".strip()
        for terms, bonus in focus_groups.values():
            if any(term in query_terms for term in terms) and any(term in combined for term in terms):
                focus_score += bonus
        if any(term in query_terms for term in {'текст', 'текста'}) and any(term in query_terms for term in {'уведомл', 'уведомления', 'сообщени'}):
            if ('текст' in combined or 'text' in combined) and ('уведомл' in combined or 'message' in combined):
                focus_score += 1.35
        return focus_score

    def _target_granularity_bonus(self, query_terms: list[str], symbol: SymbolRecord, bundle: dict) -> float:
        name = self._normalize_text(symbol.name)
        module_name = self._normalize_text(symbol.module_name)
        title = self._normalize_text(str(bundle.get('symbol_title', '')))
        text_change_terms = {'текст', 'уведомл', 'уведомления', 'сообщени', 'формулировк', 'формирован', 'сообщения'}
        if any(term in query_terms for term in text_change_terms):
            message_like = (
                'notification' in module_name
                or 'message' in name
                or 'уведомлен' in title
                or 'текст' in title
                or 'text' in title
                or 'уведомлен' in name
            )
            if message_like:
                return 1.8
            if symbol.kind in {'function', 'method'} and ('service' in module_name or bundle.get('layer') == 'services'):
                return -1.45
        return 0.0

    def _confidence(self, score: float, matched_terms: int, total_terms: int, matched_fields: int) -> float:
        if total_terms <= 0:
            return 0.0
        effective_term_count = max(1, min(total_terms, 4))
        term_coverage = min(matched_terms, effective_term_count) / effective_term_count
        normalized_score = min(score / max(effective_term_count * 4.2, 1.0), 1.0)
        field_diversity = min(matched_fields / 4.0, 1.0)
        return min(1.0, (term_coverage * 0.45) + (normalized_score * 0.35) + (field_diversity * 0.20))

    def _relevance_category(self, score: float, confidence: float) -> str:
        if confidence >= 0.72 or score >= 8.5:
            return 'высокая'
        if confidence >= 0.42 or score >= 4.8:
            return 'средняя'
        return 'низкая'

    def _deduplicate_reasons(self, reasons: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for reason in reasons:
            if reason not in seen:
                unique.append(reason)
                seen.add(reason)
        return unique

    def _rough_stem(self, value: str) -> str:
        term = value.casefold().replace('ё', 'е')
        suffixes = (
            'иями', 'ями', 'ами', 'иями', 'ение', 'ений', 'ения', 'ировать', 'ировать',
            'овать', 'овать', 'иться', 'ить', 'ать', 'ять', 'ение', 'ений', 'ения', 'ности', 'ность', 'иями',
            'ому', 'ему', 'ого', 'его', 'ий', 'ый', 'ой', 'ая', 'яя', 'ое', 'ее', 'ам', 'ям', 'ах', 'ях', 'ов', 'ев',
            'ия', 'ие', 'ий', 'иям', 'ием', 'ию', 'ия', 'ть', 'ет', 'ют', 'ут', 'ем', 'им', 'ом', 'ем', 'ие', 'ий',
            'ия', 'ию', 'ию', 'ен', 'на', 'но', 'ны', 'а', 'я', 'ы', 'и', 'у', 'ю', 'е', 'о',
        )
        for suffix in suffixes:
            if len(term) - len(suffix) >= 4 and term.endswith(suffix):
                return term[: -len(suffix)]
        return term

    def _normalize_text(self, value: str) -> str:
        normalized = value.casefold().replace('ё', 'е')
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized.strip()
