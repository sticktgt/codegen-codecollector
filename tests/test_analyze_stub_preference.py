from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codecollector.analysis.analyze_service import AnalyzeService
from codecollector.domain.models import SearchCandidate, SymbolRecord


class _FakeStore:
    def __init__(self, symbols: dict[str, SymbolRecord]) -> None:
        self.symbols = symbols

    def get_symbol(self, project_root: str, qualname: str) -> SymbolRecord | None:
        return self.symbols.get(qualname)


def test_analyze_prefers_existing_stub_method_for_implementation_request(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    symbol = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='get_save_path',
        qualname='note.note_storage.NoteStorage.get_save_path',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=10,
        end_line=20,
        docstring='Строит полный путь к файлу заметки внутри директории хранения.',
        source_code='    def get_save_path(self, note):\n        raise NotImplementedError("Метод требует реализации")\n',
    )
    candidates = [
        SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=40.0,
            confidence=0.8,
            docstring=symbol.docstring,
        ),
        SearchCandidate(
            qualname='common.utils.NotePathBuilder',
            name='NotePathBuilder',
            kind='class',
            file_path='common/utils.py',
            score=45.0,
            confidence=0.9,
            docstring='Утилита построения путей.',
            ranked_by_llm=True,
            llm_recommended=True,
            llm_rank=1,
        ),
    ]
    services = SimpleNamespace(project_root=tmp_path, store=_FakeStore({symbol.qualname: symbol}))

    result = service._prefer_existing_stub_target_for_implementation(
        services=services,
        base_query='Реализовать построение полного пути сохранения заметки внутри корневой директории.',
        candidates=candidates,
        target_recommendation={
            'recommended_operation': 'insert_after_symbol',
            'recommended_target': 'common.utils.NotePathBuilder',
            'target_confidence': 0.9,
        },
        requested_operation='insert_after_symbol',
        user_operation=None,
    )

    assert result is not None
    recommended_target, updated_candidates, target_recommendation = result
    assert recommended_target == symbol.qualname
    assert target_recommendation['recommended_operation'] == 'replace_symbol'
    assert target_recommendation['target_role'] == 'target'
    assert target_recommendation['insert_scope']['value'] == 'class_body'
    assert updated_candidates[0].qualname == symbol.qualname


def test_analyze_overrides_user_insert_after_when_existing_stub_matches(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    symbol = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='get_save_path',
        qualname='note.note_storage.NoteStorage.get_save_path',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=80,
        end_line=100,
        docstring='Строит полный путь к файлу заметки по дате создания и имени файла.',
        source_code='    def get_save_path(self, note):\n        # TODO: Реализовать построение пути\n        raise NotImplementedError("Метод требует реализации")\n',
    )
    candidates = [
        SearchCandidate(
            qualname='note.note_storage.NoteStorage',
            name='NoteStorage',
            kind='class',
            file_path='note/note_storage.py',
            score=35.0,
            confidence=1.0,
            docstring='Управление хранением и загрузкой заметок.',
            ranked_by_llm=True,
            llm_recommended=True,
            llm_rank=1,
        ),
        SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=51.0,
            confidence=1.0,
            docstring=symbol.docstring,
            ranked_by_llm=True,
            llm_recommended=False,
            llm_rank=2,
        ),
    ]
    services = SimpleNamespace(project_root=tmp_path, store=_FakeStore({symbol.qualname: symbol}))

    result = service._prefer_existing_stub_target_for_implementation(
        services=services,
        base_query='Реализовать построение полного пути сохранения заметки внутри корневой директории.',
        candidates=candidates,
        target_recommendation={
            'recommended_operation': 'replace_symbol',
            'recommended_target': symbol.qualname,
            'target_confidence': 0.95,
        },
        requested_operation='insert_after_symbol',
        user_operation='insert_after_symbol',
    )

    assert result is not None
    recommended_target, updated_candidates, target_recommendation = result
    assert recommended_target == symbol.qualname
    assert updated_candidates[0].qualname == symbol.qualname
    assert target_recommendation['recommended_operation'] == 'replace_symbol'
    assert target_recommendation['target_role'] == 'target'
    assert target_recommendation['post_processing']['existing_stub_preferred_over_insert']['user_operation'] == 'insert_after_symbol'
    assert any(
        item.get('code') == 'user_operation_overridden_by_existing_stub'
        for item in target_recommendation.get('warnings', [])
    )


def test_analyze_keeps_rerank_replace_stub_over_user_insert_after(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    symbol = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='get_save_path',
        qualname='note.note_storage.NoteStorage.get_save_path',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=80,
        end_line=100,
        docstring='Строит полный путь к файлу заметки по дате создания и имени файла.',
        source_code='    def get_save_path(self, note):\n        raise NotImplementedError("Метод требует реализации")\n',
    )
    candidates = [
        SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=50.0,
            confidence=0.95,
            docstring=symbol.docstring,
            ranked_by_llm=True,
            llm_recommended=True,
            llm_rank=1,
        ),
        SearchCandidate(
            qualname='note.note_storage.NoteStorage',
            name='NoteStorage',
            kind='class',
            file_path='note/note_storage.py',
            score=35.0,
            confidence=1.0,
            docstring='Управление хранением и загрузкой заметок.',
        ),
    ]
    services = SimpleNamespace(project_root=tmp_path, store=_FakeStore({symbol.qualname: symbol}))

    result = service._accept_recommended_replace_stub_over_insert(
        services=services,
        base_query='Реализовать построение полного пути сохранения заметки внутри корневой директории.',
        candidates=candidates,
        target_recommendation={
            'recommended_operation': 'replace_symbol',
            'recommended_target': symbol.qualname,
            'target_confidence': 0.95,
        },
        recommended_target=symbol.qualname,
        requested_operation='insert_after_symbol',
        user_operation='insert_after_symbol',
    )

    assert result is not None
    recommended_target, updated_candidates, target_recommendation = result
    assert recommended_target == symbol.qualname
    assert updated_candidates[0].qualname == symbol.qualname
    assert target_recommendation['recommended_operation'] == 'replace_symbol'
    assert target_recommendation['target_role'] == 'target'
    assert target_recommendation['insert_scope']['value'] == 'class_body'
    assert 'llm_replace_stub_kept_over_insert' in target_recommendation['post_processing']


def test_analyze_normalizes_additive_class_property_request_to_member_insert(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    service.config = SimpleNamespace(analysis_min_confidence_auto_recommend_target=0.75)
    symbol = SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchResult',
        qualname='note.note_search.SearchResult',
        kind='class',
        parent_qualname='note.note_search',
        start_line=10,
        end_line=30,
        docstring='Результат поиска заметки.',
        source_code='class SearchResult:\n    def __init__(self, note, preview, match_positions):\n        self.note = note\n',
    )
    candidates = [
        SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=90.0,
            confidence=0.95,
            docstring=symbol.docstring,
            ranked_by_llm=True,
            llm_recommended=True,
            llm_rank=1,
        )
    ]
    services = SimpleNamespace(project_root=tmp_path, store=_FakeStore({symbol.qualname: symbol}))

    result = service._prefer_member_insert_over_class_replace(
        services=services,
        base_query=(
            'Добавить возможность получить идентификатор найденной заметки из объекта результата поиска. '
            'Результат поиска должен предоставлять note_id, полученный из связанной заметки. '
            'Не менять существующий конструктор результата поиска, если можно добавить вычисляемое свойство.'
        ),
        candidates=candidates,
        target_recommendation={
            'recommended_operation': 'replace_symbol',
            'recommended_target': symbol.qualname,
            'target_confidence': 0.95,
            'expected_new_symbol_kind': 'method',
        },
        recommended_target=symbol.qualname,
        requested_operation='replace_symbol',
        user_operation=None,
        search_plan={
            'expected_new_symbols': [{'name': 'note_id', 'kind': 'method'}],
        },
    )

    assert result is not None
    recommended_target, updated_candidates, target_recommendation = result
    assert recommended_target == symbol.qualname
    assert updated_candidates[0].qualname == symbol.qualname
    assert target_recommendation['recommended_operation'] == 'insert_after_symbol'
    assert target_recommendation['insert_scope']['value'] == 'class_body'
    assert target_recommendation['parent_qualname'] == symbol.qualname
    assert target_recommendation['target_role'] == 'parent_class'
    assert 'class_replace_normalized_to_member_insert' in target_recommendation['post_processing']


def test_analyze_keeps_class_body_class_anchor_for_insert_after(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    search_result = SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchResult',
        qualname='note.note_search.SearchResult',
        kind='class',
        parent_qualname='note.note_search',
        start_line=10,
        end_line=30,
        docstring='Результат поиска заметки.',
        source_code='class SearchResult:\n    pass\n',
    )
    search_ui = SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchUI',
        qualname='note.note_search.SearchUI',
        kind='class',
        parent_qualname='note.note_search',
        start_line=40,
        end_line=80,
        docstring='UI поиска.',
        source_code='class SearchUI:\n    pass\n',
    )

    class Store(_FakeStore):
        def list_symbols_in_file(self, project_root: str, file_path: str):
            return [search_result, search_ui]

    candidates = [
        SearchCandidate(
            qualname=search_result.qualname,
            name=search_result.name,
            kind=search_result.kind,
            file_path=search_result.file_path,
            score=90.0,
            confidence=0.95,
            docstring=search_result.docstring,
        )
    ]
    services = SimpleNamespace(
        project_root=tmp_path,
        store=Store({search_result.qualname: search_result, search_ui.qualname: search_ui}),
    )

    recommended_target, updated_candidates, target_recommendation = service._post_process_recommended_target(
        services=services,
        requested_operation='insert_after_symbol',
        insert_scope='class_body',
        recommended_target=search_result.qualname,
        candidates=candidates,
        target_recommendation={'recommended_target': search_result.qualname},
    )

    assert recommended_target == search_result.qualname
    assert updated_candidates == candidates
    assert target_recommendation['recommended_target'] == search_result.qualname


def test_analyze_overrides_ui_replace_symbol_for_additive_class_property_request(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    service.config = SimpleNamespace(analysis_min_confidence_auto_recommend_target=0.75)
    symbol = SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchResult',
        qualname='note.note_search.SearchResult',
        kind='class',
        parent_qualname='note.note_search',
        start_line=10,
        end_line=30,
        docstring='Результат поиска заметки.',
        source_code='class SearchResult:\n    def __init__(self, note, preview, match_positions):\n        self.note = note\n',
    )
    search_ui = SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchUI',
        qualname='note.note_search.SearchUI',
        kind='class',
        parent_qualname='note.note_search',
        start_line=40,
        end_line=90,
        docstring='UI поиска.',
        source_code='class SearchUI:\n    pass\n',
    )
    candidates = [
        SearchCandidate(
            qualname='note.note_search.SearchUI.show_results',
            name='show_results',
            kind='method',
            file_path='note/note_search.py',
            score=72.0,
            confidence=1.0,
            docstring='Отображает UI и возвращает ID выбранной заметки.',
            ranked_by_llm=True,
            llm_recommended=True,
            llm_rank=1,
        )
    ]
    services = SimpleNamespace(
        project_root=tmp_path,
        store=_FakeStore({symbol.qualname: symbol, search_ui.qualname: search_ui}),
    )

    result = service._prefer_member_insert_over_class_replace(
        services=services,
        base_query=(
            'Добавить возможность получить идентификатор найденной заметки из объекта результата поиска. '
            'Результат поиска должен предоставлять note_id, полученный из связанной заметки. '
            'Не менять существующий конструктор результата поиска, если можно добавить вычисляемое свойство.'
        ),
        candidates=candidates,
        target_recommendation={
            'recommended_operation': 'replace_symbol',
            'recommended_target': symbol.qualname,
            'target_confidence': 0.95,
            'expected_new_symbol_kind': 'property',
        },
        recommended_target=symbol.qualname,
        requested_operation='replace_symbol',
        user_operation='replace_symbol',
        search_plan={
            'expected_new_symbols': [{'name': 'note_id', 'kind': 'property'}],
        },
    )

    assert result is not None
    recommended_target, updated_candidates, target_recommendation = result
    assert recommended_target == symbol.qualname
    assert updated_candidates[0].qualname == symbol.qualname
    assert updated_candidates[0].llm_rank == 1
    assert target_recommendation['recommended_operation'] == 'insert_after_symbol'
    assert target_recommendation['insert_scope']['value'] == 'class_body'
    assert target_recommendation['parent_qualname'] == symbol.qualname
    assert target_recommendation['expected_new_symbol_kind'] == 'method'
    assert target_recommendation['post_processing']['class_replace_normalized_to_member_insert']['user_operation_overridden'] is True


def test_analyze_does_not_replace_stub_when_llm_reported_missing_stub_module(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    symbol = SymbolRecord(
        file_path='export/outlook_exporter.py',
        module_name='export.outlook_exporter',
        name='__init__',
        qualname='export.outlook_exporter.OutlookExporter.__init__',
        kind='method',
        parent_qualname='export.outlook_exporter.OutlookExporter',
        start_line=10,
        end_line=20,
        docstring='Инициализация экспортера Outlook.',
        source_code='    def __init__(self):\n        raise NotImplementedError("Конструктор требует реализации")\n',
    )
    candidates = [
        SearchCandidate(
            qualname=symbol.qualname,
            name=symbol.name,
            kind=symbol.kind,
            file_path=symbol.file_path,
            score=50.0,
            confidence=1.0,
            docstring=symbol.docstring,
        )
    ]
    services = SimpleNamespace(project_root=tmp_path, store=_FakeStore({symbol.qualname: symbol}))
    target_recommendation = {
        'recommended_operation': 'insert_after_symbol',
        'recommended_target': 'export.outlook_exporter.OutlookExporter',
        'target_confidence': 0.95,
        'warnings': [
            {
                'code': 'missing_stub_module',
                'message': 'В проекте не найден кандидат-модуль для размещения заглушки.',
            }
        ],
    }

    marked = service._mark_manual_review_for_unsupported_new_container(target_recommendation)
    result = service._prefer_existing_stub_target_for_implementation(
        services=services,
        base_query='Добавить минимальную локальную заглушку модуля win32com.client.',
        candidates=candidates,
        target_recommendation=marked,
        requested_operation='insert_after_symbol',
        user_operation=None,
    )

    assert result is None
    assert marked['manual_review_required'] is True
    assert any(item.get('code') == 'unsupported_new_file_required' for item in marked['warnings'])
    assert marked['post_processing']['new_file_requirement_preserved']['manual_review_required'] is True


def test_manual_review_required_for_missing_stub_module_warning(tmp_path: Path) -> None:
    service = AnalyzeService.__new__(AnalyzeService)
    service.config = SimpleNamespace(analysis_min_confidence_auto_recommend_target=0.75)
    candidate = SearchCandidate(
        qualname='export.outlook_exporter.OutlookExporter.__init__',
        name='__init__',
        kind='method',
        file_path='export/outlook_exporter.py',
        score=50.0,
        confidence=1.0,
    )

    assert service._manual_review_required(
        {},
        {
            'recommended_target': candidate.qualname,
            'target_confidence': 1.0,
            'warnings': [{'code': 'missing_stub_module', 'message': 'Нужен новый stub-модуль.'}],
        },
        [candidate],
        recommended_target=candidate.qualname,
    ) is True
