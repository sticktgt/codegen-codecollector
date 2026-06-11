from pathlib import Path

import yaml

from codecollector.config import load_config
from codecollector.onboarding.knowledge_traceability import KnowledgeTraceabilityService
from codecollector.workspace.workspace_service import WorkspaceService


def test_traceability_update_adds_requirement_and_cr_to_symbol(tmp_path: Path) -> None:
    project_root = tmp_path / 'src'
    project_root.mkdir()
    service = KnowledgeTraceabilityService(project_root, load_config())

    result = service.update_for_applied_workspace(
        symbol_qualnames=['app.service.build_report'],
        change_request_id='CR-001',
        requirement_ids=['REQ-001'],
        applied_at='2026-06-10T18:47:05',
    )

    payload = yaml.safe_load(Path(result['knowledge_path']).read_text(encoding='utf-8'))
    entry = payload['symbols']['app.service.build_report']
    assert result['updated'] is True
    assert entry['requirements'] == ['REQ-001']
    assert entry['change_requests'] == [{'id': 'CR-001', 'applied_at': '2026-06-10T18:47:05'}]


def test_traceability_update_is_idempotent_for_same_cr_and_requirement(tmp_path: Path) -> None:
    project_root = tmp_path / 'src'
    project_root.mkdir()
    service = KnowledgeTraceabilityService(project_root, load_config())

    kwargs = dict(
        symbol_qualnames=['app.service.build_report'],
        change_request_id='CR-001',
        requirement_ids=['REQ-001', 'REQ-001'],
        applied_at='2026-06-10T18:47:05',
    )
    service.update_for_applied_workspace(**kwargs)
    second = service.update_for_applied_workspace(**kwargs)

    payload = yaml.safe_load(Path(second['knowledge_path']).read_text(encoding='utf-8'))
    entry = payload['symbols']['app.service.build_report']
    assert second['updated'] is False
    assert entry['requirements'] == ['REQ-001']
    assert entry['change_requests'] == [{'id': 'CR-001', 'applied_at': '2026-06-10T18:47:05'}]


def test_workspace_traceability_targets_inserted_method_not_parent_class(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path, load_config())
    bundle = {
        'apply_result': {
            'artifact': {
                'operation': 'insert_after_symbol',
                'target_qualname': 'note.note_storage.NoteStorage',
                'insert_scope': 'class_body',
                'expected_new_symbol_kind': 'method',
                'parent_qualname': 'note.note_storage.NoteStorage',
                'replacement_code': 'def find_note_file_by_id(self, note_id: str):\n    return None\n',
            },
            'impact': {
                'target_qualname': 'note.note_storage.NoteStorage',
                'symbols_in_changed_files': [
                    'note.note_storage.NoteStorage.find_note_file_by_id',
                    'tests.test_generated_generate_test_NoteStorage.TestFindNoteFileById',
                    'tests.test_generated_generate_test_NoteStorage.TestFindNoteFileById.test_find_note_file_by_id',
                ],
            },
        },
        'execution_summary': {
            'selected_target': 'note.note_storage.NoteStorage',
        },
    }

    assert service._extract_traceability_symbols_from_bundle(bundle) == [
        'note.note_storage.NoteStorage.find_note_file_by_id'
    ]


def test_workspace_traceability_targets_replacement_symbol(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path, load_config())
    bundle = {
        'apply_result': {
            'artifact': {
                'operation': 'replace_symbol',
                'target_qualname': 'editor.editor_window.EditorWindow.open_note',
            },
            'impact': {
                'target_qualname': 'editor.editor_window.EditorWindow.open_note',
                'symbols_in_changed_files': ['editor.editor_window.EditorWindow.open_note'],
            },
        },
        'execution_summary': {
            'selected_target': 'editor.editor_window.EditorWindow.open_note',
        },
    }

    assert service._extract_traceability_symbols_from_bundle(bundle) == [
        'editor.editor_window.EditorWindow.open_note'
    ]


def test_workspace_traceability_skips_unresolved_insert_target_instead_of_parent(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path, load_config())
    bundle = {
        'apply_result': {
            'artifact': {
                'operation': 'insert_after_symbol',
                'target_qualname': 'note.note_storage.NoteStorage',
                'insert_scope': 'class_body',
                'expected_new_symbol_kind': 'method',
                'parent_qualname': 'note.note_storage.NoteStorage',
                'replacement_code': 'not valid python',
            },
            'impact': {
                'target_qualname': 'note.note_storage.NoteStorage',
                'symbols_in_changed_files': [
                    'tests.test_generated_generate_test_NoteStorage.TestFindNoteFileById',
                ],
            },
        },
        'execution_summary': {
            'selected_target': 'note.note_storage.NoteStorage',
        },
    }

    assert service._extract_traceability_symbols_from_bundle(bundle) == []
