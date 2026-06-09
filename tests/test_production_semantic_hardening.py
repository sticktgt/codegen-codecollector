from __future__ import annotations

from codecollector.domain.models import ChangeRequest
from codecollector.validation.semantic_checks import validate_patch_static_semantics


def _change_request() -> ChangeRequest:
    return ChangeRequest(title='Open item', description='Open item from selected file', project='demo')


def _repository_related_symbol() -> dict[str, str]:
    return {
        'kind': 'class',
        'name': 'Repository',
        'qualname': 'demo.repository.Repository',
        'source_code': (
            'from pathlib import Path\n\n'
            'class Repository:\n'
            '    def load(self, file_path: Path) -> object:\n'
            '        raise NotImplementedError()\n'
        ),
    }


def test_patch_static_semantics_rejects_unknown_dependency_attribute() -> None:
    original = (
        'from pathlib import Path\n\n'
        'class Repository:\n'
        '    def load(self, file_path: Path) -> object:\n'
        '        raise NotImplementedError()\n\n'
        'class Window:\n'
        '    def __init__(self, repository: Repository) -> None:\n'
        '        self.repository = repository\n'
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n',
        '    def open_item(self):\n'
        '        directory = self.repository.base_dir\n'
        '        return self.repository.load(directory / "item.txt")\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=_change_request(),
        target_qualname='demo.window.Window.open_item',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo/window.py'],
        target_file='demo/window.py',
        insert_scope='class_body',
        parent_qualname='demo.window.Window',
        related_symbols=[_repository_related_symbol()],
    )

    assert not block.ok
    assert any(issue.code == 'unknown_injected_dependency_attribute' for issue in block.issues)
    checked = block.details['injected_dependency_attribute_check']['checked_attributes']
    assert any(item['attribute_path'] == 'self.repository.base_dir' for item in checked)


def test_patch_static_semantics_rejects_unknown_dependency_method_from_visible_path_surface() -> None:
    original = (
        'class TextEditor:\n'
        '    pass\n\n'
        'class Window:\n'
        '    def __init__(self, editor: TextEditor) -> None:\n'
        '        self.editor = editor\n'
        '    def reset(self):\n'
        '        self.editor.setText("")\n'
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n',
        '    def open_item(self):\n'
        '        self.editor.set_text("loaded")\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=_change_request(),
        target_qualname='demo.window.Window.open_item',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo/window.py'],
        target_file='demo/window.py',
        insert_scope='class_body',
        parent_qualname='demo.window.Window',
    )

    assert not block.ok
    assert any(issue.code == 'unknown_injected_dependency_method' for issue in block.issues)
    checked = block.details['injected_dependency_method_check']['checked_calls']
    assert checked[0]['visible_methods'] == ['setText']


def test_patch_static_semantics_uses_configured_tuple_call_return_types_for_contract_arguments() -> None:
    original = (
        'from pathlib import Path\n\n'
        'class Repository:\n'
        '    def load(self, file_path: Path) -> object:\n'
        '        raise NotImplementedError()\n\n'
        'class Window:\n'
        '    def __init__(self, repository: Repository) -> None:\n'
        '        self.repository = repository\n'
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n',
        '    def open_item(self):\n'
        '        selected, _ = ExternalPicker.pick()\n'
        '        return self.repository.load(selected)\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=_change_request(),
        target_qualname='demo.window.Window.open_item',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo/window.py'],
        target_file='demo/window.py',
        insert_scope='class_body',
        parent_qualname='demo.window.Window',
        related_symbols=[_repository_related_symbol()],
        known_call_return_types={'ExternalPicker.pick': {'tuple_items': ['str', 'str']}},
    )

    assert not block.ok
    assert any(issue.code == 'contract_call_argument_type_mismatch' for issue in block.issues)
    checked = block.details['contract_call_signature_check']['checked_calls']
    mismatch = checked[0]['argument_type_mismatches'][0]
    assert mismatch['actual_type'] == 'str'
    assert mismatch['expected_type'] == 'Path'


def test_patch_static_semantics_accepts_configured_tuple_return_after_visible_conversion() -> None:
    original = (
        'from pathlib import Path\n\n'
        'class Repository:\n'
        '    def load(self, file_path: Path) -> object:\n'
        '        raise NotImplementedError()\n\n'
        'class Window:\n'
        '    def __init__(self, repository: Repository) -> None:\n'
        '        self.repository = repository\n'
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n',
        '    def open_item(self):\n'
        '        selected, _ = ExternalPicker.pick()\n'
        '        return self.repository.load(Path(selected))\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=_change_request(),
        target_qualname='demo.window.Window.open_item',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo/window.py'],
        target_file='demo/window.py',
        insert_scope='class_body',
        parent_qualname='demo.window.Window',
        related_symbols=[_repository_related_symbol()],
        known_call_return_types={'ExternalPicker.pick': {'tuple_items': ['str', 'str']}},
    )

    assert not any(issue.code == 'contract_call_argument_type_mismatch' for issue in block.issues)


def test_patch_static_semantics_rejects_unknown_self_dependency_alias_read() -> None:
    original = (
        'from pathlib import Path\n\n'
        'class Repository:\n'
        '    def load(self, file_path: Path) -> object:\n'
        '        raise NotImplementedError()\n\n'
        'class Window:\n'
        '    def __init__(self, repository: Repository) -> None:\n'
        '        self.repository = repository\n'
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def open_item(self):\n'
        '        raise NotImplementedError()\n',
        '    def open_item(self):\n'
        '        selected = "item.txt"\n'
        '        return self.repository_alias.load(Path(selected))\n',
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=_change_request(),
        target_qualname='demo.window.Window.open_item',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo/window.py'],
        target_file='demo/window.py',
        insert_scope='class_body',
        parent_qualname='demo.window.Window',
        related_symbols=[_repository_related_symbol()],
    )

    assert not block.ok
    assert any(issue.code == 'unknown_self_attribute' for issue in block.issues)
    details = block.details['self_attribute_usage_check']
    assert any(item['attribute'] == 'repository_alias' for item in details['unknown_attributes'])
