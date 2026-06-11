from pathlib import Path

from codecollector.domain.models import ChangeRequest
from codecollector.validation.semantic_checks import (
    validate_generated_test_static_semantics,
    validate_patch_static_semantics,
)


def test_generated_test_rejects_unknown_pytest_fixture_qapp(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_open_note(qapp, monkeypatch):\n"
        "    assert True\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="editor.editor_window.EditorWindow.open_note",
        requested_operation="replace_symbol",
        generated_symbol_names=["editor.editor_window.EditorWindow.open_note"],
    )

    assert not block.ok
    assert any(issue.code == "generated_test_uses_unapproved_external_pytest_fixture" for issue in block.issues)


def test_generated_test_rejects_real_parent_instance_for_method_target(tmp_path: Path) -> None:
    module_file = tmp_path / "editor" / "editor_window.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        "class EditorWindow:\n"
        "    def open_note(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from editor.editor_window import EditorWindow\n\n"
        "def test_open_note():\n"
        "    window = EditorWindow()\n"
        "    assert EditorWindow.open_note(window) is None\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="editor.editor_window.EditorWindow.open_note",
        requested_operation="replace_symbol",
        generated_symbol_names=["editor.editor_window.EditorWindow.open_note"],
    )

    assert not block.ok
    assert any(issue.code == "generated_test_instantiates_target_parent_class" for issue in block.issues)


def test_generated_test_rejects_project_instance_method_called_on_class(tmp_path: Path) -> None:
    module_file = tmp_path / "note" / "note_storage.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        "class NoteStorage:\n"
        "    def load_from_file(self, file_path: Path) -> object:\n"
        "        return object()\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from note.note_storage import NoteStorage\n\n"
        "def test_open_note(monkeypatch):\n"
        "    assert NoteStorage.load_from_file('x.note') is not None\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="editor.editor_window.EditorWindow.open_note",
        requested_operation="replace_symbol",
        generated_symbol_names=["editor.editor_window.EditorWindow.open_note"],
    )

    assert not block.ok
    assert any(issue.code == "generated_test_calls_project_instance_method_on_class" for issue in block.issues)


def test_patch_static_semantics_rejects_project_method_called_on_class() -> None:
    original = (
        "class NoteStorage:\n"
        "    def load_from_file(self, file_path: Path) -> object:\n"
        "        return object()\n\n"
        "class EditorWindow:\n"
        "    def __init__(self, storage):\n"
        "        self.storage = storage\n"
        "    def open_note(self):\n"
        "        raise NotImplementedError()\n"
    )
    patched = original.replace(
        "    def open_note(self):\n"
        "        raise NotImplementedError()\n",
        "    def open_note(self):\n"
        "        return NoteStorage.load_from_file('x.note')\n",
    )

    block = validate_patch_static_semantics(
        requested_operation="replace_symbol",
        change_request=ChangeRequest(title="Open", description="Open note", project="demo"),
        target_qualname="editor.editor_window.EditorWindow.open_note",
        original_file_text=original,
        patched_file_text=patched,
        changed_files=["editor/editor_window.py"],
        target_file="editor/editor_window.py",
        insert_scope="class_body",
        parent_qualname="editor.editor_window.EditorWindow",
        related_symbols=[
            {
                "kind": "class",
                "name": "NoteStorage",
                "qualname": "note.note_storage.NoteStorage",
                "source_code": (
                    "class NoteStorage:\n"
                    "    def load_from_file(self, file_path: Path) -> object:\n"
                    "        return object()\n"
                ),
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == "contract_method_called_on_class_instead_of_instance" for issue in block.issues)


def test_generated_test_allows_unbound_call_for_exact_target_method(tmp_path: Path) -> None:
    module_file = tmp_path / "editor" / "editor_window.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        "class EditorWindow:\n"
        "    def open_note(self):\n"
        "        self.storage.load_from_file(self.selected_path)\n"
        "        return None\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from types import SimpleNamespace\n"
        "from editor.editor_window import EditorWindow\n\n"
        "class FakeStorage:\n"
        "    def __init__(self):\n"
        "        self.calls = []\n"
        "    def load_from_file(self, path):\n"
        "        self.calls.append(path)\n"
        "        return object()\n\n"
        "def test_open_note_uses_fake_self():\n"
        "    storage = FakeStorage()\n"
        "    fake_self = SimpleNamespace(storage=storage, selected_path='x.note')\n"
        "    EditorWindow.open_note(fake_self)\n"
        "    assert storage.calls == ['x.note']\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="editor.editor_window.EditorWindow.open_note",
        requested_operation="replace_symbol",
        generated_symbol_names=["editor.editor_window.EditorWindow.open_note"],
    )

    assert block.ok, [issue.code + ': ' + issue.message for issue in block.issues]


def test_generated_test_allows_self_receiver_in_pytest_class_method(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "class TestGeneratedOpenNote:\n"
        "    def test_open_note(self, monkeypatch):\n"
        "        assert True\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="editor.editor_window.EditorWindow.open_note",
        requested_operation="replace_symbol",
        generated_symbol_names=["editor.editor_window.EditorWindow.open_note"],
    )

    assert not any(
        issue.code == "generated_test_uses_unapproved_external_pytest_fixture" and issue.symbol == "self"
        for issue in block.issues
    )
    fixture_details = block.details["pytest_fixture_check"]
    assert any(
        item.get("fixture") == "self" and item.get("ignored_reason") == "pytest_test_method_receiver"
        for item in fixture_details["checked_fixtures"]
    )
