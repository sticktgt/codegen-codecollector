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


def _class_symbol() -> SymbolRecord:
    return SymbolRecord(
        file_path='note/note_search.py',
        module_name='note.note_search',
        name='SearchResult',
        qualname='note.note_search.SearchResult',
        kind='class',
        parent_qualname='note.note_search',
        start_line=1,
        end_line=3,
        source_code='',
    )


def test_insert_class_body_accepts_decorated_method(tmp_path: Path) -> None:
    target = tmp_path / 'note_search.py'
    target.write_text(
        'class SearchResult:\n'
        '    def __init__(self, note):\n'
        '        self.note = note\n'
        '\n'
        'class SearchUI:\n'
        '    pass\n',
        encoding='utf-8',
    )

    _service()._apply_operation_to_file(
        target,
        _class_symbol(),
        PatchArtifact(
            target_qualname='note.note_search.SearchResult',
            replacement_code=(
                '@property\n'
                'def note_id(self):\n'
                '    return getattr(self.note, "id", None)\n'
            ),
            operation='insert_after_symbol',
            insert_scope='class_body',
            expected_new_symbol_kind='method',
            parent_qualname='note.note_search.SearchResult',
        ),
    )

    updated = target.read_text(encoding='utf-8')
    assert '\n    @property\n    def note_id(self):\n' in updated
    assert '        return getattr(self.note, "id", None)\n' in updated
    assert '\nclass SearchUI:\n' in updated


def test_insert_class_body_reindents_decorated_method_with_class_indent(tmp_path: Path) -> None:
    target = tmp_path / 'note_search.py'
    target.write_text(
        'class SearchResult:\n'
        '    def __init__(self, note):\n'
        '        self.note = note\n',
        encoding='utf-8',
    )

    _service()._apply_operation_to_file(
        target,
        _class_symbol(),
        PatchArtifact(
            target_qualname='note.note_search.SearchResult',
            replacement_code=(
                '    @property\n'
                '    def note_id(self):\n'
                '        return self.note.id\n'
            ),
            operation='insert_after_symbol',
            insert_scope='class_body',
            expected_new_symbol_kind='method',
            parent_qualname='note.note_search.SearchResult',
        ),
    )

    updated = target.read_text(encoding='utf-8')
    assert '\n    @property\n    def note_id(self):\n        return self.note.id\n' in updated


def test_insert_class_body_rejects_decorator_without_method(tmp_path: Path) -> None:
    target = tmp_path / 'note_search.py'
    target.write_text(
        'class SearchResult:\n'
        '    def __init__(self, note):\n'
        '        self.note = note\n',
        encoding='utf-8',
    )

    with pytest.raises(ValueError, match='expects generated code to start with def, async def, or method decorator'):
        _service()._apply_operation_to_file(
            target,
            _class_symbol(),
            PatchArtifact(
                target_qualname='note.note_search.SearchResult',
                replacement_code='@property\nnote_id = 1\n',
                operation='insert_after_symbol',
                insert_scope='class_body',
                expected_new_symbol_kind='method',
                parent_qualname='note.note_search.SearchResult',
            ),
        )


def _module_class_symbol(start_line: int, end_line: int) -> SymbolRecord:
    return SymbolRecord(
        file_path='export/outlook_exporter.py',
        module_name='export.outlook_exporter',
        name='OutlookExporter',
        qualname='export.outlook_exporter.OutlookExporter',
        kind='class',
        parent_qualname='export.outlook_exporter',
        start_line=start_line,
        end_line=end_line,
        source_code='',
    )


def test_apply_import_changes_removes_plain_import(tmp_path: Path) -> None:
    target = tmp_path / 'outlook_exporter.py'
    target.write_text(
        'import re\n'
        'import win32com.client\n'
        'from typing import List\n'
        '\n'
        'class OutlookExporter:\n'
        '    def __init__(self):\n'
        '        raise NotImplementedError\n',
        encoding='utf-8',
    )

    _service()._apply_operation_to_file(
        target,
        _module_class_symbol(5, 7),
        PatchArtifact(
            target_qualname='export.outlook_exporter.OutlookExporter',
            replacement_code=(
                'class OutlookExporter:\n'
                '    def __init__(self):\n'
                '        self.outlook_app = None\n'
            ),
            operation='replace_symbol',
            import_changes=[{'action': 'remove_import', 'module': 'win32com.client'}],
        ),
    )

    updated = target.read_text(encoding='utf-8')
    assert 'import win32com.client' not in updated
    assert 'import re' in updated
    assert 'from typing import List' in updated
    assert 'class OutlookExporter:' in updated


def test_apply_import_changes_removes_name_from_from_import(tmp_path: Path) -> None:
    target = tmp_path / 'outlook_exporter.py'
    target.write_text(
        'from win32com import client, other\n'
        '\n'
        'class OutlookExporter:\n'
        '    pass\n',
        encoding='utf-8',
    )

    _service()._apply_operation_to_file(
        target,
        _module_class_symbol(3, 4),
        PatchArtifact(
            target_qualname='export.outlook_exporter.OutlookExporter',
            replacement_code='class OutlookExporter:\n    pass\n',
            operation='replace_symbol',
            import_changes=[{'action': 'remove_from_import', 'module': 'win32com', 'names': ['client']}],
        ),
    )

    updated = target.read_text(encoding='utf-8')
    assert 'from win32com import other' in updated
    assert 'client' not in updated
