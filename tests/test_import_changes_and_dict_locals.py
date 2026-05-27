from codecollector.domain.models import ChangeRequest
from codecollector.validation.semantic_checks import validate_patch_static_semantics


def test_json_load_local_dict_get_is_not_contract_result_field_access() -> None:
    original = (
        'import json\n'
        'from dataclasses import dataclass\n\n'
        '@dataclass\n'
        'class Note:\n'
        '    subject: str\n'
        '    content: str\n'
        '    id: str | None = None\n\n'
        'class Store:\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError\n'
    )
    patched = original.replace(
        '        raise NotImplementedError\n',
        '        with open(note_id, "r", encoding="utf-8") as f:\n'
        '            data = json.load(f)\n'
        '        note_id_value = data.get("id") or note_id\n'
        '        return Note(subject=data["subject"], content=data["content"], id=note_id_value)\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(title='Загрузить заметку', description='Прочитать JSON.', project='demo'),
        target_qualname='demo.Store.load',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        parent_qualname='demo.Store',
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'demo.Note',
                'source_code': 'from dataclasses import dataclass\n@dataclass\nclass Note:\n    subject: str\n    content: str\n    id: str | None = None\n',
            },
            {
                'kind': 'method',
                'name': 'load',
                'qualname': 'demo.Store.load',
                'parent_qualname': 'demo.Store',
                'signature': 'def load(self, note_id: str) -> Note:',
            },
        ],
    )

    assert not any(issue.code == 'unknown_contract_result_attribute' for issue in block.issues)


def test_import_changes_unused_import_is_reported() -> None:
    original = (
        'class Helper:\n'
        '    pass\n'
    )
    patched = original + (
        '\n'
        'def build(value: str) -> str:\n'
        '    import re\n'
        '    return re.escape(value)\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить helper', description='Сформировать строку.', project='demo'),
        target_qualname='demo.Helper',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        insert_scope='module_body',
        import_changes=[{'action': 'add_from_import', 'module': 're', 'names': ['escape']}],
    )

    assert any(issue.code == 'unused_import_change' for issue in block.issues)
    assert 'escape' in block.details['import_changes_usage_check']['unused_imports']


def test_import_changes_unresolved_project_module_is_reported(tmp_path) -> None:
    original = 'class Storage:\n    pass\n'
    patched = original + (
        '\n'
        'def build(value):\n'
        '    return SearchResult(value)\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить результат', description='Создать result object.', project='demo'),
        target_qualname='demo.Storage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        insert_scope='module_body',
        import_changes=[{'action': 'add_from_import', 'module': 'demo.models', 'names': ['SearchResult']}],
        project_root=tmp_path,
    )

    assert any(issue.code == 'unresolved_import_change_module' for issue in block.issues)
    assert block.details['import_changes_resolvable_check']['unresolved']


def test_import_changes_unresolved_project_name_is_reported(tmp_path) -> None:
    package = tmp_path / 'demo'
    package.mkdir()
    (package / 'helpers.py').write_text('def other_helper():\n    pass\n', encoding='utf-8')

    original = 'class Storage:\n    pass\n'
    patched = original + (
        '\n'
        'def build(value):\n'
        '    return SearchResult(value)\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить результат', description='Создать result object.', project='demo'),
        target_qualname='demo.Storage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        insert_scope='module_body',
        import_changes=[{'action': 'add_from_import', 'module': 'demo.helpers', 'names': ['SearchResult']}],
        project_root=tmp_path,
    )

    assert any(issue.code == 'unresolved_import_change_name' for issue in block.issues)
    assert block.details['import_changes_resolvable_check']['unresolved'][0]['missing_names'] == ['SearchResult']


def test_import_changes_standard_library_module_is_resolvable(tmp_path) -> None:
    original = 'class Storage:\n    pass\n'
    patched = original + (
        '\n'
        'def build(value):\n'
        '    return datetime.now()\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить дату', description='Использовать стандартную библиотеку.', project='demo'),
        target_qualname='demo.Storage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        insert_scope='module_body',
        import_changes=[{'action': 'add_from_import', 'module': 'datetime', 'names': ['datetime']}],
        project_root=tmp_path,
    )

    assert not any(issue.code == 'unresolved_import_change_module' for issue in block.issues)
    assert not any(issue.code == 'unresolved_import_change_name' for issue in block.issues)
    assert block.details['import_changes_resolvable_check']['checked'][0]['is_stdlib'] is True
