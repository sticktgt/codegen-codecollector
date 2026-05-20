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
