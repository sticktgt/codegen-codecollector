from __future__ import annotations

from json import JSONDecodeError
from pathlib import Path
from types import SimpleNamespace

from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService


def _service() -> AnalysisLlmAssistService:
    service = AnalysisLlmAssistService.__new__(AnalysisLlmAssistService)
    service.config = SimpleNamespace(analysis_llm_rerank_raw_preview_chars=1200)
    return service


def test_rerank_json_repair_handles_premature_top_level_close() -> None:
    service = _service()
    content = '''{
      "recommended_operation": "replace_symbol",
      "operation_confidence": 0.9,
      "operation_reason": "Коротко."
      },
      "insert_scope": "unknown",
      "recommended_candidate_id": "c6",
      "recommended_target": "editor.editor_window.EditorWindow._setup_ui",
      "target_confidence": 0.95,
      "target_reason": "Коротко.",
      "manual_review_required": false,
      "ranked_candidates": [{"candidate_id": "c6", "rank": 1, "reason": "Коротко."}],
      "alternatives": []
    }'''

    parsed = service._try_parse_repaired_rerank_response(content)

    assert parsed is not None
    assert parsed["recommended_target"] == "editor.editor_window.EditorWindow._setup_ui"
    assert parsed["ranked_candidates"][0]["candidate_id"] == "c6"


def test_rerank_fallback_requires_manual_review() -> None:
    service = _service()
    service._safe_float = lambda value: float(value or 0.0)  # type: ignore[method-assign]

    payload = service._fallback_parse_rerank_response(
        '{"recommended_operation":"insert_after_symbol"\n'
        ' "recommended_candidate_id":"c1"\n'
        ' "target_confidence":0.9}',
        [{"candidate_id": "c1", "qualname": "pkg.Controller"}],
        JSONDecodeError("bad", "{}", 0),
    )

    assert payload["recommended_target"] == "pkg.Controller"
    assert payload["manual_review_required"] is True
    assert payload["warnings"][0]["code"] == "analysis_llm_rerank_json_recovered"


def test_rerank_candidate_priority_keeps_ui_setup_candidate_visible() -> None:
    service = _service()
    payload = {
        "request": {
            "titles": ["Кнопка открытия заметки на панели инструментов"],
            "descriptions": ["На панели инструментов должна быть кнопка Открыть заметку."],
            "constraints": ["Использовать уже существующий метод EditorWindow.open_note."],
        },
        "search_plan": {
            "search_plan": {
                "search_queries": ["open_note", "_setup_ui", "toolbutton"],
                "preferred_qualnames": ["editor.editor_window.EditorWindow"],
            }
        },
        "candidate_cards": [
            {
                "candidate_id": "c1",
                "qualname": "editor.editor_window.EditorWindow.open_note",
                "name": "open_note",
                "kind": "method",
                "source_excerpt": "def open_note(self): ...",
            },
            {
                "candidate_id": "c6",
                "qualname": "editor.editor_window.EditorWindow._setup_ui",
                "name": "_setup_ui",
                "kind": "method",
                "source_excerpt": "self.actions_toolbar = QToolBar('Действия', self)\nself.actions_toolbar.addAction('Сохранить', self.save_note)",
            },
        ],
    }

    report = service._prioritize_candidate_cards_for_rerank(payload)

    assert report["reordered"] is True
    assert payload["candidate_cards"][0]["candidate_id"] == "c6"
    assert "ui_action_source_patterns" in report["scores"]["c6"]["priority_reasons"]

from codecollector.analysis.analyze_service import AnalyzeService
from codecollector.domain.models import SearchCandidate


def _analyze_service() -> AnalyzeService:
    service = AnalyzeService.__new__(AnalyzeService)
    service.config = SimpleNamespace(analysis_max_candidate_cards=6)
    return service


def _candidate(qualname: str, name: str | None = None, kind: str = "method", score: float = 1.0) -> SearchCandidate:
    return SearchCandidate(
        qualname=qualname,
        name=name or qualname.rsplit(".", 1)[-1],
        kind=kind,
        file_path="app/window.py",
        score=score,
        confidence=1.0,
        relevance_category="высокая",
        reasons=[],
    )


def test_exact_symbol_match_does_not_override_confident_llm_target() -> None:
    service = _analyze_service()
    toolbar = _candidate("app.window.MainWindow._setup_toolbar", "_setup_toolbar", score=10.0)
    open_document = _candidate("app.window.MainWindow.open_document", "open_document", score=100.0)
    recommendation = {
        "recommended_target": toolbar.qualname,
        "recommended_candidate_id": "c1",
        "recommended_operation": "replace_symbol",
        "target_confidence": 0.95,
        "manual_review_required": False,
        "_diagnostics": {},
    }

    target, candidates, updated, operation_source = service._apply_exact_symbol_match_diagnostics(
        candidates=[toolbar, open_document],
        target_recommendation=recommendation,
        recommended_target=toolbar.qualname,
        explicit_symbol_names={"open_document", open_document.qualname},
        requested_operation="replace_symbol",
    )

    assert target == toolbar.qualname
    assert operation_source is None
    assert [candidate.qualname for candidate in candidates] == [toolbar.qualname, open_document.qualname]
    exact = updated["_diagnostics"]["exact_symbol_matches"]
    assert exact["conflicts_with_selected_target"] is True
    assert exact["used_as_target"] is False
    assert exact["used_as_unconfirmed_fallback"] is False
    assert updated["recommended_target"] == toolbar.qualname


def test_exact_symbol_match_can_be_unconfirmed_fallback_when_llm_has_no_target() -> None:
    service = _analyze_service()
    open_document = _candidate("app.window.MainWindow.open_document", "open_document", score=100.0)
    unrelated = _candidate("app.storage.Repository.save", "save", score=20.0)

    target, candidates, updated, operation_source = service._apply_exact_symbol_match_diagnostics(
        candidates=[unrelated, open_document],
        target_recommendation={"recommended_target": None, "target_confidence": 0.0, "warnings": []},
        recommended_target=None,
        explicit_symbol_names={"open_document", open_document.qualname},
        requested_operation="replace_symbol",
    )

    assert target == open_document.qualname
    assert operation_source == "exact_symbol_match_unconfirmed"
    assert candidates[0].qualname == open_document.qualname
    assert updated["manual_review_required"] is True
    assert updated["target_role"] == "unconfirmed_exact_match"
    assert updated["warnings"][-1]["code"] == "exact_match_requires_review"
    exact = updated["_diagnostics"]["exact_symbol_matches"]
    assert exact["used_as_target"] is False
    assert exact["used_as_unconfirmed_fallback"] is True


def test_rerank_prompt_describes_mentioned_symbols_as_unknown_role() -> None:
    text = Path("codecollector/prompts/analyze_candidate_rerank_user_template.txt").read_text(encoding="utf-8")

    assert "mentioned_symbols" in text
    assert "роль неизвестна" in text
    assert "Само упоминание символа не означает, что его нужно менять" in text
