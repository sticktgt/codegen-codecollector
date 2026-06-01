from codecollector.external_codegen.adapter import patch_artifact_from_result
from codecollector.validation.semantic_checks import validate_code_artifact_static_semantics


def _block_for(code: str, target: str = "pkg.mod.Service.method"):
    return validate_code_artifact_static_semantics(
        result_payload={
            "code_artifact": {
                "operation": "replace_symbol",
                "target_qualname": target,
                "target_file": "pkg/mod.py",
                "code": code,
            }
        },
        step_name="external_generate",
        expected_operation="replace_symbol",
        expected_target_qualname=target,
    )


def test_replace_symbol_accepts_single_target_method():
    block = _block_for("def method(self):\n    return 1\n")
    assert block.ok


def test_replace_symbol_rejects_extra_top_level_method():
    block = _block_for("def method(self):\n    return 1\n\ndef helper():\n    return 2\n")
    assert not block.ok
    assert any(issue.code == "replace_symbol_artifact_must_contain_single_symbol" for issue in block.issues)


def test_replace_symbol_rejects_target_name_mismatch():
    block = _block_for("def other(self):\n    return 1\n")
    assert not block.ok
    assert any(issue.code == "replace_symbol_artifact_target_name_mismatch" for issue in block.issues)


def test_replace_symbol_rejects_nested_helper_function():
    block = _block_for("def method(self):\n    def helper():\n        return 2\n    return helper()\n")
    assert not block.ok
    assert any(issue.code == "replace_symbol_artifact_contains_nested_symbol" for issue in block.issues)


def test_replace_symbol_accepts_single_target_class():
    block = _block_for("class Service:\n    pass\n", target="pkg.mod.Service")
    assert block.ok


def test_replace_symbol_reports_extra_symbol_before_syntax_error_when_indentation_breaks_parse():
    code = '''def _setup_connections(self) -> None:
        self.editor.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self) -> None:
        self.is_modified = True
'''
    block = _block_for(code, target="editor.editor_window.EditorWindow._setup_connections")
    assert not block.ok
    assert block.issues[0].code == "replace_symbol_artifact_contains_extra_symbol_text"
    assert any(issue.code == "code_artifact_ast_parse_failed" for issue in block.issues)
    assert "_on_text_changed" in block.issues[0].message



def test_replace_symbol_accepts_and_normalizes_single_indented_method_artifact():
    payload = {
        "code_artifact": {
            "operation": "replace_symbol",
            "target_qualname": "pkg.mod.Service.method",
            "target_file": "pkg/mod.py",
            "code": "    def method(self):\n        return 1\n",
        }
    }
    block = validate_code_artifact_static_semantics(
        result_payload=payload,
        step_name="external_repair",
        expected_operation="replace_symbol",
        expected_target_qualname="pkg.mod.Service.method",
    )

    assert block.ok
    assert payload["code_artifact"]["code"].startswith("def method")
    assert block.details["code_normalization"]["applied"] is True


def test_patch_artifact_from_result_normalizes_indented_replace_symbol_code():
    artifact = patch_artifact_from_result(
        {
            "status": "ok",
            "code_artifact": {
                "operation": "replace_symbol",
                "target_qualname": "pkg.mod.Service.method",
                "target_file": "pkg/mod.py",
                "code": "    def method(self):\n        return 1\n",
                "import_changes": [],
            },
        },
        "pkg.mod.Service.method",
    )

    assert artifact.replacement_code.startswith("def method")
    assert artifact.replacement_code.endswith("return 1\n")
