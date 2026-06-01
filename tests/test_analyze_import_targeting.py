from __future__ import annotations

from types import SimpleNamespace

from codecollector.analysis.analyze_service import AnalyzeService
from codecollector.domain.models import SymbolRecord


def _service() -> AnalyzeService:
    return object.__new__(AnalyzeService)


def test_explicit_symbol_hints_ignore_protective_constraints() -> None:
    service = _service()

    hints = service._explicit_symbol_name_hints([
        {
            "title": "Исправить верхнеуровневый импорт QsciScintilla",
            "description": "Использовать PyQt5.Qsci вместо старого пути импорта.",
            "constraints": [
                "Не менять _init_editor.",
                "Не менять __init__.",
                "Сохранить защитную обработку ситуации, когда QScintilla недоступна.",
            ],
        }
    ])

    assert "_init_editor" not in hints
    assert "__init__" not in hints
    assert "PyQt5.Qsci" in hints


def test_import_level_change_boosts_preferred_file_symbols() -> None:
    service = _service()
    project_key = "/project"
    symbols = [
        SymbolRecord(
            qualname="editor.editor_window.EditorWindow._init_editor",
            name="_init_editor",
            kind="method",
            file_path="editor/editor_window.py",
            module_name="editor.editor_window",
            parent_qualname="editor.editor_window.EditorWindow",
            start_line=10,
            end_line=20,
            source_code="def _init_editor(self): ...",
        ),
        SymbolRecord(
            qualname="common.utils.NotePathBuilder.__init__",
            name="__init__",
            kind="method",
            file_path="common/utils.py",
            module_name="common.utils",
            parent_qualname="common.utils.NotePathBuilder",
            start_line=1,
            end_line=2,
            source_code="def __init__(self): ...",
        ),
    ]

    class Store:
        def list_symbols(self, key: str):
            assert key == project_key
            return symbols

    services = SimpleNamespace(project_root=project_key, store=Store())
    by_qualname = {}

    service._add_hint_candidates(
        by_qualname,
        services,
        {"search_plan": {"preferred_files": ["editor/editor_window.py"]}},
        strong_preferred_files=True,
    )

    candidate = by_qualname["editor.editor_window.EditorWindow._init_editor"]
    assert candidate.score == 80.0
    assert "import" in candidate.reasons[0]


def test_dependency_detection_problem_is_import_level_change() -> None:
    service = _service()

    assert service._request_is_import_level_change([
        {
            "title": "Исправить запуск текстового редактора после установки QScintilla",
            "description": (
                "После установки зависимости QScintilla приложение всё равно сообщает, "
                "что редактор не найден. Нужно исправить подключение компонента."
            ),
            "constraints": [
                "Сохранить защитную обработку ситуации, когда QScintilla действительно недоступна.",
            ],
        }
    ]) is True


def test_regular_dependency_request_is_not_import_level_change_without_connection_problem() -> None:
    service = _service()

    assert service._request_is_import_level_change([
        {
            "title": "Добавить настройку сервиса",
            "description": "Использовать существующую зависимость сервиса при сохранении заметки.",
        }
    ]) is False


def test_non_symbol_change_kind_blocks_only_import_change() -> None:
    service = _service()

    assert service._non_symbol_change_kind(
        {
            "change_kind": {
                "value": "import_change",
                "confidence": 0.91,
                "reason": "нужно изменить подключение зависимости",
            }
        },
        {},
    ) == "import_change"


def test_non_symbol_change_kind_does_not_block_file_level_change() -> None:
    service = _service()

    assert service._non_symbol_change_kind(
        {
            "change_kind": {
                "value": "file_level_change",
                "confidence": 0.95,
                "reason": "нужно настроить размер окна внутри существующего метода",
            }
        },
        {},
    ) is None


def test_non_symbol_change_manual_review_clears_symbol_target() -> None:
    service = _service()

    recommended_target, updated = service._mark_manual_review_for_non_symbol_change(
        target_recommendation={
            "recommended_target": "editor.editor_window.EditorWindow._init_editor",
            "change_kind": {"value": "import_change", "confidence": 0.9},
            "warnings": [],
        },
        search_plan={},
        recommended_target="editor.editor_window.EditorWindow._init_editor",
        change_kind="import_change",
    )

    assert recommended_target is None
    assert updated["recommended_target"] is None
    assert updated["manual_review_required"] is True
    assert updated["target_role"] == "import_change"
    assert updated["post_processing"]["non_symbol_change_manual_review"]["previous_recommended_target"] == "editor.editor_window.EditorWindow._init_editor"
