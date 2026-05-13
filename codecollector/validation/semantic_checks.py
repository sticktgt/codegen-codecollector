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



def _find_pytest_mock_usage(tree: ast.AST) -> list[dict[str, Any]]:
    """Find generated-test usage of pytest-mock features unavailable by default."""
    findings: list[dict[str, Any]] = []

    def add(code: str, message: str, node: ast.AST, name: str | None = None) -> None:
        findings.append(
            {
                "code": code,
                "message": message,
                "line": getattr(node, "lineno", None),
                "name": name or "",
            }
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest_mock" or alias.name.startswith("pytest_mock."):
                    add(
                        "generated_test_imports_pytest_mock",
                        "Generated test импортирует pytest-mock, но эта зависимость не гарантирована. Для mock/patch используй unittest.mock.",
                        node,
                        alias.name,
                    )

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pytest_mock" or module.startswith("pytest_mock."):
                add(
                    "generated_test_imports_pytest_mock",
                    "Generated test импортирует pytest-mock, но эта зависимость не гарантирована. Для mock/patch используй unittest.mock.",
                    node,
                    module,
                )

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if arg.arg == "mocker":
                    add(
                        "generated_test_uses_mocker_fixture",
                        "Generated test использует fixture `mocker` из pytest-mock. Эта зависимость не гарантирована; тест нужно сгенерировать через unittest.mock или без mocker.",
                        arg,
                        node.name,
                    )

                annotation = arg.annotation
                if isinstance(annotation, ast.Name) and annotation.id == "MockerFixture":
                    add(
                        "generated_test_uses_mocker_fixture",
                        "Generated test использует MockerFixture из pytest-mock. Эта зависимость не гарантирована; тест нужно сгенерировать через unittest.mock или без mocker.",
                        annotation,
                        node.name,
                    )
                elif (
                    isinstance(annotation, ast.Attribute)
                    and isinstance(annotation.value, ast.Name)
                    and annotation.value.id == "pytest"
                    and annotation.attr == "MockerFixture"
                ):
                    add(
                        "generated_test_uses_mocker_fixture",
                        "Generated test использует pytest.MockerFixture. Эта зависимость не гарантирована; тест нужно сгенерировать через unittest.mock или без mocker.",
                        annotation,
                        node.name,
                    )

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "mocker":
                add(
                    "generated_test_uses_mocker_fixture",
                    "Generated test вызывает `mocker.*` из pytest-mock. Эта зависимость не гарантирована; тест нужно сгенерировать через unittest.mock или без mocker.",
                    func,
                    func.attr,
                )

    # Deduplicate while preserving deterministic output.
    seen: set[tuple[str, int | None, str]] = set()
    result: list[dict[str, Any]] = []
    for item in findings:
        key = (str(item.get("code") or ""), item.get("line"), str(item.get("name") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def find_duplicate_symbol_definitions(source: str, module_name: str, file_path: str) -> list[dict[str, Any]]:
    """Return duplicate top-level/class symbol definitions in a Python source file.

    Python itself allows redefining functions and methods. For codecollector this is
    usually an invalid generated patch because the graph index uses qualname as a
    stable symbol key. Detecting duplicates before reindex keeps such cases as
    readable validation issues instead of database constraint errors.
    """
    tree = _safe_parse(source)
    if tree is None:
        return []

    definitions: dict[str, list[dict[str, Any]]] = {}

    def add_definition(qualname: str, *, name: str, kind: str, node: ast.AST) -> None:
        definitions.setdefault(qualname, []).append(
            {
                "qualname": qualname,
                "name": name,
                "kind": kind,
                "line": getattr(node, "lineno", None),
            }
        )

    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_definition(
                f"{module_name}.{node.name}",
                name=node.name,
                kind="function",
                node=node,
            )
        elif isinstance(node, ast.ClassDef):
            class_qualname = f"{module_name}.{node.name}"
            add_definition(class_qualname, name=node.name, kind="class", node=node)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add_definition(
                        f"{class_qualname}.{child.name}",
                        name=child.name,
                        kind="method",
                        node=child,
                    )

    duplicates: list[dict[str, Any]] = []
    for qualname, items in definitions.items():
        if len(items) <= 1:
            continue
        first = items[0]
        duplicates.append(
            {
                "qualname": qualname,
                "name": first["name"],
                "kind": first["kind"],
                "file_path": file_path,
                "lines": [item.get("line") for item in items if item.get("line") is not None],
            }
        )
    return duplicates

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

def _class_methods(tree: ast.AST | None, class_name: str) -> set[str]:
    if tree is None or not class_name:
        return set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()

def _class_node(tree: ast.AST | None, class_name: str) -> ast.ClassDef | None:
    if tree is None or not class_name:
        return None
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _annotation_name(annotation: ast.AST | None) -> str:
    if annotation is None:
        return ""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return _call_display_name(annotation)
    if isinstance(annotation, ast.Subscript):
        return _annotation_name(annotation.value)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", 1)[-1]
    return ""


def _self_attribute_type_map(tree: ast.AST | None, class_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return result

    init_node = next(
        (
            child
            for child in class_node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__"
        ),
        None,
    )
    if init_node is None:
        return result

    arg_types = {
        arg.arg: _annotation_name(arg.annotation).rsplit(".", 1)[-1]
        for arg in [*init_node.args.posonlyargs, *init_node.args.args, *init_node.args.kwonlyargs]
        if arg.arg != "self"
    }

    for node in ast.walk(init_node):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        value_type = arg_types.get(node.value.id, "")
        if not value_type:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                result[target.attr] = value_type

    return result


def _related_method_names_by_parent_type(related_symbols: list[Any] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in related_symbols or []:
        if not isinstance(item, dict):
            item = asdict(item)

        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        parent_qualname = str(item.get("parent_qualname") or "").strip()
        parent_type = parent_qualname.rsplit(".", 1)[-1] if parent_qualname else ""

        if kind == "method" and name and parent_type:
            result.setdefault(parent_type, set()).add(name)

        if kind == "class":
            class_name = name or str(item.get("qualname") or "").rsplit(".", 1)[-1]
            source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
            methods = _class_methods(_safe_parse(source), class_name)
            if class_name and methods:
                result.setdefault(class_name, set()).update(methods)

    return result


def _self_dependency_method_call(func: ast.expr) -> tuple[str, str] | None:
    """Return (access_path, method_name) for calls like self.x.y.method(...)."""
    if not isinstance(func, ast.Attribute):
        return None

    method_name = func.attr
    receiver = func.value
    parts: list[str] = []

    while isinstance(receiver, ast.Attribute):
        parts.append(receiver.attr)
        receiver = receiver.value

    if isinstance(receiver, ast.Name) and receiver.id == "self" and parts:
        access_path = "self." + ".".join(reversed(parts))
        return access_path, method_name

    return None


def _access_path_root_attribute(access_path: str) -> str:
    parts = str(access_path or "").split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "self" else ""


def _method_owner_types_by_name(related_symbols: list[Any] | None) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in related_symbols or []:
        if not isinstance(item, dict):
            item = asdict(item)

        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        parent_qualname = str(item.get("parent_qualname") or "").strip()
        parent_type = parent_qualname.rsplit(".", 1)[-1] if parent_qualname else ""

        if kind == "method" and name and parent_type:
            result.setdefault(name, set()).add(parent_type)

    return result


def _visible_self_call_path_types(
    owner_tree: ast.AST | None,
    related_symbols: list[Any] | None,
) -> dict[str, str]:
    """Infer nested self.<path> owner types only from visible calls.

    Example: if visible code calls self.service.repository.list_by_agent(...)
    and list_by_agent is a visible method of TicketRepository, infer
    self.service.repository -> TicketRepository.
    """
    if owner_tree is None:
        return {}

    owner_types_by_method = _method_owner_types_by_name(related_symbols)
    result: dict[str, str] = {}

    for node in ast.walk(owner_tree):
        if not isinstance(node, ast.Call):
            continue

        match = _self_dependency_method_call(node.func)
        if match is None:
            continue

        access_path, method_name = match
        owner_types = owner_types_by_method.get(method_name, set())
        if len(owner_types) != 1:
            continue

        result.setdefault(access_path, next(iter(owner_types)))

    return result


def _check_unknown_injected_dependency_methods(
    *,
    owner_tree: ast.AST,
    scan_tree: ast.AST | None,
    related_symbols: list[Any] | None,
    target_file: str,
    target_qualname: str,
    parent_qualname: str | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if scan_tree is None:
        return [], {"checked_calls": [], "skipped_calls": []}

    parent_class_name = _class_name_from_qualname(parent_qualname or target_qualname.rsplit(".", 1)[0])
    injected_types = _self_attribute_type_map(owner_tree, parent_class_name)
    visible_path_types = _visible_self_call_path_types(owner_tree, related_symbols)
    known_methods_by_type = _related_method_names_by_parent_type(related_symbols)

    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []
    skipped_calls: list[dict[str, Any]] = []

    for node in ast.walk(scan_tree):
        if not isinstance(node, ast.Call):
            continue

        match = _self_dependency_method_call(node.func)
        if match is None:
            continue

        access_path, method_name = match
        attr_name = _access_path_root_attribute(access_path)
        attr_type = ""

        if access_path in visible_path_types:
            attr_type = visible_path_types[access_path]
        elif access_path.count(".") == 1 and attr_name:
            attr_type = injected_types.get(attr_name, "")

        if not attr_type:
            skipped_calls.append(
                {
                    "call": _call_display_name(node.func),
                    "reason": "unknown_injected_attribute_or_path_type",
                    "access_path": access_path,
                    "line": getattr(node, "lineno", None),
                }
            )
            continue

        known_methods = known_methods_by_type.get(attr_type, set())
        if not known_methods:
            skipped_calls.append(
                {
                    "call": _call_display_name(node.func),
                    "reason": "no_known_methods_for_injected_type",
                    "access_path": access_path,
                    "injected_type": attr_type,
                    "line": getattr(node, "lineno", None),
                }
            )
            continue

        checked = {
            "call": _call_display_name(node.func),
            "line": getattr(node, "lineno", None),
            "access_path": access_path,
            "attribute": attr_name,
            "injected_type": attr_type,
            "method": method_name,
            "known_methods": sorted(known_methods),
        }
        checked_calls.append(checked)

        if method_name not in known_methods:
            issues.append(
                VerificationIssue(
                    code="unknown_injected_dependency_method",
                    message=(
                        f"В сгенерированном коде вызов {_call_display_name(node.func)} использует метод "
                        f"{method_name} у зависимости {access_path} типа {attr_type}, но такой метод "
                        "не найден в видимом проектном контексте. Для repair: используй только методы "
                        "зависимости, явно видимые в context pack / contract context, или передай явный "
                        "контракт через request/context."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

    return issues, {
        "injected_attribute_types": injected_types,
        "visible_path_types": visible_path_types,
        "known_methods_by_type": {key: sorted(value) for key, value in known_methods_by_type.items()},
        "checked_calls": checked_calls,
        "skipped_calls": skipped_calls,
    }




def _class_name_from_qualname(value: str | None) -> str:
    return str(value or '').strip().rsplit('.', 1)[-1]


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
                candidate = decorator
                if isinstance(candidate, ast.Call):
                    candidate = candidate.func

                if isinstance(candidate, ast.Name) and candidate.id == "dataclass":
                    return True
                if isinstance(candidate, ast.Attribute) and candidate.attr == "dataclass":
                    return True
    return False




def _module_name_from_target_file(target_file: str) -> str:
    normalized = str(target_file or "").replace("\\", "/").strip("/")
    if normalized.endswith("/__init__.py"):
        normalized = normalized[: -len("/__init__.py")]
    elif normalized.endswith(".py"):
        normalized = normalized[:-3]
    return normalized.replace("/", ".")

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


def _collect_project_imported_names(tree: ast.AST, project_root: Path | None = None) -> dict[str, str]:
    imported: dict[str, str] = {}

    def is_project_module(module_name: str) -> bool:
        if not module_name:
            return False
        if project_root is not None:
            return _project_module_exists(project_root, module_name)
        return module_name.startswith("support_app") or module_name.startswith("tests")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if is_project_module(node.module):
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    imported[imported_name] = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if is_project_module(module_name):
                    imported_name = alias.asname or module_name.split(".")[-1]
                    imported[imported_name] = module_name
    return imported


def _collect_defined_names(tree: ast.AST) -> set[str]:
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)

        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)

        if isinstance(node, ast.arg):
            defined.add(node.arg)

        if isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)

        if isinstance(node, ast.Import):
            for alias in node.names:
                defined.add(alias.asname or alias.name.split(".")[0])

        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    defined.add(alias.asname or alias.name)

    return defined


def _collect_loaded_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _builtin_names() -> set[str]:
    import builtins

    return set(dir(builtins))


def _find_unresolved_names(tree: ast.AST) -> list[str]:
    loaded = _collect_loaded_names(tree)
    defined = _collect_defined_names(tree)
    unresolved = {
        name
        for name in loaded
        if name not in defined
        and name not in _builtin_names()
        and not name.startswith("__")
    }
    return sorted(unresolved)


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


def _call_display_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return "<dynamic call>"


def _required_contract_call_names(required_contracts: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in required_contracts or []:
        if not isinstance(item, dict):
            continue
        qualname = str(item.get("qualname") or "").strip()
        name = str(item.get("name") or qualname.rsplit(".", 1)[-1]).strip()
        if not name:
            continue
        key = qualname or name
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "qualname": qualname,
            "name": name,
            "reason": str(item.get("reason") or ""),
            "source": str(item.get("source") or ""),
        })
    return result


def _check_required_contract_usage(
    *,
    scan_tree: ast.AST | None,
    required_contracts: list[dict[str, Any]] | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    contracts = _required_contract_call_names(required_contracts)
    if not contracts:
        return [], {"required_contracts": [], "checked_calls": [], "missing_contracts": [], "skipped": False}
    if scan_tree is None:
        return [], {"required_contracts": contracts, "checked_calls": [], "missing_contracts": [], "skipped": True, "reason": "no_generated_symbol_scan_tree"}

    called_names: set[str] = set()
    checked_calls: list[dict[str, Any]] = []
    for node in ast.walk(scan_tree):
        if not isinstance(node, ast.Call):
            continue
        display_name = _call_display_name(node.func)
        called_name = _called_symbol_name(node.func) or display_name.rsplit(".", 1)[-1]
        if called_name:
            called_names.add(called_name)
        checked_calls.append({
            "call": display_name,
            "name": called_name,
            "line": getattr(node, "lineno", None),
        })

    missing = [item for item in contracts if item["name"] not in called_names]
    issues = [
        VerificationIssue(
            code="required_contract_not_used",
            message=(
                "Пользователь потребовал переиспользовать существующий production contract, "
                f"но generated symbol не вызывает {item.get('qualname') or item.get('name')}. "
                "Для repair: замени ручную реализацию бизнес-логики на вызов этого видимого contract."
            ),
            severity="error",
            file_path=target_file,
            symbol=target_qualname,
        )
        for item in missing
    ]
    return issues, {
        "required_contracts": contracts,
        "checked_calls": checked_calls,
        "missing_contracts": missing,
        "skipped": False,
    }


def _called_symbol_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _attribute_receiver_display(func: ast.expr) -> str:
    if not isinstance(func, ast.Attribute):
        return ""
    return _call_display_name(func.value)


def _split_identifier_tokens(value: str) -> set[str]:
    import re

    text = str(value or "")
    parts: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text):
        if not chunk:
            continue
        parts.extend(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", chunk))
    return {part.lower() for part in parts if part}


def _method_receiver_matches_contract(func: ast.expr, spec: dict[str, Any]) -> bool:
    if not isinstance(func, ast.Attribute):
        return False

    receiver_display = _attribute_receiver_display(func)
    receiver_tokens = _split_identifier_tokens(receiver_display)
    if not receiver_tokens:
        return False

    parent_qualname = str(spec.get("parent_qualname") or "")
    parent_name = parent_qualname.rsplit(".", 1)[-1]
    parent_tokens = _split_identifier_tokens(parent_name)
    if not parent_tokens:
        return False

    # Attribute calls are checked only when the receiver name resembles the
    # related contract owner. This avoids matching arbitrary short methods such
    # as payload.get(...) to TicketRepository.get(...).
    return bool(receiver_tokens & parent_tokens)


def _top_level_def_nodes(tree: ast.AST | None, names: set[str]) -> list[ast.stmt]:
    if tree is None:
        return []
    return [
        node
        for node in getattr(tree, "body", [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in names
    ]


def _class_method_nodes(tree: ast.AST | None, class_name: str, method_names: set[str]) -> list[ast.stmt]:
    if tree is None or not class_name:
        return []
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in method_names
            ]
    return []


def _module_from_nodes(nodes: list[ast.stmt]) -> ast.Module:
    return ast.Module(body=list(nodes), type_ignores=[])


def _parse_required_call_args(signature: str, *, kind: str) -> dict[str, Any] | None:
    signature = str(signature or "").strip()
    if not signature.startswith(("def ", "async def ")):
        return None

    try:
        tree = ast.parse(f"{signature}\n    pass\n")
    except SyntaxError:
        return None

    fn = next(
        (
            node
            for node in getattr(tree, "body", [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if fn is None:
        return None

    args = fn.args
    positional_args = list(args.posonlyargs) + list(args.args)
    defaults_count = len(args.defaults or [])
    required_positional = max(0, len(positional_args) - defaults_count)
    required_positional_args = positional_args[:required_positional]

    # Calls such as ``obj.method(value)`` do not pass ``self``/``cls`` explicitly.
    if kind == "method" and required_positional_args:
        first_arg_name = required_positional_args[0].arg
        if first_arg_name in {"self", "cls"}:
            required_positional_args = required_positional_args[1:]
            required_positional = max(0, required_positional - 1)

    required_positional_names = [arg.arg for arg in required_positional_args]

    required_keyword_only = [
        arg.arg
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False)
        if default is None
    ]

    return_annotation = ""
    if getattr(fn, "returns", None) is not None:
        try:
            return_annotation = ast.unparse(fn.returns)
        except Exception:
            return_annotation = _annotation_name(fn.returns)

    return {
        "required_positional": required_positional,
        "required_positional_names": required_positional_names,
        "required_keyword_only": required_keyword_only,
        "return_annotation": return_annotation,
        "signature": signature,
    }


def _known_imported_names(tree: ast.AST, import_changes: list[dict[str, Any]] | None = None) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[-1])

    for change in import_changes or []:
        if not isinstance(change, dict):
            continue
        action = str(change.get("action") or "").strip()
        if action == "add_from_import":
            for name in change.get("names") or []:
                if isinstance(name, str) and name.strip():
                    names.add(name.strip().split(" as ")[-1].strip())
        elif action == "add_import":
            module = str(change.get("module") or "").strip()
            if module:
                names.add(module.rsplit(".", 1)[-1])
    return names


def _literal_value_for_diagnostic(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and (isinstance(node.value, str) or node.value is None):
        return repr(node.value)
    return None


def _change_request_text(change_request: ChangeRequest | None) -> str:
    if change_request is None:
        return ""
    parts = [
        str(getattr(change_request, "title", "") or ""),
        str(getattr(change_request, "description", "") or ""),
        *[str(item) for item in (getattr(change_request, "constraints", None) or [])],
        *[str(item) for item in (getattr(change_request, "notes", None) or [])],
    ]
    return "\n".join(parts)


def _literal_is_explicitly_requested(literal: str, request_text: str) -> bool:
    normalized = literal.strip("'\"")
    return bool(normalized) and normalized in request_text


def _contract_call_specs(
    related_symbols: list[Any] | None,
    *,
    available_names: set[str],
) -> dict[str, dict[str, Any]]:
    raw_specs: dict[str, list[dict[str, Any]]] = {}
    for item in related_symbols or []:
        if not isinstance(item, dict):
            item = asdict(item)
        kind = str(item.get("kind") or "").strip()
        if kind not in {"function", "method"}:
            continue
        name = str(item.get("name") or "").strip()
        qualname = str(item.get("qualname") or "").strip()
        signature = str(item.get("signature") or "").strip()
        if not name or not qualname:
            continue
        parsed_signature = _parse_required_call_args(signature, kind=kind)
        if parsed_signature is None:
            continue

        # Direct function calls are safe to check when the name is available in the target file.
        # Attribute method calls are checked only by attribute name, and ambiguous names are skipped below.
        if kind == "function" and name not in available_names:
            continue

        raw_specs.setdefault(name, []).append(
            {
                "name": name,
                "kind": kind,
                "qualname": qualname,
                "parent_qualname": str(item.get("parent_qualname") or "").strip(),
                "signature": parsed_signature["signature"],
                "required_positional": parsed_signature["required_positional"],
                "required_positional_names": parsed_signature.get("required_positional_names", []),
                "required_keyword_only": parsed_signature["required_keyword_only"],
                "return_annotation": parsed_signature.get("return_annotation", ""),
            }
        )

    # Avoid false positives for overloaded or duplicated short names in the visible context.
    return {name: specs[0] for name, specs in raw_specs.items() if len(specs) == 1}


def _check_contract_call_signatures(
    *,
    tree: ast.AST,
    related_symbols: list[Any] | None,
    scan_tree: ast.AST | None = None,
    target_file: str,
    target_qualname: str,
    import_changes: list[dict[str, Any]] | None = None,
    change_request: ChangeRequest | None = None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    available_names = _known_imported_names(tree, import_changes)
    specs_by_name = _contract_call_specs(related_symbols, available_names=available_names)
    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []
    skipped_calls: list[dict[str, Any]] = []
    request_text = _change_request_text(change_request)

    if not specs_by_name:
        return issues, {
            "known_contract_call_specs": [],
            "checked_calls": checked_calls,
            "skipped_calls": skipped_calls,
        }

    tree_to_scan = scan_tree or tree
    for node in ast.walk(tree_to_scan):
        if not isinstance(node, ast.Call):
            continue
        called_name = _called_symbol_name(node.func)
        if not called_name or called_name not in specs_by_name:
            continue

        spec = specs_by_name[called_name]
        if spec.get("kind") == "function":
            if not isinstance(node.func, ast.Name):
                skipped_calls.append(
                    {
                        "call": _call_display_name(node.func),
                        "reason": "function_contract_called_as_attribute",
                        "line": getattr(node, "lineno", None),
                    }
                )
                continue
        elif spec.get("kind") == "method":
            if not _method_receiver_matches_contract(node.func, spec):
                skipped_calls.append(
                    {
                        "call": _call_display_name(node.func),
                        "reason": "receiver_does_not_match_contract_owner",
                        "contract_qualname": spec.get("qualname"),
                        "line": getattr(node, "lineno", None),
                    }
                )
                continue

        call_name = _call_display_name(node.func)
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            skipped_calls.append({"call": call_name, "reason": "star_args", "line": getattr(node, "lineno", None)})
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            skipped_calls.append({"call": call_name, "reason": "kwargs_unpack", "line": getattr(node, "lineno", None)})
            continue

        keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg}
        supplied_positional = len(node.args)
        required_positional = int(spec.get("required_positional") or 0)
        required_positional_names = list(spec.get("required_positional_names") or [])
        placeholder_literals: list[dict[str, Any]] = []
        for index, arg in enumerate(node.args[:required_positional]):
            literal = _literal_value_for_diagnostic(arg)
            if literal is None or _literal_is_explicitly_requested(literal, request_text):
                continue
            arg_name = required_positional_names[index] if index < len(required_positional_names) else f"arg{index + 1}"
            placeholder_literals.append(
                {
                    "argument": arg_name,
                    "literal": literal,
                    "position": index + 1,
                    "line": getattr(arg, "lineno", getattr(node, "lineno", None)),
                }
            )
        required_keyword_only = [
            name for name in (spec.get("required_keyword_only") or []) if name not in keyword_names
        ]

        checked = {
            "call": call_name,
            "line": getattr(node, "lineno", None),
            "contract_qualname": spec["qualname"],
            "parent_qualname": spec.get("parent_qualname", ""),
            "signature": spec["signature"],
            "required_positional": required_positional,
            "supplied_positional": supplied_positional,
            "missing_keyword_only": required_keyword_only,
            "placeholder_literals": placeholder_literals,
        }
        checked_calls.append(checked)

        for placeholder in placeholder_literals:
            issues.append(
                VerificationIssue(
                    code="contract_call_uses_unrequested_literal_arg",
                    message=(
                        "В сгенерированном коде вызов "
                        f"{call_name} передает literal {placeholder['literal']} "
                        f"для обязательного аргумента {placeholder['argument']} "
                        f"production-контракта {spec['qualname']}. Сигнатура: {spec['signature']}. "
                        "Этот literal не указан в пользовательском запросе. Для repair: получи значение "
                        "аргумента из параметров нового symbol, локальных переменных или видимого проектного "
                        "контекста; если значения нет, добавь его в сигнатуру нового symbol вместо подстановки "
                        "заглушки."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

        if supplied_positional < required_positional:
            issues.append(
                VerificationIssue(
                    code="contract_call_missing_required_positional_args",
                    message=(
                        "В сгенерированном коде вызов "
                        f"{call_name} использует {supplied_positional} positional args, "
                        f"но production-контракт {spec['qualname']} требует минимум "
                        f"{required_positional}. Сигнатура: {spec['signature']}. "
                        "Для repair: исправь вызов, передав обязательные аргументы из доступного проектного контекста, "
                        "или используй другой подход, не нарушающий сигнатуру связанного production symbol."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

        if required_keyword_only:
            issues.append(
                VerificationIssue(
                    code="contract_call_missing_required_keyword_only_args",
                    message=(
                        "В сгенерированном коде вызов "
                        f"{call_name} не передает обязательные keyword-only args: "
                        f"{', '.join(required_keyword_only)}. Production-контракт: "
                        f"{spec['qualname']} с сигнатурой {spec['signature']}. "
                        "Для repair: исправь вызов, явно передав обязательные keyword-only аргументы "
                        "из доступного проектного контекста."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

    return issues, {
        "known_contract_call_specs": sorted(
            [
                {
                    "name": name,
                    "qualname": spec["qualname"],
                    "parent_qualname": spec.get("parent_qualname", ""),
                    "signature": spec["signature"],
                    "required_positional": spec["required_positional"],
                    "required_positional_names": spec.get("required_positional_names", []),
                    "required_keyword_only": spec["required_keyword_only"],
                    "return_annotation": spec.get("return_annotation", ""),
                }
                for name, spec in specs_by_name.items()
            ],
            key=lambda item: item["qualname"],
        ),
        "checked_calls": checked_calls,
        "skipped_calls": skipped_calls,
    }

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


def _is_dict_return_annotation(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    name = _annotation_name(annotation).rsplit(".", 1)[-1].lower()
    if name in {"dict", "dictionary", "mapping", "mutablemapping"}:
        return True
    try:
        rendered = ast.unparse(annotation).lower()
    except Exception:
        rendered = name
    return rendered.startswith("dict[") or rendered.startswith("typing.dict[")


def _return_annotation_is_dict_like(annotation: str) -> bool:
    normalized = str(annotation or "").strip().lower()
    if not normalized:
        return False
    return (
        normalized == "dict"
        or normalized.startswith("dict[")
        or normalized.startswith("typing.dict[")
        or normalized == "mapping"
        or normalized.startswith("mapping[")
    )


def _constructor_return_fields_from_source(source: str) -> dict[str, set[str]]:
    tree = _safe_parse(source)
    if tree is None:
        return {}

    result: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue

        type_name = ""
        if isinstance(value.func, ast.Name):
            type_name = value.func.id
        elif isinstance(value.func, ast.Attribute):
            type_name = _call_display_name(value.func).rsplit(".", 1)[-1]

        if not type_name:
            continue

        fields = {keyword.arg for keyword in value.keywords if keyword.arg}
        if fields:
            result.setdefault(type_name, set()).update(fields)

    return result


def _class_fields_from_source(source: str) -> dict[str, set[str]]:
    tree = _safe_parse(source)
    if tree is None:
        return {}

    result: dict[str, set[str]] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ClassDef):
            continue

        fields: set[str] = set()
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                fields.add(child.target.id)
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        fields.add(target.id)

        if fields:
            result[node.name] = fields

    return result


def _visible_return_fields_by_type(related_symbols: list[Any] | None) -> dict[str, set[str]]:
    fields_by_type: dict[str, set[str]] = {}

    for raw in related_symbols or []:
        item = raw if isinstance(raw, dict) else asdict(raw)
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        source = str(
            item.get("source_code")
            or item.get("source")
            or item.get("source_excerpt")
            or ""
        )

        if source:
            for type_name, fields in _constructor_return_fields_from_source(source).items():
                fields_by_type.setdefault(type_name, set()).update(fields)

            for type_name, fields in _class_fields_from_source(source).items():
                fields_by_type.setdefault(type_name, set()).update(fields)

        if kind == "class" and name and source:
            class_fields = _class_fields_from_source(source).get(name, set())
            if class_fields:
                fields_by_type.setdefault(name, set()).update(class_fields)

    return fields_by_type


def _result_attribute_name(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None


def _check_dict_return_shape(
    *,
    tree: ast.AST,
    related_symbols: list[Any] | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
    import_changes: list[dict[str, Any]] | None = None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if scan_tree is None:
        return [], {"checked_returns": [], "skipped_returns": []}

    available_names = _known_imported_names(tree, import_changes)
    specs_by_name = _contract_call_specs(related_symbols, available_names=available_names)

    issues: list[VerificationIssue] = []
    checked_returns: list[dict[str, Any]] = []
    skipped_returns: list[dict[str, Any]] = []

    for function_node in ast.walk(scan_tree):
        if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_dict_return_annotation(function_node.returns):
            continue

        assigned_contract_calls: dict[str, dict[str, Any]] = {}

        for node in ast.walk(function_node):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                called_name = _called_symbol_name(node.value.func)
                spec = specs_by_name.get(called_name or "")
                if spec is None:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned_contract_calls[target.id] = spec

        for node in ast.walk(function_node):
            if not isinstance(node, ast.Return) or node.value is None:
                continue

            value = node.value
            checked: dict[str, Any] = {
                "function": function_node.name,
                "line": getattr(node, "lineno", None),
                "return_expr": type(value).__name__,
            }

            spec: dict[str, Any] | None = None
            if isinstance(value, ast.Call):
                called_name = _called_symbol_name(value.func)
                spec = specs_by_name.get(called_name or "")
                if spec is not None:
                    checked["source"] = "direct_contract_call"
                    checked["contract_qualname"] = spec.get("qualname")
                    checked["contract_return_annotation"] = spec.get("return_annotation", "")
            elif isinstance(value, ast.Name):
                spec = assigned_contract_calls.get(value.id)
                if spec is not None:
                    checked["source"] = "assigned_contract_call"
                    checked["return_name"] = value.id
                    checked["contract_qualname"] = spec.get("qualname")
                    checked["contract_return_annotation"] = spec.get("return_annotation", "")
            elif isinstance(value, ast.Attribute) and value.attr == "__dict__":
                checked["source"] = "dunder_dict"
                checked_returns.append(checked)
                issues.append(
                    VerificationIssue(
                        code="dict_return_uses_dunder_dict",
                        message=(
                            f"Новый symbol {function_node.name} объявлен как возвращающий dict, "
                            "но возвращает `__dict__`. Это небезопасно для dataclass/slots/model объектов. "
                            "Для repair: сформируй dict явно по видимым полям/атрибутам или используй "
                            "явный project helper."
                        ),
                        severity="error",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )
                continue

            if spec is None:
                skipped_returns.append(checked)
                continue

            checked_returns.append(checked)
            contract_return = str(spec.get("return_annotation") or "")
            if not _return_annotation_is_dict_like(contract_return):
                issues.append(
                    VerificationIssue(
                        code="dict_return_annotation_returns_contract_object",
                        message=(
                            f"Новый symbol {function_node.name} объявлен как возвращающий dict, "
                            f"но возвращает результат production-контракта {spec.get('qualname')} "
                            f"с return annotation `{contract_return or 'unknown'}`. Для repair: "
                            "сформируй dict явно по видимым полям/атрибутам возвращаемого объекта "
                            "или используй явный project helper."
                        ),
                        severity="error",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )

    return issues, {"checked_returns": checked_returns, "skipped_returns": skipped_returns}


def _check_contract_result_field_usage(
    *,
    tree: ast.AST,
    related_symbols: list[Any] | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
    import_changes: list[dict[str, Any]] | None = None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if scan_tree is None:
        return [], {"visible_return_fields": {}, "checked_attributes": [], "skipped_attributes": []}

    available_names = _known_imported_names(tree, import_changes)
    specs_by_name = _contract_call_specs(related_symbols, available_names=available_names)
    fields_by_type = _visible_return_fields_by_type(related_symbols)

    issues: list[VerificationIssue] = []
    checked_attributes: list[dict[str, Any]] = []
    skipped_attributes: list[dict[str, Any]] = []

    for function_node in ast.walk(scan_tree):
        if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        assigned_contract_calls: dict[str, dict[str, Any]] = {}

        for node in ast.walk(function_node):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            called_name = _called_symbol_name(node.value.func)
            spec = specs_by_name.get(called_name or "")
            if spec is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned_contract_calls[target.id] = spec

        if not assigned_contract_calls:
            continue

        for node in ast.walk(function_node):
            attr = _result_attribute_name(node)
            if attr is None:
                continue

            variable_name, field_name = attr
            spec = assigned_contract_calls.get(variable_name)
            if spec is None:
                continue

            return_type = str(spec.get("return_annotation") or "").strip()
            return_type_name = return_type.rsplit(".", 1)[-1].split("[", 1)[0].strip()
            visible_fields = fields_by_type.get(return_type_name)

            checked = {
                "function": function_node.name,
                "variable": variable_name,
                "attribute": field_name,
                "line": getattr(node, "lineno", None),
                "contract_qualname": spec.get("qualname"),
                "return_annotation": return_type,
                "visible_fields": sorted(visible_fields or []),
            }

            if not visible_fields:
                skipped_attributes.append({**checked, "reason": "no_visible_fields_for_return_type"})
                continue

            checked_attributes.append(checked)
            if field_name not in visible_fields:
                issues.append(
                    VerificationIssue(
                        code="unknown_contract_result_attribute",
                        message=(
                            f"В сгенерированном коде используется поле {variable_name}.{field_name}, "
                            f"но для результата production-контракта {spec.get('qualname')} "
                            f"видимы только поля: {', '.join(sorted(visible_fields))}. "
                            "Для repair: сформируй dict только по видимым полям результата или используй "
                            "явный project helper."
                        ),
                        severity="error",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )

    return issues, {
        "visible_return_fields": {key: sorted(value) for key, value in fields_by_type.items()},
        "checked_attributes": checked_attributes,
        "skipped_attributes": skipped_attributes,
    }




def validate_patch_static_semantics(
    *,
    requested_operation: str,
    change_request: ChangeRequest,
    target_qualname: str,
    original_file_text: str,
    patched_file_text: str,
    changed_files: list[str],
    target_file: str,
    insert_scope: str | None = None,
    parent_qualname: str | None = None,
    related_symbols: list[Any] | None = None,
    import_changes: list[dict[str, Any]] | None = None,
    required_contracts: list[dict[str, Any]] | None = None,
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
    contract_scan_tree: ast.AST | None = None

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

    if requested_operation == "replace_symbol":
        if "." in target_qualname:
            parent_name_for_target = _class_name_from_qualname(target_qualname.rsplit(".", 1)[0])
            method_nodes = _class_method_nodes(after_tree, parent_name_for_target, {target_name})
            if method_nodes:
                contract_scan_tree = _module_from_nodes(method_nodes)
        if contract_scan_tree is None:
            nodes = _top_level_def_nodes(after_tree, {target_name})
            if nodes:
                contract_scan_tree = _module_from_nodes(nodes)

    if requested_operation == "insert_after_symbol":
        if insert_scope == "class_body":
            parent_name = _class_name_from_qualname(parent_qualname or target_qualname.rsplit('.', 1)[0])
            before_methods = _class_methods(before_tree, parent_name)
            after_methods = _class_methods(after_tree, parent_name)
            new_methods = sorted(after_methods - before_methods)
            anchor_name = target_name
            if anchor_name and anchor_name not in after_methods and anchor_name in before_methods:
                issues.append(
                    VerificationIssue(
                        code="method_anchor_missing_after_insert",
                        message="После insert_after_symbol anchor method отсутствует в классе.",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )
            if not new_methods:
                issues.append(
                    VerificationIssue(
                        code="generated_method_missing_after_insert",
                        message="После insert_after_symbol не найден новый метод в целевом классе.",
                        file_path=target_file,
                        symbol=parent_qualname,
                    )
                )
            top_level_new_functions = [name for name, kind in new_symbols.items() if kind == "function"]
            if top_level_new_functions:
                issues.append(
                    VerificationIssue(
                        code="generated_symbol_scope_mismatch",
                        message=(
                            "Ожидался метод внутри класса, но появился новый top-level function: "
                            + ", ".join(top_level_new_functions)
                        ),
                        file_path=target_file,
                        symbol=parent_qualname,
                    )
                )
            new_symbols = {name: "method" for name in new_methods}
            method_nodes = _class_method_nodes(after_tree, parent_name, set(new_methods))
            if method_nodes:
                contract_scan_tree = _module_from_nodes(method_nodes)
        else:
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
            top_level_nodes = _top_level_def_nodes(after_tree, set(new_symbols.keys()))
            if top_level_nodes:
                contract_scan_tree = _module_from_nodes(top_level_nodes)

    if _constraint_contains(change_request, "только одну новую функцию"):
        expected_kind = "method" if insert_scope == "class_body" else "function"
        new_functions = [name for name, kind in new_symbols.items() if kind == expected_kind]
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

    contract_issues, contract_details = _check_contract_call_signatures(
        tree=after_tree,
        scan_tree=contract_scan_tree,
        related_symbols=related_symbols,
        target_file=target_file,
        target_qualname=target_qualname,
        import_changes=import_changes,
        change_request=change_request,
    )
    issues.extend(contract_issues)

    injected_method_issues, injected_method_details = _check_unknown_injected_dependency_methods(
        owner_tree=after_tree,
        scan_tree=contract_scan_tree,
        related_symbols=related_symbols,
        target_file=target_file,
        target_qualname=target_qualname,
        parent_qualname=parent_qualname,
    )
    issues.extend(injected_method_issues)

    return_shape_issues, return_shape_details = _check_dict_return_shape(
        tree=after_tree,
        scan_tree=contract_scan_tree,
        related_symbols=related_symbols,
        target_file=target_file,
        target_qualname=target_qualname,
        import_changes=import_changes,
    )
    issues.extend(return_shape_issues)

    result_field_issues, result_field_details = _check_contract_result_field_usage(
        tree=after_tree,
        scan_tree=contract_scan_tree,
        related_symbols=related_symbols,
        target_file=target_file,
        target_qualname=target_qualname,
        import_changes=import_changes,
    )
    issues.extend(result_field_issues)

    required_contract_issues, required_contract_details = _check_required_contract_usage(
        scan_tree=contract_scan_tree,
        required_contracts=required_contracts,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(required_contract_issues)

    duplicate_symbols = find_duplicate_symbol_definitions(
        patched_file_text,
        _module_name_from_target_file(target_file),
        target_file,
    )
    for duplicate in duplicate_symbols:
        lines = duplicate.get("lines") or []
        line_text = ", ".join(str(item) for item in lines) if lines else "unknown"
        issues.append(
            VerificationIssue(
                code="duplicate_symbol_definition",
                message=(
                    f"В измененном файле найден дубликат symbol {duplicate['qualname']} "
                    f"({duplicate['kind']}) на строках {line_text}. "
                    "Для repair: сгенерируй новый symbol с уникальным именем, соответствующим исходному пользовательскому запросу; "
                    "не копируй и не вставляй повторно уже существующий symbol."
                ),
                severity="error",
                file_path=target_file,
                symbol=str(duplicate.get("qualname") or target_qualname),
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
            "insert_scope": insert_scope,
            "parent_qualname": parent_qualname,
            "new_symbols": new_symbols,
            "contract_call_signature_check": contract_details,
            "injected_dependency_method_check": injected_method_details,
            "dict_return_shape_check": return_shape_details,
            "contract_result_field_check": result_field_details,
            "required_contract_usage_check": required_contract_details,
            "duplicate_symbols": duplicate_symbols,
        },
    )




def _module_file_for_import(project_root: Path, module_name: str) -> Path | None:
    module_path = module_name.replace('.', '/')
    py_path = project_root / f'{module_path}.py'
    if py_path.exists():
        return py_path
    init_path = project_root / module_path / '__init__.py'
    if init_path.exists():
        return init_path
    return None


def _class_constructor_keyword_fields_from_source(source: str, class_name: str) -> set[str]:
    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return set()

    init_node = next(
        (
            child
            for child in class_node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == '__init__'
        ),
        None,
    )
    if init_node is not None:
        return {
            arg.arg
            for arg in [*init_node.args.posonlyargs, *init_node.args.args, *init_node.args.kwonlyargs]
            if arg.arg != 'self'
        }

    fields: set[str] = set()
    for child in class_node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            fields.add(child.target.id)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    fields.add(target.id)
    return fields


def _project_constructor_keyword_fields(
    project_root: Path,
    imported_names: dict[str, str],
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for imported_name, module_name in sorted(imported_names.items()):
        module_file = _module_file_for_import(project_root, module_name)
        if module_file is None:
            continue
        try:
            source = module_file.read_text(encoding='utf-8')
        except OSError:
            continue
        fields = _class_constructor_keyword_fields_from_source(source, imported_name)
        if fields:
            result[imported_name] = sorted(fields)
    return result


def _check_generated_test_constructor_keywords(
    *,
    tree: ast.AST,
    project_root: Path,
    imported_names: dict[str, str],
    test_file_path: Path,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    constructor_fields = _project_constructor_keyword_fields(project_root, imported_names)
    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []

    if not constructor_fields:
        return issues, {'constructor_fields': {}, 'checked_calls': []}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        class_name = node.func.id
        allowed_fields = set(constructor_fields.get(class_name) or [])
        if not allowed_fields:
            continue
        supplied_keywords = [kw.arg for kw in node.keywords if kw.arg]
        if not supplied_keywords:
            continue
        unknown = sorted({name for name in supplied_keywords if name not in allowed_fields})
        checked_calls.append({
            'class_name': class_name,
            'module_name': imported_names.get(class_name, ''),
            'line': getattr(node, 'lineno', None),
            'allowed_keywords': sorted(allowed_fields),
            'supplied_keywords': supplied_keywords,
            'unknown_keywords': unknown,
        })
        for keyword in unknown:
            issues.append(
                VerificationIssue(
                    code='generated_test_uses_unknown_constructor_keyword',
                    message=(
                        f'Generated test передает неизвестный keyword `{keyword}` в конструктор {class_name}. '
                        f'Видимые аргументы конструктора/поля: {", ".join(sorted(allowed_fields))}.'
                    ),
                    file_path=str(test_file_path),
                    symbol=class_name,
                )
            )

    return issues, {
        'constructor_fields': constructor_fields,
        'checked_calls': checked_calls,
    }
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

    pytest_mock_usage = _find_pytest_mock_usage(tree)
    for usage in pytest_mock_usage:
        issues.append(
            VerificationIssue(
                code=str(usage.get("code") or "generated_test_uses_mocker_fixture"),
                message=str(usage.get("message") or "Generated test uses pytest-mock/mocker."),
                file_path=str(test_file_path),
                symbol=str(usage.get("name") or ""),
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

    imported_names = _collect_project_imported_names(tree, project_root)
    constructor_keyword_issues, constructor_keyword_details = _check_generated_test_constructor_keywords(
        tree=tree,
        project_root=project_root,
        imported_names=imported_names,
        test_file_path=test_file_path,
    )
    issues.extend(constructor_keyword_issues)
    called_names = _collect_called_names(tree)
    asserted_names = _collect_assert_names(tree)
    unresolved_names = _find_unresolved_names(tree)

    for name in unresolved_names:
        issues.append(
            VerificationIssue(
                code="generated_test_uses_unresolved_name",
                message=(
                    "Generated test использует имя, которое не импортировано и не определено "
                    f"в тестовом файле: {name}. Нужно явно импортировать project symbol "
                    "из видимого проектного контекста или определить локальный fake/stub."
                ),
                file_path=str(test_file_path),
                symbol=name,
            )
        )

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
            "pytest_mock_usage": pytest_mock_usage,
            "imported_project_names": imported_names,
            "unresolved_names": unresolved_names,
            "called_names": sorted(called_names),
            "asserted_names": sorted(asserted_names),
            "referenced_names": sorted(referenced_names),
            "used_names": sorted(used_names),
            "constructor_keyword_check": constructor_keyword_details,
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