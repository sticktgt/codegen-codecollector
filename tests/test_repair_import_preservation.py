from __future__ import annotations

from codecollector.orchestration.pipeline_service import PipelineService


def test_repair_preserves_previous_import_when_repaired_code_still_uses_name() -> None:
    previous_payload = {
        'code_artifact': {
            'code': 'def open_note(self):\n    return QFileDialog.getOpenFileName(self)\n',
            'import_changes': [
                {'action': 'add_from_import', 'module': 'PyQt5.QtWidgets', 'names': ['QFileDialog']},
                {'action': 'add_from_import', 'module': 'unused.module', 'names': ['UnusedName']},
            ],
        }
    }
    repair_payload = {
        'status': 'ok',
        'code_artifact': {
            'code': (
                'def open_note(self):\n'
                '    file_path, _ = QFileDialog.getOpenFileName(self)\n'
                '    return file_path\n'
            ),
            'import_changes': [],
        },
    }

    merged = PipelineService._merge_repair_import_changes(
        PipelineService.__new__(PipelineService),
        previous_payload,
        repair_payload,
    )

    import_changes = merged['code_artifact']['import_changes']
    assert {'action': 'add_from_import', 'module': 'PyQt5.QtWidgets', 'names': ['QFileDialog']} in import_changes
    assert not any(item.get('module') == 'unused.module' for item in import_changes)


def test_repair_import_preservation_keeps_current_repair_imports() -> None:
    previous_payload = {
        'code_artifact': {
            'code': 'def open_note(self):\n    return old_name\n',
            'import_changes': [
                {'action': 'add_from_import', 'module': 'old.module', 'names': ['old_name']},
            ],
        }
    }
    repair_payload = {
        'status': 'ok',
        'code_artifact': {
            'code': 'def open_note(self):\n    return new_name\n',
            'import_changes': [
                {'action': 'add_from_import', 'module': 'new.module', 'names': ['new_name']},
            ],
        },
    }

    merged = PipelineService._merge_repair_import_changes(
        PipelineService.__new__(PipelineService),
        previous_payload,
        repair_payload,
    )

    assert merged['code_artifact']['import_changes'] == [
        {'action': 'add_from_import', 'module': 'new.module', 'names': ['new_name']},
    ]
