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
