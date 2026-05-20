from pathlib import Path

import pytest

from codecollector.domain.models import PatchArtifact, SymbolRecord
from codecollector.patching.apply_service import ApplyService


def _service() -> ApplyService:
    return ApplyService.__new__(ApplyService)


def _method_symbol() -> SymbolRecord:
    return SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='__init__',
        qualname='note.note_storage.NoteStorage.__init__',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=2,
        end_line=4,
        source_code='',
    )


def test_replace_symbol_method_payload_is_reindented_inside_class(tmp_path: Path) -> None:
    target = tmp_path / 'note_storage.py'
    target.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        raise NotImplementedError\n'
        '\n'
        '    def save(self):\n'
        '        return self.base_dir\n',
        encoding='utf-8',
    )

    _service()._apply_operation_to_file(
        target,
        _method_symbol(),
        PatchArtifact(
            target_qualname='note.note_storage.NoteStorage.__init__',
            replacement_code=(
                'def __init__(self, base_dir):\n'
                '    self.base_dir = base_dir\n'
            ),
            operation='replace_symbol',
        ),
    )

    updated = target.read_text(encoding='utf-8')
    assert '\n    def __init__(self, base_dir):\n' in updated
    assert '\ndef __init__(self, base_dir):\n' not in updated
    assert '    def save(self):\n' in updated


def test_replace_symbol_rejects_method_that_would_move_to_module_level(tmp_path: Path) -> None:
    target = tmp_path / 'note_storage.py'
    target.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        raise NotImplementedError\n',
        encoding='utf-8',
    )

    service = _service()
    with pytest.raises(ValueError, match='module-level function'):
        service._assert_target_structure_preserved(
            'class NoteStorage:\n'
            '    pass\n'
            '\n'
            'def __init__(self, base_dir):\n'
            '    self.base_dir = base_dir\n',
            _method_symbol(),
            PatchArtifact(
                target_qualname='note.note_storage.NoteStorage.__init__',
                replacement_code='def __init__(self, base_dir):\n    self.base_dir = base_dir\n',
                operation='replace_symbol',
            ),
        )
