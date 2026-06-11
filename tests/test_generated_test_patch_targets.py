from __future__ import annotations

from pathlib import Path

from codecollector.validation.semantic_checks import validate_generated_test_static_semantics


def test_generated_test_static_semantics_rejects_patch_target_missing_module_attribute(tmp_path: Path) -> None:
    module_file = tmp_path / "app" / "window.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        "class Window:\n"
        "    def open_item(self):\n"
        "        from vendor.dialogs import Picker\n"
        "        return Picker.choose()\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from unittest.mock import patch\n"
        "from app.window import Window\n\n"
        "def test_open_item():\n"
        "    fake_self = object()\n"
        "    with patch('app.window.Picker.choose', return_value='item'):\n"
        "        assert Window.open_item(fake_self) == 'item'\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="app.window.Window.open_item",
        requested_operation="replace_symbol",
        generated_symbol_names=["app.window.Window.open_item"],
    )

    assert not block.ok
    assert any(issue.code == "generated_test_patch_target_missing_project_attribute" for issue in block.issues)
    checked = block.details["patch_target_check"]["checked_patch_targets"]
    assert checked[0]["target"] == "app.window.Picker.choose"
    assert checked[0]["module"] == "app.window"
    assert checked[0]["missing_first_attribute"] == "Picker"


def test_generated_test_static_semantics_accepts_patch_target_existing_module_attribute(tmp_path: Path) -> None:
    module_file = tmp_path / "app" / "window.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        "class Picker:\n"
        "    @staticmethod\n"
        "    def choose():\n"
        "        return None\n\n"
        "class Window:\n"
        "    def open_item(self):\n"
        "        return Picker.choose()\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_generated.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from unittest.mock import patch\n"
        "from app.window import Window\n\n"
        "def test_open_item():\n"
        "    fake_self = object()\n"
        "    with patch('app.window.Picker.choose', return_value='item'):\n"
        "        assert Window.open_item(fake_self) == 'item'\n",
        encoding="utf-8",
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname="app.window.Window.open_item",
        requested_operation="replace_symbol",
        generated_symbol_names=["app.window.Window.open_item"],
    )

    assert not any(issue.code == "generated_test_patch_target_missing_project_attribute" for issue in block.issues)
