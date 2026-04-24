from __future__ import annotations

import ast
from dataclasses import asdict
from pathlib import Path
from typing import Any

from codecollector.domain.models import (
    ChangeRequest,
    VerificationBlock,
    VerificationIssue,
    VerificationReport,
)


def _safe_parse(source: str) -> ast.AST | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None

def _parse_with_error(source: str) -> tuple[ast.AST | None, SyntaxError | None]:
    try:
        return ast.parse(source), None
    except SyntaxError as exc:
        return None, exc

def _top_level_defs(tree: ast.AST | None) -> dict[str, str]:
    if tree is None:
        return {}

    result: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[node.name] = "function"
        elif isinstance(node, ast.ClassDef):
            result[node.name] = "class"
    return result


def _constraint_contains(change_request: ChangeRequest, needle: str) -> bool:
    needle_norm = needle.strip().lower()
    return any(needle_norm in str(item).lower() for item in (change_request.constraints or []))


def _extract_new_top_level_symbols(original_text: str, patched_text: str) -> dict[str, str]:
    before = _top_level_defs(_safe_parse(original_text))
    after = _top_level_defs(_safe_parse(patched_text))
    return {name: kind for name, kind in after.items() if name not in before}


def _has_dataclass_decorator_for_class(source: str, class_name: str) -> bool:
    tree = _safe_parse(source)
    if tree is None:
        return False

    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
                    return True
                if isinstance(decorator, ast.Attribute) and decorator.attr == "dataclass":
                    return True
    return False


def _target_symbol_name(target_qualname: str) -> str:
    return target_qualname.rsplit(".", 1)[-1]

def _project_module_exists(project_root: Path, module_name: str) -> bool:
    module_path = module_name.replace(".", "/")
    py_path = project_root / f"{module_path}.py"
    pkg_init = project_root / module_path / "__init__.py"
    return py_path.exists() or pkg_init.exists()


def _collect_called_names(tree: ast.AST) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


def _collect_assert_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            for child in ast.walk(node.test):
                if isinstance(child, ast.Name):
                    names.add(child.id)
                elif isinstance(child, ast.Attribute):
                    names.add(child.attr)
    return names


def _collect_project_imported_names(tree: ast.AST) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("support_app") or node.module.startswith("tests"):
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    imported[imported_name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if module_name.startswith("support_app") or module_name.startswith("tests"):
                    imported_name = alias.asname or module_name.split(".")[-1]
                    imported[imported_name] = module_name
    return imported


def _has_nontrivial_assert(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.Constant):
            continue
        if isinstance(test, ast.Compare):
            values = [test.left, *test.comparators]
            if all(isinstance(item, ast.Constant) for item in values):
                continue
        return True
    return False

def validate_code_artifact_static_semantics(
    *,
    result_payload: dict[str, Any],
    step_name: str,
    expected_operation: str | None = None,
    expected_target_qualname: str | None = None,
) -> VerificationBlock:
    issues: list[VerificationIssue] = []

    code_artifact = result_payload.get("code_artifact") or {}
    code = str(code_artifact.get("code") or "")
    operation = str(code_artifact.get("operation") or "").strip() or None
    target_qualname = str(code_artifact.get("target_qualname") or "").strip() or None
    target_file = str(code_artifact.get("target_file") or "").strip() or None

    if not code.strip():
        issues.append(
            VerificationIssue(
                code="code_artifact_missing_code",
                message=f"{step_name} не вернул непустой code_artifact.code.",
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )
        return VerificationBlock(
            name=f"{step_name}_static_semantics",
            ok=False,
            severity="error",
            issues=issues,
            details={
                "step_name": step_name,
                "operation": operation,
                "target_qualname": target_qualname,
                "target_file": target_file,
            },
        )

    tree, syntax_error = _parse_with_error(code)

    if syntax_error is not None:
        issues.append(
            VerificationIssue(
                code="code_artifact_ast_parse_failed",
                message=str(syntax_error),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )

    if expected_operation and operation and operation != expected_operation:
        issues.append(
            VerificationIssue(
                code="code_artifact_unexpected_operation",
                message=(
                    f"{step_name} вернул operation={operation}, "
                    f"ожидалось {expected_operation}."
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )

    if (
        expected_target_qualname
        and target_qualname
        and target_qualname != expected_target_qualname
    ):
        issues.append(
            VerificationIssue(
                code="code_artifact_unexpected_target",
                message=(
                    f"{step_name} вернул target_qualname={target_qualname}, "
                    f"ожидалось {expected_target_qualname}."
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )

    return VerificationBlock(
        name=f"{step_name}_static_semantics",
        ok=not issues,
        severity="error" if issues else "info",
        issues=issues,
        details={
            "step_name": step_name,
            "operation": operation,
            "target_qualname": target_qualname,
            "target_file": target_file,
            "code_chars": len(code),
            "parseable": tree is not None,
            "expected_operation": expected_operation,
            "expected_target_qualname": expected_target_qualname,
            "syntax_error": (
                {
                    "error_type": type(syntax_error).__name__,
                    "message": str(syntax_error),
                    "lineno": syntax_error.lineno,
                    "offset": syntax_error.offset,
                    "text": (syntax_error.text or "").strip(),
                }
                if syntax_error is not None
                else None
            ),
        },
    )

def validate_patch_static_semantics(
    *,
    requested_operation: str,
    change_request: ChangeRequest,
    target_qualname: str,
    original_file_text: str,
    patched_file_text: str,
    changed_files: list[str],
    target_file: str,
) -> VerificationBlock:
    issues: list[VerificationIssue] = []

    before_tree = _safe_parse(original_file_text)
    after_tree = _safe_parse(patched_file_text)
    target_name = _target_symbol_name(target_qualname)

    if before_tree is None or after_tree is None:
        issues.append(
            VerificationIssue(
                code="patch_ast_parse_failed",
                message="Не удалось разобрать AST исходного или измененного файла.",
                file_path=target_file,
            )
        )
        return VerificationBlock(
            name="patch_static_semantics",
            ok=False,
            severity="error",
            issues=issues,
            details={},
        )

    if target_file not in changed_files:
        issues.append(
            VerificationIssue(
                code="target_file_not_changed",
                message="Измененный target file отсутствует в списке changed_files.",
                file_path=target_file,
            )
        )

    before_defs = _top_level_defs(before_tree)
    after_defs = _top_level_defs(after_tree)
    new_symbols = _extract_new_top_level_symbols(original_file_text, patched_file_text)

    if requested_operation == "replace_symbol":
        if target_name not in after_defs:
            issues.append(
                VerificationIssue(
                    code="target_symbol_missing_after_replace",
                    message="После replace_symbol target symbol отсутствует в файле.",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

    if requested_operation == "insert_after_symbol":
        if target_name not in after_defs:
            issues.append(
                VerificationIssue(
                    code="anchor_symbol_missing_after_insert",
                    message="После insert_after_symbol anchor symbol отсутствует в файле.",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if not new_symbols:
            issues.append(
                VerificationIssue(
                    code="no_new_top_level_symbol",
                    message="После insert_after_symbol не найден новый top-level symbol.",
                    file_path=target_file,
                )
            )

    if _constraint_contains(change_request, "только одну новую функцию"):
        new_functions = [name for name, kind in new_symbols.items() if kind == "function"]
        if len(new_functions) != 1:
            issues.append(
                VerificationIssue(
                    code="constraint_one_new_function_failed",
                    message="Ограничение 'только одну новую функцию' нарушено.",
                    file_path=target_file,
                )
            )

    if _constraint_contains(change_request, "не менять существующие функции"):
        before_functions = {name for name, kind in before_defs.items() if kind == "function"}
        after_functions = {name for name, kind in after_defs.items() if kind == "function"}
        missing_existing = sorted(before_functions - after_functions)
        if missing_existing:
            issues.append(
                VerificationIssue(
                    code="existing_functions_removed",
                    message=f"Нарушено ограничение: исчезли существующие функции: {', '.join(missing_existing)}.",
                    file_path=target_file,
                )
            )

    if _constraint_contains(change_request, "только dataclass"):
        new_classes = [name for name, kind in new_symbols.items() if kind == "class"]
        if not new_classes:
            issues.append(
                VerificationIssue(
                    code="expected_dataclass_not_found",
                    message="Ожидалось добавление dataclass, но новый класс не найден.",
                    file_path=target_file,
                )
            )
        else:
            for class_name in new_classes:
                if not _has_dataclass_decorator_for_class(patched_file_text, class_name):
                    issues.append(
                        VerificationIssue(
                            code="new_class_without_dataclass",
                            message=f"Новый класс {class_name} не помечен @dataclass.",
                            file_path=target_file,
                            symbol=class_name,
                        )
                    )

    return VerificationBlock(
        name="patch_static_semantics",
        ok=not issues,
        severity="error" if issues else "info",
        issues=issues,
        details={
            "requested_operation": requested_operation,
            "target_file": target_file,
            "target_qualname": target_qualname,
            "new_symbols": new_symbols,
        },
    )


def validate_generated_test_static_semantics(
    *,
    project_root: Path,
    test_file_path: Path,
    target_qualname: str,
    requested_operation: str = "replace_symbol",
    generated_symbol_names: list[str] | None = None,
) -> VerificationBlock:
    issues: list[VerificationIssue] = []

    if not test_file_path.exists():
        issues.append(
            VerificationIssue(
                code="generated_test_file_missing",
                message="Файл generated test не найден.",
                file_path=str(test_file_path),
            )
        )
        return VerificationBlock(
            name="generated_test_static_semantics",
            ok=False,
            severity="error",
            issues=issues,
            details={},
        )

    source = test_file_path.read_text(encoding="utf-8")
    tree = _safe_parse(source)
    if tree is None:
        issues.append(
            VerificationIssue(
                code="generated_test_ast_parse_failed",
                message="Generated test не проходит AST parse.",
                file_path=str(test_file_path),
            )
        )
        return VerificationBlock(
            name="generated_test_static_semantics",
            ok=False,
            severity="error",
            issues=issues,
            details={},
        )

    target_name = _target_symbol_name(target_qualname)

    test_functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    if not test_functions:
        issues.append(
            VerificationIssue(
                code="generated_test_has_no_test_functions",
                message="В generated test нет функций вида test_*.",
                file_path=str(test_file_path),
            )
        )

    has_assert = any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    has_pytest_raises = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
        for node in ast.walk(tree)
    )
    if not has_assert and not has_pytest_raises:
        issues.append(
            VerificationIssue(
                code="generated_test_has_no_assertions",
                message="В generated test нет assert и нет pytest.raises.",
                file_path=str(test_file_path),
            )
        )

    if has_assert and not _has_nontrivial_assert(tree):
        issues.append(
            VerificationIssue(
                code="generated_test_only_trivial_asserts",
                message="Generated test содержит только тривиальные assert без проверки поведения.",
                file_path=str(test_file_path),
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue

        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("support_app") or node.module.startswith("tests"):
                if not _project_module_exists(project_root, node.module):
                    issues.append(
                        VerificationIssue(
                            code="generated_test_import_points_to_missing_module",
                            message=f"Import указывает на отсутствующий модуль: {node.module}.",
                            file_path=str(test_file_path),
                        )
                    )

        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if module_name.startswith("support_app") or module_name.startswith("tests"):
                    if not _project_module_exists(project_root, module_name):
                        issues.append(
                            VerificationIssue(
                                code="generated_test_import_points_to_missing_module",
                                message=f"Import указывает на отсутствующий модуль: {module_name}.",
                                file_path=str(test_file_path),
                            )
                        )

    imported_names = _collect_project_imported_names(tree)
    called_names = _collect_called_names(tree)
    asserted_names = _collect_assert_names(tree)

    generated_symbol_names = generated_symbol_names or []
    new_symbol_names = [name.rsplit(".", 1)[-1] for name in generated_symbol_names if name]
    concrete_new_symbols = [name for name in new_symbol_names if name and name != target_name]

    referenced_names = {
        name
        for name in [target_name, *concrete_new_symbols]
        if name and name in source
    }
    used_names = {
        name
        for name in [target_name, *concrete_new_symbols]
        if name and (name in called_names or name in asserted_names)
    }

    if requested_operation == "insert_after_symbol" and concrete_new_symbols:
        if not any(name in referenced_names for name in concrete_new_symbols):
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_reference_new_symbol_name",
                    message=(
                        "Generated test не содержит имени нового symbol, "
                        "добавленного через insert_after_symbol."
                    ),
                    file_path=str(test_file_path),
                    symbol=", ".join(concrete_new_symbols),
                )
            )

        if not any(name in used_names for name in concrete_new_symbols):
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_use_new_symbol",
                    message=(
                        "Generated test не использует новый symbol "
                        "в вызове или проверке."
                    ),
                    file_path=str(test_file_path),
                    symbol=", ".join(concrete_new_symbols),
                )
            )
    else:
        if target_name not in referenced_names:
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_reference_target_name",
                    message=f"Generated test не содержит имени target symbol: {target_name}.",
                    file_path=str(test_file_path),
                    symbol=target_name,
                )
            )

        if target_name in imported_names and target_name not in used_names:
            issues.append(
                VerificationIssue(
                    code="generated_test_imports_target_but_does_not_use_it",
                    message=f"Generated test импортирует {target_name}, но не использует его в вызове или проверке.",
                    file_path=str(test_file_path),
                    symbol=target_name,
                )
            )

    return VerificationBlock(
        name="generated_test_static_semantics",
        ok=not issues,
        severity="error" if issues else "info",
        issues=issues,
        details={
            "requested_operation": requested_operation,
            "target_qualname": target_qualname,
            "generated_symbol_names": generated_symbol_names,
            "test_file_path": str(test_file_path),
            "test_functions": test_functions,
            "has_assert": has_assert,
            "has_pytest_raises": has_pytest_raises,
            "imported_project_names": imported_names,
            "called_names": sorted(called_names),
            "asserted_names": sorted(asserted_names),
            "referenced_names": sorted(referenced_names),
            "used_names": sorted(used_names),
        },
    )


def validate_generated_test_relevance(
    *,
    test_source: str,
    requested_operation: str,
    target_qualname: str,
    generated_symbol_names: list[str],
) -> VerificationBlock:
    issues: list[VerificationIssue] = []

    tree = _safe_parse(test_source)
    if tree is None:
        issues.append(
            VerificationIssue(
                code="generated_test_ast_parse_failed_for_relevance",
                message="Невозможно проверить релевантность: generated test не парсится.",
            )
        )
        return VerificationBlock(
            name="generated_test_relevance",
            ok=False,
            severity="error",
            issues=issues,
            details={},
        )

    target_name = _target_symbol_name(target_qualname)
    new_symbol_names = [name.rsplit(".", 1)[-1] for name in generated_symbol_names if name]
    relevant_names = [target_name, *new_symbol_names]

    normalized_source = test_source.lower()
    matched_names = [name for name in relevant_names if name and name.lower() in normalized_source]

    called_names = _collect_called_names(tree)
    asserted_names = _collect_assert_names(tree)

    if not matched_names:
        issues.append(
            VerificationIssue(
                code="generated_test_not_relevant",
                message="Generated test не содержит явной связи с target symbol или новыми symbol.",
            )
        )

    if requested_operation == "insert_after_symbol":
        concrete_new_symbols = [name for name in new_symbol_names if name and name != target_name]
        if concrete_new_symbols:
            if not any(name in matched_names for name in concrete_new_symbols):
                issues.append(
                    VerificationIssue(
                        code="generated_test_does_not_reference_new_symbol",
                        message="Для insert_after_symbol generated test не ссылается на новый symbol.",
                    )
                )
            if not any(name in called_names or name in asserted_names for name in concrete_new_symbols):
                issues.append(
                    VerificationIssue(
                        code="generated_test_does_not_use_new_symbol",
                        message="Для insert_after_symbol generated test не использует новый symbol в вызове или проверке.",
                    )
                )
    else:
        if target_name not in matched_names:
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_reference_target_symbol",
                    message="Generated test не ссылается на target symbol.",
                    symbol=target_name,
                )
            )
        if target_name not in called_names and target_name not in asserted_names:
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_use_target_symbol",
                    message="Generated test не использует target symbol в вызове или проверке.",
                    symbol=target_name,
                )
            )

    return VerificationBlock(
        name="generated_test_relevance",
        ok=not issues,
        severity="error" if issues else "info",
        issues=issues,
        details={
            "requested_operation": requested_operation,
            "target_qualname": target_qualname,
            "generated_symbol_names": generated_symbol_names,
            "matched_names": matched_names,
            "called_names": sorted(called_names),
            "asserted_names": sorted(asserted_names),
        },
    )


def build_verification_report(
    *,
    blocks: list[VerificationBlock],
) -> VerificationReport:
    production_block_names = {
        "patch_static_semantics",
        "runtime_ast_parse",
        "runtime_py_compile",
        "runtime_ruff",
        "runtime_pytest_recommended",
        "runtime_pytest_full",
        "apply_generated_artifact",
    }
    generated_test_block_names = {
        "generated_test_static_semantics",
        "generated_test_relevance",
    }

    production_failed = any((not block.ok) and block.name in production_block_names for block in blocks)
    generated_test_failed = any((not block.ok) and block.name in generated_test_block_names for block in blocks)

    if production_failed:
        verdict = "verification_failed"
        passed = False
    elif generated_test_failed:
        verdict = "generated_test_verification_failed"
        passed = False
    else:
        verdict = "ready_for_merge_review"
        passed = True

    return VerificationReport(
        verdict=verdict,
        passed=passed,
        blocks=blocks,
        summary={
            "production_failed": production_failed,
            "generated_test_failed": generated_test_failed,
            "blocks": [asdict(block) for block in blocks],
        },
    )