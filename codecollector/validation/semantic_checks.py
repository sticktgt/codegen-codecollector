from __future__ import annotations

import ast
from dataclasses import asdict
from pathlib import Path
import re
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



_BUILTIN_ANNOTATION_NAMES = {
    "None", "True", "False",
    "str", "int", "float", "bool", "bytes", "dict", "list", "set", "tuple",
    "object", "type", "Exception", "BaseException",
}
_TYPING_ANNOTATION_NAMES = {
    "Any", "Optional", "Union", "Literal", "Callable", "Iterable", "Iterator", "Sequence", "Mapping",
    "MutableMapping", "List", "Dict", "Set", "Tuple", "Type", "ClassVar", "Protocol",
}

def _module_available_names(tree: ast.AST | None) -> set[str]:
    names: set[str] = set(_BUILTIN_ANNOTATION_NAMES)
    if tree is None:
        return names
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.asname or alias.name.split('.', 1)[0]).strip()
                if root:
                    names.add(root)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == '*':
                    # Star imports are intentionally treated as unknown surface.
                    continue
                name = (alias.asname or alias.name).strip()
                if name:
                    names.add(name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names

def _annotation_name_nodes(annotation: ast.AST | None) -> list[ast.Name]:
    if annotation is None:
        return []
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        # String annotations are not evaluated at class/function definition time.
        return []
    return [node for node in ast.walk(annotation) if isinstance(node, ast.Name)]

def _default_expression_name_nodes(expr: ast.AST | None) -> list[ast.Name]:
    if expr is None:
        return []
    return [node for node in ast.walk(expr) if isinstance(node, ast.Name)]

def _check_unresolved_annotation_names(
    *,
    module_tree: ast.AST | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if module_tree is None or scan_tree is None:
        return [], {"available_names": [], "checked_names": [], "unknown_names": [], "skipped": True}

    available_names = _module_available_names(module_tree)
    checked: list[dict[str, Any]] = []
    unknown_seen: dict[tuple[str, int | None, str], ast.Name] = {}
    parent_map: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(scan_tree)
        for child in ast.iter_child_nodes(parent)
    }

    def check_name(node: ast.Name, usage: str) -> None:
        name = str(node.id or "")
        if not name or name in available_names:
            return
        # PEP 585 builtins should be preferred, but old typing aliases are common.
        # If they are used, they still need to be imported unless annotations are strings.
        key = (name, getattr(node, "lineno", None), usage)
        unknown_seen[key] = node
        checked.append({"name": name, "usage": usage, "line": getattr(node, "lineno", None), "available": False})

    for node in ast.walk(scan_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                for name_node in _annotation_name_nodes(arg.annotation):
                    check_name(name_node, f"argument_annotation:{node.name}.{arg.arg}")
            if node.returns is not None:
                for name_node in _annotation_name_nodes(node.returns):
                    check_name(name_node, f"return_annotation:{node.name}")
        elif isinstance(node, ast.AnnAssign):
            for name_node in _annotation_name_nodes(node.annotation):
                target_name = ast.unparse(node.target) if hasattr(ast, 'unparse') else ''
                check_name(name_node, f"variable_annotation:{target_name}")
            # Only class-level annotated assignment values are evaluated at import time.
            # Local annotated assignments inside generated functions may reference local
            # variables in the value expression, so scanning the value as a class-level
            # expression produces false positives such as ``data`` or ``note_id``.
            if isinstance(parent_map.get(node), ast.ClassDef):
                for name_node in _default_expression_name_nodes(node.value):
                    check_name(name_node, "class_default_expression")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                for name_node in _default_expression_name_nodes(base):
                    check_name(name_node, f"class_base:{node.name}")
            for decorator in node.decorator_list:
                for name_node in _default_expression_name_nodes(decorator):
                    check_name(name_node, f"class_decorator:{node.name}")

    issues: list[VerificationIssue] = []
    for (name, line, usage), node in sorted(unknown_seen.items(), key=lambda item: (str(item[0][0]), item[0][1] or 0, str(item[0][2]))):
        issues.append(
            VerificationIssue(
                code="unknown_annotation_name",
                message=(
                    f"Generated production code uses `{name}` in annotation or class-level expression, "
                    f"but this name is not imported or defined in target module. "
                    "For repair: add the required import or use an already available built-in annotation."
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )

    details = {
        "available_names": sorted(available_names),
        "checked_names": checked,
        "unknown_names": [issue.message.split('`', 2)[1] for issue in issues if '`' in issue.message],
        "skipped": False,
    }
    return issues, details


def _self_attribute_type_map(tree: ast.AST | None, class_name: str) -> dict[str, str]:
    """Infer simple ``self.<attr>`` types from visible class assignments.

    The map is intentionally conservative: it uses constructor argument
    annotations and direct constructor calls visible in the class body. This is
    enough to distinguish valid accesses like ``self.path_builder.build_path``
    from invented aliases like ``self._path_builder.build_path``.
    """
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

    arg_types: dict[str, str] = {}
    if init_node is not None:
        arg_types = {
            arg.arg: _annotation_name(arg.annotation).rsplit(".", 1)[-1]
            for arg in [*init_node.args.posonlyargs, *init_node.args.args, *init_node.args.kwonlyargs]
            if arg.arg != "self"
        }

    def infer_value_type(value: ast.AST) -> str:
        if isinstance(value, ast.Name):
            return arg_types.get(value.id, "")
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name):
                return func.id
            if isinstance(func, ast.Attribute):
                return func.attr
        return ""

    for node in ast.walk(class_node):
        if isinstance(node, ast.Assign):
            value_type = infer_value_type(node.value)
            if not value_type:
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    result[target.attr] = value_type
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
            value_type = _annotation_name(node.annotation).rsplit(".", 1)[-1]
            if value_type:
                result[target.attr] = value_type
            elif node.value is not None:
                value_type = infer_value_type(node.value)
                if value_type:
                    result[target.attr] = value_type

    return result


def _module_level_names(tree: ast.AST | None) -> set[str]:
    """Return names defined at module level in the target file."""
    if tree is None:
        return set()
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".", 1)[0]
                if imported_name:
                    names.add(imported_name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _self_attribute_names(tree: ast.AST | None, class_name: str) -> set[str]:
    """Return visible instance attributes for a class.

    Includes assignments to ``self.<attr>`` and attributes documented in the
    class docstring. This keeps the check structural while avoiding project-
    specific names.
    """
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return set()

    names: set[str] = set()
    for node in ast.walk(class_node):
        target: ast.AST | None = None
        if isinstance(node, ast.Assign):
            for candidate in node.targets:
                if (
                    isinstance(candidate, ast.Attribute)
                    and isinstance(candidate.value, ast.Name)
                    and candidate.value.id == "self"
                ):
                    names.add(candidate.attr)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)

    docstring = ast.get_docstring(class_node) or ""
    for line in docstring.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("-", "*")):
            continue
        token = stripped.split(":", 1)[0].split("(", 1)[0].strip()
        if token.isidentifier():
            names.add(token)

    return names



def _simple_return_annotation_name(annotation: ast.AST | None) -> str:
    """Return a simple return annotation name if it is safe to use for local type inference."""
    if annotation is None:
        return ""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        value = annotation.value.strip().strip("'\"")
        if value.isidentifier():
            return value
        if "." in value:
            tail = value.rsplit(".", 1)[-1]
            return tail if tail.isidentifier() else ""
    # Keep the first step deliberately conservative: Optional[Note], list[Note]
    # and other compound annotations are not inferred here.
    return ""


def _same_class_method_return_type_map(
    tree: ast.AST | None,
    class_name: str,
    allowed_type_names: set[str] | None = None,
) -> dict[str, str]:
    """Infer direct ``self.method(...)`` return types from same-class method annotations.

    The inference is intentionally narrow and safe: only methods declared in the
    same visible class and only simple return annotations are used. If
    ``allowed_type_names`` is provided, the returned type must be in that set.
    """
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return {}

    result: dict[str, str] = {}
    allowed = set(allowed_type_names or set())
    for child in class_node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        type_name = _simple_return_annotation_name(child.returns)
        if not type_name:
            continue
        if allowed and type_name not in allowed:
            continue
        result[child.name] = type_name
    return result


def _direct_self_method_call_return_type(expr: ast.AST, same_class_method_return_types: dict[str, str]) -> str:
    """Return type for simple ``self.method(...)`` calls if explicitly known."""
    if not isinstance(expr, ast.Call):
        return ""
    func = expr.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    ):
        return ""
    return same_class_method_return_types.get(str(func.attr or ""), "")


def _check_unknown_self_attribute_usage(
    *,
    owner_tree: ast.AST | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
    parent_qualname: str | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if owner_tree is None or scan_tree is None:
        return [], {"checked_attributes": [], "unknown_attributes": [], "skipped": True}

    parent_class_name = _class_name_from_qualname(parent_qualname or target_qualname.rsplit(".", 1)[0])
    if not parent_class_name:
        return [], {"checked_attributes": [], "unknown_attributes": [], "skipped": True, "reason": "no_parent_class"}

    known_attributes = _self_attribute_names(owner_tree, parent_class_name)
    known_methods = _class_methods(owner_tree, parent_class_name)
    module_level_names = _module_level_names(owner_tree)
    checked: list[dict[str, Any]] = []
    checked_methods: list[dict[str, Any]] = []
    unknown: dict[tuple[str, int | None], ast.Attribute] = {}
    unknown_methods: dict[tuple[str, int | None], ast.Attribute] = {}

    def _name_tokens(name: str) -> set[str]:
        cleaned = name.strip("_").replace("-", "_")
        return {part for part in cleaned.split("_") if part}

    def suggested_replacements(attr: str, *, candidates: set[str] | None = None) -> list[str]:
        pool = candidates if candidates is not None else (known_attributes | known_methods)
        suggestions: list[str] = []
        public_attr = attr.lstrip("_")
        if public_attr and public_attr in pool:
            suggestions.append(public_attr)

        attr_tokens = _name_tokens(public_attr or attr)
        if attr_tokens:
            scored: list[tuple[float, str]] = []
            for candidate in pool:
                if candidate in suggestions:
                    continue
                candidate_tokens = _name_tokens(candidate)
                if not candidate_tokens:
                    continue
                overlap = len(attr_tokens & candidate_tokens)
                if not overlap:
                    continue
                coverage = overlap / max(len(attr_tokens), 1)
                # Prefer candidates that cover every requested token, e.g.
                # _find_file_by_id -> find_note_file_by_id.
                if coverage >= 0.75:
                    scored.append((coverage + (0.1 if candidate.startswith(public_attr) else 0.0), candidate))
            for _score, candidate in sorted(scored, key=lambda item: (-item[0], item[1])):
                suggestions.append(candidate)
                if len(suggestions) >= 3:
                    break
        return suggestions

    for node in ast.walk(scan_tree):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        attr = str(node.attr or "")
        if not attr:
            continue
        checked.append({"attribute": attr, "line": getattr(node, "lineno", None)})
        if attr in known_attributes or attr in known_methods:
            continue

        is_call = isinstance(getattr(node, "ctx", None), ast.Load) and isinstance(getattr(node, "parent", None), ast.Call)
        # parent links are not available on the parsed tree, so detect calls with a second pass below.
        if attr.startswith("_") and attr.lstrip("_") in known_attributes:
            unknown[(attr, getattr(node, "lineno", None))] = node
        elif attr in module_level_names:
            unknown[(attr, getattr(node, "lineno", None))] = node

    for node in ast.walk(scan_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            continue
        method = str(func.attr or "")
        if not method or method.startswith("__"):
            continue
        checked_methods.append({"method": method, "line": getattr(func, "lineno", None)})
        if method in known_methods or method in known_attributes:
            continue
        unknown_methods[(method, getattr(func, "lineno", None))] = func

    issues: list[VerificationIssue] = []
    unknown_details: list[dict[str, Any]] = []
    for (attr, line), _node in sorted(unknown.items(), key=lambda item: (item[0][0], item[0][1] or 0)):
        is_module_level_name = attr in module_level_names and attr not in known_attributes and attr not in known_methods
        suggestions = [attr] if is_module_level_name else suggested_replacements(attr)
        if is_module_level_name:
            suggestion_text = f" Возможная замена: `{attr}` без `self`."
            repair_hint = (
                "For repair: remove `self.` before this name and use the visible module-level name directly, "
                "or use another visible instance attribute or method from the class context."
            )
        else:
            suggestion_text = f" Возможная замена: self.{suggestions[0]}." if suggestions else ""
            repair_hint = (
                "For repair: remove every usage of this unknown self-attribute and use only visible instance "
                "attributes or methods from the class context. Do not create a new alias/underscore field unless "
                "the target is the initializer and the user explicitly requested a state change."
            )
        issues.append(
            VerificationIssue(
                code="unknown_self_attribute",
                message=(
                    f"Generated production code uses `self.{attr}`, but this attribute is not visible "
                    f"as an existing attribute or method of {parent_class_name}."
                    f" Visible attributes: {', '.join(sorted(known_attributes)) or '<none>'}."
                    f" Visible methods: {', '.join(sorted(known_methods)) or '<none>'}."
                    f" Module-level names: {', '.join(sorted(module_level_names)) or '<none>'}."
                    f"{suggestion_text} "
                    f"{repair_hint}"
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )
        detail = {"attribute": attr, "line": line, "suggested_replacements": suggestions}
        if is_module_level_name:
            detail["replacement_kind"] = "module_level_name"
            detail["suggested_expression"] = attr
        unknown_details.append(detail)

    unknown_method_details: list[dict[str, Any]] = []
    for (method, line), _node in sorted(unknown_methods.items(), key=lambda item: (item[0][0], item[0][1] or 0)):
        suggestions = suggested_replacements(method, candidates=known_methods)
        suggestion_text = f" Возможная замена: self.{suggestions[0]}(...)." if suggestions else ""
        issues.append(
            VerificationIssue(
                code="unknown_self_method",
                message=(
                    f"Generated production code calls `self.{method}(...)`, but this method is not visible "
                    f"as an existing method of {parent_class_name}."
                    f" Visible methods: {', '.join(sorted(known_methods)) or '<none>'}."
                    f" Visible attributes: {', '.join(sorted(known_attributes)) or '<none>'}."
                    f"{suggestion_text} "
                    "For repair: remove every call of this unknown self-method and use only visible methods from "
                    "the class context. Do not invent a private helper method inside another method; add a new "
                    "method only when the requested operation is insert_after_symbol for that method."
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )
        )
        unknown_method_details.append({"method": method, "line": line, "suggested_replacements": suggestions})

    return issues, {
        "parent_class": parent_class_name,
        "known_attributes": sorted(known_attributes),
        "known_methods": sorted(known_methods),
        "module_level_names": sorted(module_level_names),
        "checked_attributes": checked,
        "unknown_attributes": unknown_details,
        "checked_methods": checked_methods,
        "unknown_methods": unknown_method_details,
        "skipped": False,
    }



def _same_class_method_call_specs(owner_tree: ast.AST | None, class_name: str) -> dict[str, dict[str, Any]]:
    """Return conservative call specs for methods declared in the same class."""
    class_node = _class_node(owner_tree, class_name)
    if class_node is None:
        return {}

    specs: dict[str, dict[str, Any]] = {}
    for child in class_node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        args = child.args
        if args.vararg is not None or args.kwarg is not None:
            # Dynamic signatures are intentionally skipped to avoid false positives.
            continue

        positional_args = list(args.posonlyargs) + list(args.args)
        if positional_args and positional_args[0].arg in {"self", "cls"}:
            positional_args = positional_args[1:]

        defaults_count = len(args.defaults or [])
        required_positional_count = max(0, len(positional_args) - defaults_count)
        required_positional_names = [arg.arg for arg in positional_args[:required_positional_count]]
        positional_names = [arg.arg for arg in positional_args]
        keyword_only_names = [arg.arg for arg in args.kwonlyargs]
        required_keyword_only = [
            arg.arg
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False)
            if default is None
        ]
        allowed_keyword_names = set(positional_names) | set(keyword_only_names)

        specs[child.name] = {
            "name": child.name,
            "required_positional_names": required_positional_names,
            "positional_names": positional_names,
            "required_keyword_only": required_keyword_only,
            "allowed_keyword_names": sorted(allowed_keyword_names),
            "signature": _function_signature_for_diagnostic(child),
        }
    return specs


def _function_signature_for_diagnostic(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a compact readable signature from a function node."""
    try:
        args_text = ast.unparse(node.args)
    except Exception:
        args_text = "..."
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = ""
    if node.returns is not None:
        try:
            returns = f" -> {ast.unparse(node.returns)}"
        except Exception:
            returns = ""
    return f"{prefix} {node.name}({args_text}){returns}:"


def _check_same_class_method_call_signatures(
    *,
    owner_tree: ast.AST | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
    parent_qualname: str | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    """Validate simple ``self.method(...)`` calls against visible same-class signatures.

    This check is deliberately narrow: it verifies only argument count and keyword
    names for methods declared in the same visible class. It does not perform
    type checking and skips methods with *args/**kwargs.
    """
    if owner_tree is None or scan_tree is None:
        return [], {"known_methods": {}, "checked_calls": [], "skipped_calls": [], "skipped": True}

    parent_class_name = _class_name_from_qualname(parent_qualname or target_qualname.rsplit(".", 1)[0])
    if not parent_class_name:
        return [], {"known_methods": {}, "checked_calls": [], "skipped_calls": [], "skipped": True, "reason": "no_parent_class"}

    specs = _same_class_method_call_specs(owner_tree, parent_class_name)
    if not specs:
        return [], {"known_methods": {}, "checked_calls": [], "skipped_calls": [], "skipped": True, "reason": "no_visible_same_class_methods"}

    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []
    skipped_calls: list[dict[str, Any]] = []

    for node in ast.walk(scan_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            continue

        method_name = str(func.attr or "")
        spec = specs.get(method_name)
        if spec is None:
            continue

        call_name = _call_display_name(func)
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            skipped_calls.append({"call": call_name, "reason": "star_args", "line": getattr(node, "lineno", None)})
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            skipped_calls.append({"call": call_name, "reason": "kwargs_unpack", "line": getattr(node, "lineno", None)})
            continue

        positional_names = list(spec.get("positional_names") or [])
        required_positional_names = list(spec.get("required_positional_names") or [])
        required_keyword_only = list(spec.get("required_keyword_only") or [])
        allowed_keyword_names = set(spec.get("allowed_keyword_names") or [])
        keyword_names = {str(keyword.arg) for keyword in node.keywords if keyword.arg}
        supplied_positional_count = len(node.args)
        satisfied_by_position = set(positional_names[:supplied_positional_count])
        missing_required_positional = [
            name for name in required_positional_names
            if name not in satisfied_by_position and name not in keyword_names
        ]
        unknown_keywords = sorted(name for name in keyword_names if name not in allowed_keyword_names)
        missing_required_keyword_only = [name for name in required_keyword_only if name not in keyword_names]

        checked_calls.append(
            {
                "call": call_name,
                "method": method_name,
                "line": getattr(node, "lineno", None),
                "signature": spec.get("signature"),
                "supplied_positional": supplied_positional_count,
                "keyword_names": sorted(keyword_names),
                "required_positional_names": required_positional_names,
                "missing_required_positional": missing_required_positional,
                "unknown_keywords": unknown_keywords,
                "required_keyword_only": required_keyword_only,
                "missing_required_keyword_only": missing_required_keyword_only,
            }
        )

        if missing_required_positional:
            issues.append(
                VerificationIssue(
                    code="self_method_call_missing_required_args",
                    message=(
                        f"В production-коде вызов {call_name} не передает обязательные аргументы метода "
                        f"{parent_class_name}.{method_name}: {', '.join(missing_required_positional)}. "
                        f"Сигнатура: {spec.get('signature')}. Для repair: исправь вызов по видимой сигнатуре метода того же класса."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if unknown_keywords:
            issues.append(
                VerificationIssue(
                    code="self_method_call_unknown_keyword_arg",
                    message=(
                        f"В production-коде вызов {call_name} передает неизвестные keyword-аргументы метода "
                        f"{parent_class_name}.{method_name}: {', '.join(unknown_keywords)}. "
                        f"Сигнатура: {spec.get('signature')}. Для repair: используй только параметры из видимой сигнатуры метода того же класса."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if missing_required_keyword_only:
            issues.append(
                VerificationIssue(
                    code="self_method_call_missing_required_keyword_only_args",
                    message=(
                        f"В production-коде вызов {call_name} не передает обязательные keyword-only аргументы метода "
                        f"{parent_class_name}.{method_name}: {', '.join(missing_required_keyword_only)}. "
                        f"Сигнатура: {spec.get('signature')}. Для repair: исправь вызов по видимой сигнатуре метода того же класса."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

    return issues, {
        "parent_class": parent_class_name,
        "known_methods": specs,
        "checked_calls": checked_calls,
        "skipped_calls": skipped_calls,
        "skipped": False,
    }


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
            # Be conservative for dynamic attributes, but block invented private aliases
            # when the public counterpart is visible in the class, e.g.
            # self._path_builder while self.path_builder exists.
            if attr_name.startswith("_") and attr_name.lstrip("_") in injected_types:
                replacement = attr_name.lstrip("_")
                issues.append(
                    VerificationIssue(
                        code="unknown_injected_dependency_attribute",
                        message=(
                            f"В сгенерированном коде вызов {_call_display_name(node.func)} использует "
                            f"зависимость {access_path}, но такой self-attribute или его тип не виден "
                            f"в классе. Видимый атрибут с тем же смыслом: self.{replacement}. "
                            "Для repair: полностью убери обращение к неизвестному атрибуту и используй только "
                            "существующие атрибуты класса; не придумывай новые alias/underscore-поля."
                        ),
                        severity="error",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
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


_PATH_LIKE_TYPE_NAMES = {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"}


def _related_class_field_type_map(related_symbols: list[Any] | None) -> dict[str, dict[str, str]]:
    """Infer simple field type annotations for visible related classes.

    This intentionally handles only local, visible facts: class annotations,
    constructor argument annotations and assignments like ``self.field = arg``
    or ``self.field = SomeType(...)``. It is used for conservative static
    safety checks around generated code that applies operations to visible
    values or restores project models from serialized data.
    """
    result: dict[str, dict[str, str]] = {}

    for raw in related_symbols or []:
        item = raw if isinstance(raw, dict) else asdict(raw)
        if str(item.get("kind") or "") != "class":
            continue

        source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
        tree = _safe_parse(source)
        class_name = str(item.get("name") or item.get("qualname", "").rsplit(".", 1)[-1])
        class_node = _class_node(tree, class_name)
        if class_node is None:
            continue

        field_types: dict[str, str] = {}

        for child in class_node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                type_name = _annotation_name(child.annotation).rsplit(".", 1)[-1]
                if type_name:
                    field_types[child.target.id] = type_name

        init_node = next(
            (
                child
                for child in class_node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__"
            ),
            None,
        )
        arg_types: dict[str, str] = {}
        if init_node is not None:
            for arg in [*init_node.args.posonlyargs, *init_node.args.args, *init_node.args.kwonlyargs]:
                if arg.arg == "self":
                    continue
                type_name = _annotation_name(arg.annotation).rsplit(".", 1)[-1]
                if type_name:
                    arg_types[arg.arg] = type_name

            for node in ast.walk(init_node):
                if isinstance(node, ast.Assign):
                    value_type = ""
                    if isinstance(node.value, ast.Name):
                        value_type = arg_types.get(node.value.id, "")
                    elif isinstance(node.value, ast.Call):
                        if isinstance(node.value.func, ast.Name):
                            value_type = node.value.func.id
                        elif isinstance(node.value.func, ast.Attribute):
                            value_type = node.value.func.attr
                    if not value_type:
                        continue
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            field_types[target.attr] = value_type
                elif isinstance(node, ast.AnnAssign):
                    target = node.target
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        continue
                    type_name = _annotation_name(node.annotation).rsplit(".", 1)[-1]
                    if type_name:
                        field_types[target.attr] = type_name

        if field_types:
            result[class_name] = field_types

    return result



def _field_has_default_value(value: ast.AST | None) -> bool:
    if value is None:
        return False
    # Any explicit class/dataclass default means the caller may omit the field.
    # ``None`` is also a default, but whether explicit None is valid depends on
    # the annotation and is checked separately.
    return True


def _related_class_default_field_map(related_symbols: list[Any] | None) -> dict[str, set[str]]:
    """Infer fields that have visible defaults in related project classes."""
    result: dict[str, set[str]] = {}

    for raw in related_symbols or []:
        item = raw if isinstance(raw, dict) else asdict(raw)
        if str(item.get("kind") or "") != "class":
            continue

        source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
        tree = _safe_parse(source)
        class_name = str(item.get("name") or item.get("qualname", "").rsplit(".", 1)[-1])
        class_node = _class_node(tree, class_name)
        if class_node is None:
            continue

        defaults: set[str] = set()

        for child in class_node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                if _field_has_default_value(child.value):
                    defaults.add(child.target.id)
            elif isinstance(child, ast.Assign):
                if not _field_has_default_value(child.value):
                    continue
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        defaults.add(target.id)

        init_node = next(
            (
                child
                for child in class_node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__"
            ),
            None,
        )
        if init_node is not None:
            args = [arg.arg for arg in [*init_node.args.posonlyargs, *init_node.args.args] if arg.arg != "self"]
            positional_defaults = list(init_node.args.defaults or [])
            if positional_defaults:
                for arg_name in args[-len(positional_defaults):]:
                    defaults.add(arg_name)
            for arg, default in zip(init_node.args.kwonlyargs, init_node.args.kw_defaults):
                if arg.arg != "self" and default is not None:
                    defaults.add(arg.arg)

        if defaults:
            result[class_name] = defaults

    return result


def _type_allows_none(type_name: str) -> bool:
    value = str(type_name or "")
    if not value:
        return False
    return "None" in value or value.startswith("Optional")


def _is_structured_model_field_type(type_name: str) -> bool:
    normalized = _normalized_type_name(type_name)
    if not normalized or _type_allows_none(type_name):
        return False
    return normalized not in {"str", "int", "float", "bool", "dict", "list", "set", "tuple", "bytes", "Any", "object"}


def _expr_is_none_literal(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


def _expr_can_be_none(expr: ast.AST, local_maybe_none: set[str]) -> bool:
    if _expr_is_none_literal(expr):
        return True
    if isinstance(expr, ast.Name):
        return expr.id in local_maybe_none
    if isinstance(expr, ast.IfExp):
        return _expr_can_be_none(expr.body, local_maybe_none) or _expr_can_be_none(expr.orelse, local_maybe_none)
    if isinstance(expr, ast.BoolOp):
        return any(_expr_can_be_none(value, local_maybe_none) for value in expr.values)
    return False

def _nested_self_attribute_path(expr: ast.AST) -> list[str]:
    parts: list[str] = []
    node = expr
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "self":
        return ["self", *reversed(parts)]
    return []


def _check_incompatible_visible_type_operator_usage(
    *,
    owner_tree: ast.AST | None,
    scan_tree: ast.AST | None,
    related_symbols: list[Any] | None,
    target_file: str,
    target_qualname: str,
    parent_qualname: str | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    """Block operations that contradict visible dependency field types.

    The check is intentionally narrow: it only inspects operands of the
    ``/`` operator in expressions that use nested ``self.<dependency>.<field>``
    values. It reports a problem only when the dependency field type is visible
    and that visible type is not compatible with the operator.
    """
    if owner_tree is None or scan_tree is None:
        return [], {"checked_operands": [], "issues": [], "skipped": True}

    parent_class_name = _class_name_from_qualname(parent_qualname or target_qualname.rsplit(".", 1)[0])
    injected_types = _self_attribute_type_map(owner_tree, parent_class_name)
    field_types_by_class = _related_class_field_type_map(related_symbols)

    checked: list[dict[str, Any]] = []
    issues_by_key: dict[tuple[str, str, str, int | None], VerificationIssue] = {}

    def inspect_operand(expr: ast.AST, binop_node: ast.BinOp) -> None:
        parts = _nested_self_attribute_path(expr)
        if len(parts) < 3:
            return
        dependency_attr = parts[1]
        field_name = parts[2]
        dependency_type = injected_types.get(dependency_attr, "")
        if not dependency_type:
            return
        field_type = (field_types_by_class.get(dependency_type) or {}).get(field_name, "")
        item = {
            "expression": ".".join(parts[:3]),
            "dependency_attribute": dependency_attr,
            "dependency_type": dependency_type,
            "field": field_name,
            "field_type": field_type,
            "line": getattr(expr, "lineno", getattr(binop_node, "lineno", None)),
        }
        checked.append(item)
        if field_type and field_type not in _PATH_LIKE_TYPE_NAMES:
            key = (dependency_attr, dependency_type, field_name, item["line"])
            issues_by_key[key] = VerificationIssue(
                code="incompatible_operator_for_visible_type",
                message=(
                    f"В сгенерированном production-коде значение `self.{dependency_attr}.{field_name}` "
                    f"используется в выражении с оператором `/`, но из видимого контекста поле `{field_name}` "
                    f"у типа {dependency_type} имеет тип {field_type}. "
                    "Для repair: не используй значение как объект другого типа; выбери видимый атрибут текущего класса, "
                    "видимый метод вспомогательного объекта или явно видимый проектный контракт, подходящий для этой операции."
                ),
                severity="error",
                file_path=target_file,
                symbol=target_qualname,
            )

    for node in ast.walk(scan_tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        inspect_operand(node.left, node)
        inspect_operand(node.right, node)

    issues = [issue for _key, issue in sorted(issues_by_key.items(), key=lambda item: item[0])]
    return issues, {
        "injected_attribute_types": injected_types,
        "related_class_field_types": field_types_by_class,
        "checked_operands": checked,
        "issues": [issue.code for issue in issues],
        "skipped": False,
    }



def _class_name_from_qualname(value: str | None) -> str:
    return str(value or '').strip().rsplit('.', 1)[-1]


def _constraint_contains(change_request: ChangeRequest, needle: str) -> bool:
    needle_norm = needle.strip().lower()
    return any(needle_norm in str(item).lower() for item in (change_request.constraints or []))


def _change_request_full_text(change_request: ChangeRequest) -> str:
    parts = [
        str(change_request.title or ""),
        str(change_request.description or ""),
        *[str(item or "") for item in (change_request.constraints or [])],
        *[str(item or "") for item in (change_request.notes or [])],
    ]
    return "\n".join(part for part in parts if part).lower()


def _requested_result_model_names(change_request: ChangeRequest, model_surfaces: list[dict[str, Any]] | None) -> list[str]:
    """Deprecated: do not infer required result objects from free-form CR text.

    Free-form wording is too ambiguous for deterministic validation.
    Required result objects must come from a future structured verification
    contract produced by analyze/planner and confirmed against project index.
    """
    return []


def _annotation_mentions_dict(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name):
        return annotation.id == "dict"
    if isinstance(annotation, ast.Subscript):
        return _annotation_mentions_dict(annotation.value) or _annotation_mentions_dict(annotation.slice)
    if isinstance(annotation, ast.Tuple):
        return any(_annotation_mentions_dict(elt) for elt in annotation.elts)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_mentions_dict(annotation.left) or _annotation_mentions_dict(annotation.right)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return "dict" in annotation.value
    if isinstance(annotation, ast.Attribute):
        return annotation.attr == "dict"
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _check_requested_result_model_usage(
    *,
    scan_tree: ast.AST | None,
    change_request: ChangeRequest,
    model_surfaces: list[dict[str, Any]] | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    """Skip request-text-driven result model validation.

    This hook is intentionally inert until codecollector has a structured
    verification contract from analyze/planner. The semantic checker should not
    reinterpret natural-language CR text because phrases that preserve existing
    contracts (for example "existing method continues returning List[Note]")
    can be confused with requirements to construct that model.
    """
    return [], {
        "requested_models": [],
        "forbids_dict": False,
        "constructed_models": [],
        "dict_literals": 0,
        "dict_return_annotations": 0,
        "skipped": True,
        "skip_reason": "structured_verification_contract_not_available",
    }


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


def _target_relative_parts(target_file: str, target_qualname: str) -> list[str]:
    module_name = _module_name_from_target_file(target_file)
    prefix = f"{module_name}."
    if target_qualname == module_name:
        return []
    if target_qualname.startswith(prefix):
        return [part for part in target_qualname[len(prefix):].split(".") if part]
    return [part for part in target_qualname.split(".") if part]


def _target_is_class_method(target_file: str, target_qualname: str) -> bool:
    return len(_target_relative_parts(target_file, target_qualname)) == 2


def _target_qualname_exists_in_tree(tree: ast.AST | None, target_file: str, target_qualname: str) -> bool:
    if tree is None:
        return False
    parts = _target_relative_parts(target_file, target_qualname)
    if len(parts) == 1:
        name = parts[0]
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
            for node in getattr(tree, "body", [])
        )
    if len(parts) == 2:
        class_name, method_name = parts
        return bool(_class_method_nodes(tree, class_name, {method_name}))
    return False

def _project_module_exists(project_root: Path, module_name: str) -> bool:
    module_path = module_name.replace(".", "/")
    py_path = project_root / f"{module_path}.py"
    pkg_init = project_root / module_path / "__init__.py"
    namespace_dir = project_root / module_path
    return py_path.exists() or pkg_init.exists() or (namespace_dir.is_dir() and any(namespace_dir.glob("*.py")))


def _project_root_packages(project_root: Path) -> set[str]:
    """Return import roots that belong to the checked project.

    Generated-test validation should not be tied to one demo package name.  We
    treat every top-level Python module/package under ``project_root`` as a
    project import root and use it to validate imports such as
    ``from note.search_result import SearchResult``.
    """
    roots: set[str] = set()
    try:
        children = list(project_root.iterdir())
    except OSError:
        return roots

    for child in children:
        name = child.name
        if not name or name.startswith(".") or name in {"tests", "__pycache__"}:
            continue
        if child.is_file() and child.suffix == ".py" and child.stem.isidentifier():
            roots.add(child.stem)
        elif child.is_dir() and name.isidentifier():
            if (child / "__init__.py").exists() or any(child.glob("*.py")):
                roots.add(name)
    return roots


def _looks_like_project_module(project_root: Path, module_name: str) -> bool:
    root = str(module_name or "").split(".", 1)[0]
    return bool(root and root in _project_root_packages(project_root))


def _module_defined_names(project_root: Path, module_name: str) -> set[str]:
    module_file = _module_file_for_import(project_root, module_name)
    if module_file is None:
        return set()
    try:
        source = module_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    tree = _safe_parse(source)
    if tree is None:
        return set()
    return _module_available_names(tree)


def _project_imported_name_exists(project_root: Path, module_name: str, imported_name: str) -> bool:
    """Check whether ``from module_name import imported_name`` is project-visible."""
    if not imported_name or imported_name == "*":
        return True

    # ``from package import submodule`` is valid if package/submodule.py or
    # package/submodule/__init__.py exists even when package/__init__.py does
    # not explicitly re-export it.
    if _project_module_exists(project_root, f"{module_name}.{imported_name}"):
        return True

    module_file = _module_file_for_import(project_root, module_name)
    if module_file is None:
        return False

    return imported_name in _module_defined_names(project_root, module_name)


def _check_generated_test_project_imports(
    *,
    tree: ast.AST,
    project_root: Path,
    test_file_path: Path,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    issues: list[VerificationIssue] = []
    checked: list[dict[str, Any]] = []
    roots = sorted(_project_root_packages(project_root))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module_name = node.module
            if not _looks_like_project_module(project_root, module_name):
                continue
            module_exists = _project_module_exists(project_root, module_name)
            missing_names: list[str] = []
            if module_exists:
                for alias in node.names:
                    if not _project_imported_name_exists(project_root, module_name, alias.name):
                        missing_names.append(alias.name)
            checked.append(
                {
                    "kind": "from_import",
                    "module": module_name,
                    "line": getattr(node, "lineno", None),
                    "module_exists": module_exists,
                    "names": [alias.name for alias in node.names],
                    "missing_names": missing_names,
                }
            )
            if not module_exists:
                issues.append(
                    VerificationIssue(
                        code="generated_test_import_points_to_missing_module",
                        message=f"Generated test импортирует отсутствующий project module: {module_name}.",
                        file_path=str(test_file_path),
                        symbol=module_name,
                    )
                )
            for name in missing_names:
                issues.append(
                    VerificationIssue(
                        code="generated_test_imports_missing_project_name",
                        message=(
                            f"Generated test импортирует `{name}` из project module `{module_name}`, "
                            "но это имя или submodule не видны в проекте."
                        ),
                        file_path=str(test_file_path),
                        symbol=f"{module_name}.{name}",
                    )
                )

        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if not _looks_like_project_module(project_root, module_name):
                    continue
                module_exists = _project_module_exists(project_root, module_name)
                checked.append(
                    {
                        "kind": "import",
                        "module": module_name,
                        "line": getattr(node, "lineno", None),
                        "module_exists": module_exists,
                    }
                )
                if not module_exists:
                    issues.append(
                        VerificationIssue(
                            code="generated_test_import_points_to_missing_module",
                            message=f"Generated test импортирует отсутствующий project module: {module_name}.",
                            file_path=str(test_file_path),
                            symbol=module_name,
                        )
                    )

    return issues, {"project_roots": roots, "checked_imports": checked}


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


def _check_unresolved_runtime_names(
    *,
    module_tree: ast.AST | None,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if module_tree is None or scan_tree is None:
        return [], {"available_names": [], "checked_names": [], "unknown_names": [], "skipped": True}

    available_names = set(_module_available_names(module_tree))
    available_names.update(_builtin_names())
    local_defined = _collect_defined_names(scan_tree)
    loaded_names = _collect_loaded_names(scan_tree)

    ignored_names = {"self", "cls"}
    unknown_names = sorted(
        name
        for name in loaded_names
        if name not in available_names
        and name not in local_defined
        and name not in ignored_names
        and not name.startswith("__")
    )

    issues = [
        VerificationIssue(
            code="unknown_runtime_name",
            message=(
                f"В сгенерированном production-коде используется имя `{name}`, но оно не импортировано "
                "и не определено в видимом контексте файла. Для repair: верни нужный import в import_changes "
                "или используй уже доступное имя из целевого файла."
            ),
            severity="error",
            file_path=target_file,
            symbol=target_qualname,
        )
        for name in unknown_names
    ]

    return issues, {
        "available_names": sorted(name for name in available_names if not name.startswith("__")),
        "local_defined_names": sorted(local_defined),
        "checked_names": sorted(loaded_names),
        "unknown_names": unknown_names,
        "skipped": False,
    }


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


def _required_class_member_names(required_class_members: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in required_class_members or []:
        if not isinstance(item, dict):
            continue
        if item.get("required") is False:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            "name": name,
            "kind": str(item.get("kind") or "method"),
            "source": ",".join(str(value) for value in (item.get("sources") or []) if str(value)) or str(item.get("source") or ""),
        })
    return result


def _public_class_methods_for_required_members(tree: ast.AST | None, class_name: str) -> list[dict[str, str]]:
    methods = sorted(_class_methods(tree, class_name))
    result = []
    for name in methods:
        if name == "__init__" or not name.startswith("_"):
            result.append({"name": name, "kind": "method", "source": "original_class_public_member"})
    return result


def _check_required_class_members_after_replace(
    *,
    before_tree: ast.AST | None,
    after_tree: ast.AST | None,
    target_name: str,
    target_qualname: str,
    target_file: str,
    required_class_members: list[dict[str, Any]] | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if not target_name:
        return [], {"required_class_members": [], "present_members": [], "missing_members": [], "skipped": True, "reason": "empty_target_name"}

    before_node = _class_node(before_tree, target_name)
    after_node = _class_node(after_tree, target_name)
    if before_node is None or after_node is None:
        return [], {
            "required_class_members": [],
            "present_members": [],
            "missing_members": [],
            "skipped": True,
            "reason": "target_class_missing_before_or_after",
        }

    required = _required_class_member_names(required_class_members)
    if not required:
        required = _public_class_methods_for_required_members(before_tree, target_name)

    present = sorted(_class_methods(after_tree, target_name))
    present_set = set(present)
    missing = [item for item in required if item["name"] not in present_set]

    issues = [
        VerificationIssue(
            code="class_replacement_missing_existing_member",
            message=(
                f"После замены класса {target_name} отсутствует публичный метод {item['name']}. "
                "Метод был в исходном классе и пользователь не просил его удалить. "
                "Для repair: верни этот метод в заменяемый класс и реализуй его вместо удаления."
            ),
            severity="error",
            file_path=target_file,
            symbol=f"{target_qualname}.{item['name']}",
        )
        for item in missing
    ]
    return issues, {
        "required_class_members": required,
        "present_members": present,
        "missing_members": missing,
        "skipped": False,
    }


def _model_surfaces_by_name(model_surfaces: list[dict[str, Any]] | None, related_symbols: list[Any] | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    related_field_types = _related_class_field_type_map(related_symbols)
    related_default_fields = _related_class_default_field_map(related_symbols)

    def add_surface(
        name: str,
        qualname: str,
        fields: set[str],
        constructor_fields: set[str],
        source: str,
        field_types: dict[str, str] | None = None,
        default_fields: set[str] | None = None,
    ) -> None:
        if not name or (not fields and not constructor_fields):
            return
        result[name] = {
            "name": name,
            "qualname": qualname,
            "fields": sorted(fields | constructor_fields),
            "constructor_fields": sorted(constructor_fields),
            "field_types": dict(sorted((field_types or {}).items())),
            "default_fields": sorted(default_fields or set()),
            "source": source,
        }

    for item in model_surfaces or []:
        if not isinstance(item, dict):
            continue
        qualname = str(item.get("qualname") or "").strip()
        name = str(item.get("name") or qualname.rsplit(".", 1)[-1]).strip()
        fields = {str(value) for value in (item.get("fields") or item.get("model_fields") or []) if str(value)}
        constructor_fields = {str(value) for value in (item.get("constructor_fields") or []) if str(value)}
        raw_field_types = item.get("field_types") if isinstance(item.get("field_types"), dict) else {}
        field_types = {str(key): str(value).rsplit(".", 1)[-1] for key, value in raw_field_types.items() if str(key) and str(value)}
        if not field_types:
            field_types = related_field_types.get(name, {})
        raw_default_fields = item.get("default_fields") or item.get("fields_with_defaults") or []
        default_fields = {str(value) for value in raw_default_fields if str(value)} or set(related_default_fields.get(name, set()))
        add_surface(name, qualname, fields, constructor_fields, str(item.get("source") or "model_surfaces"), field_types, default_fields)

    # Fallback: derive simple class surfaces from visible related class symbols.
    for raw in related_symbols or []:
        item = raw if isinstance(raw, dict) else asdict(raw)
        if str(item.get("kind") or "") != "class":
            continue
        source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
        if not source:
            continue
        class_name = str(item.get("name") or item.get("qualname", "").rsplit(".", 1)[-1])
        if class_name in result:
            continue
        fields_by_class = _class_fields_from_source(source)
        fields = set(fields_by_class.get(class_name, set()))
        constructor_fields = _class_constructor_keyword_fields_from_source(source, class_name)
        add_surface(
            class_name,
            str(item.get("qualname") or ""),
            fields,
            constructor_fields,
            "related_class_symbol",
            related_field_types.get(class_name, {}),
            set(related_default_fields.get(class_name, set())),
        )

    return result


def _check_model_surface_usage(
    *,
    tree: ast.AST,
    scan_tree: ast.AST | None,
    target_file: str,
    target_qualname: str,
    model_surfaces: list[dict[str, Any]] | None,
    related_symbols: list[Any] | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if scan_tree is None:
        return [], {"model_surfaces": {}, "checked_constructor_calls": [], "checked_attributes": [], "skipped": True}

    surfaces = _model_surfaces_by_name(model_surfaces, related_symbols)
    if not surfaces:
        return [], {"model_surfaces": {}, "checked_constructor_calls": [], "checked_attributes": [], "skipped": True, "reason": "no_model_surfaces"}

    issues: list[VerificationIssue] = []
    checked_constructor_calls: list[dict[str, Any]] = []
    checked_attributes: list[dict[str, Any]] = []
    parent_class_name = _class_name_from_qualname(target_qualname.rsplit(".", 1)[0])
    self_attribute_types = _self_attribute_type_map(tree, parent_class_name)
    same_class_method_return_types = _same_class_method_return_type_map(
        tree,
        parent_class_name,
        set(surfaces.keys()),
    )
    field_types_by_class = _related_class_field_type_map(related_symbols)
    related_function_return_types = _related_function_return_types(related_symbols)
    parent_map: dict[ast.AST, ast.AST] = {
        child: parent
        for parent in ast.walk(scan_tree)
        for child in ast.iter_child_nodes(parent)
    }

    for function_node in ast.walk(scan_tree):
        if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        local_types: dict[str, str] = {}
        local_maybe_none: set[str] = set()
        for arg in [*function_node.args.posonlyargs, *function_node.args.args, *function_node.args.kwonlyargs]:
            if arg.arg == "self":
                continue
            type_name = _annotation_name(arg.annotation).rsplit(".", 1)[-1]
            if type_name:
                local_types[arg.arg] = type_name

        def infer_model_expr_type(expr: ast.AST) -> str:
            self_method_type = _direct_self_method_call_return_type(expr, same_class_method_return_types)
            if self_method_type:
                return self_method_type
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                return_type = related_function_return_types.get(expr.func.id, "")
                if return_type:
                    return return_type
            return _infer_expr_type_for_contract_call(
                expr,
                local_types=local_types,
                field_types_by_class=field_types_by_class,
                self_attribute_types=self_attribute_types,
            )

        for node in ast.walk(function_node):
            if isinstance(node, ast.Assign):
                value_type = infer_model_expr_type(node.value)
                value_maybe_none = _expr_can_be_none(node.value, local_maybe_none)
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                    class_name = node.value.func.id
                    if class_name in surfaces:
                        value_type = class_name
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if value_type:
                            local_types[target.id] = value_type
                        if value_maybe_none:
                            local_maybe_none.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                type_name = _annotation_name(node.annotation).rsplit(".", 1)[-1] or (infer_model_expr_type(node.value) if node.value else "")
                if type_name:
                    local_types[node.target.id] = type_name
                if node.value is not None and _expr_can_be_none(node.value, local_maybe_none):
                    local_maybe_none.add(node.target.id)

            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                class_name = node.func.id
                surface = surfaces.get(class_name)
                if not surface:
                    continue
                allowed = set(surface.get("constructor_fields") or surface.get("fields") or [])
                supplied = [keyword.arg for keyword in node.keywords if keyword.arg]
                if not supplied or not allowed:
                    continue
                unknown = sorted({name for name in supplied if name not in allowed})
                field_types = surface.get("field_types") if isinstance(surface.get("field_types"), dict) else {}
                default_fields = set(surface.get("default_fields") or [])
                field_type_mismatches: list[dict[str, Any]] = []
                default_none_mismatches: list[dict[str, Any]] = []
                for keyword in node.keywords:
                    if not keyword.arg or keyword.arg not in allowed:
                        continue
                    expected_type = str(field_types.get(keyword.arg) or "")
                    actual_type = infer_model_expr_type(keyword.value)
                    value_can_be_none = _expr_can_be_none(keyword.value, local_maybe_none)
                    if expected_type and actual_type and not _types_are_compatible_for_model_constructor(expected=expected_type, actual=actual_type):
                        field_type_mismatches.append({
                            "field": keyword.arg,
                            "expected_type": expected_type,
                            "actual_type": actual_type,
                            "line": getattr(keyword.value, "lineno", getattr(node, "lineno", None)),
                        })
                    if (
                        keyword.arg in default_fields
                        and expected_type
                        and _is_structured_model_field_type(expected_type)
                        and value_can_be_none
                    ):
                        default_none_mismatches.append({
                            "field": keyword.arg,
                            "expected_type": expected_type,
                            "actual_type": actual_type or "None",
                            "line": getattr(keyword.value, "lineno", getattr(node, "lineno", None)),
                        })
                checked_constructor_calls.append({
                    "function": function_node.name,
                    "class_name": class_name,
                    "line": getattr(node, "lineno", None),
                    "allowed_keywords": sorted(allowed),
                    "supplied_keywords": supplied,
                    "unknown_keywords": unknown,
                    "field_types": dict(sorted(field_types.items())),
                    "field_type_mismatches": field_type_mismatches,
                    "default_fields": sorted(default_fields),
                    "default_none_mismatches": default_none_mismatches,
                    "local_types": dict(sorted(local_types.items())),
                    "local_maybe_none": sorted(local_maybe_none),
                })
                for mismatch in field_type_mismatches:
                    issues.append(
                        VerificationIssue(
                            code="model_constructor_field_type_mismatch",
                            message=(
                                f"В production-коде в конструктор {class_name} для поля `{mismatch['field']}` "
                                f"передано значение типа {mismatch['actual_type']}, но видимый тип поля — "
                                f"{mismatch['expected_type']}. Если значение восстановлено из сериализованных данных, "
                                "его нужно явно преобразовать к типу модели до создания объекта."
                            ),
                            severity="error",
                            file_path=target_file,
                            symbol=target_qualname,
                        )
                    )
                for mismatch in default_none_mismatches:
                    issues.append(
                        VerificationIssue(
                            code="model_constructor_default_field_overridden_with_none",
                            message=(
                                f"В production-коде в конструктор {class_name} для поля `{mismatch['field']}` "
                                f"может быть передан None, хотя поле имеет видимый тип {mismatch['expected_type']} "
                                "и у модели есть значение по умолчанию. Если сериализованное значение отсутствует, "
                                "не передавай это поле в конструктор, чтобы модель использовала свой default."
                            ),
                            severity="error",
                            file_path=target_file,
                            symbol=target_qualname,
                        )
                    )
                for keyword in unknown:
                    issues.append(
                        VerificationIssue(
                            code="unknown_model_constructor_keyword",
                            message=(
                                f"В production-коде передан неизвестный keyword `{keyword}` в конструктор {class_name}. "
                                f"Видимые аргументы конструктора/поля: {', '.join(sorted(allowed))}. "
                                "Для repair: используй видимые поля модели, не alias-имена."
                            ),
                            severity="error",
                            file_path=target_file,
                            symbol=target_qualname,
                        )
                    )

            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                parent = parent_map.get(node)
                if isinstance(parent, ast.Call) and parent.func is node:
                    # ``helper.method(...)`` is a method call, not a model field access.
                    # Method availability/signature is checked by contract/dependency
                    # checks; treating it as an unknown model attribute causes false
                    # positives for visible helper methods such as build_path(...).
                    continue
                variable_name = node.value.id
                type_name = local_types.get(variable_name)
                surface = surfaces.get(type_name or "")
                if not surface:
                    continue
                attr = str(node.attr or "")
                if not attr or attr.startswith("_"):
                    continue
                allowed = set(surface.get("fields") or [])
                if not allowed:
                    continue
                checked = {
                    "function": function_node.name,
                    "variable": variable_name,
                    "type_name": type_name,
                    "attribute": attr,
                    "line": getattr(node, "lineno", None),
                    "visible_fields": sorted(allowed),
                }
                checked_attributes.append(checked)
                if attr not in allowed:
                    issues.append(
                        VerificationIssue(
                            code="unknown_model_attribute",
                            message=(
                                f"В production-коде используется поле {variable_name}.{attr}, "
                                f"но для модели {type_name} видимы только поля: {', '.join(sorted(allowed))}. "
                                "Для repair: используй видимое поле модели или явно преобразуй данные до вызова."
                            ),
                            severity="error",
                            file_path=target_file,
                            symbol=target_qualname,
                        )
                    )

    return issues, {
        "model_surfaces": surfaces,
        "same_class_method_return_types": dict(sorted(same_class_method_return_types.items())),
        "checked_constructor_calls": checked_constructor_calls,
        "checked_attributes": checked_attributes,
        "skipped": False,
    }


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

    parse_signature = signature if signature.endswith(":") else f"{signature}:"
    try:
        tree = ast.parse(f"{parse_signature}\n    pass\n")
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

    call_positional_args = positional_args
    if kind == "method" and call_positional_args and call_positional_args[0].arg in {"self", "cls"}:
        call_positional_args = call_positional_args[1:]

    required_positional_names = [arg.arg for arg in required_positional_args]
    positional_arg_types = [
        {"name": arg.arg, "type": _annotation_name(arg.annotation).rsplit(".", 1)[-1]}
        for arg in call_positional_args
    ]
    keyword_arg_types = {
        arg.arg: _annotation_name(arg.annotation).rsplit(".", 1)[-1]
        for arg in args.kwonlyargs
        if _annotation_name(arg.annotation).rsplit(".", 1)[-1]
    }

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
        "positional_arg_types": positional_arg_types,
        "keyword_arg_types": keyword_arg_types,
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




def _import_change_added_names(import_changes: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Return names added by import_changes mapped to their source change."""
    result: dict[str, dict[str, Any]] = {}
    for change in import_changes or []:
        if not isinstance(change, dict):
            continue
        action = str(change.get("action") or "").strip()
        if action == "add_import":
            module = str(change.get("module") or "").strip()
            if module:
                asname = str(change.get("asname") or change.get("alias") or "").strip()
                imported_name = asname or module.rsplit(".", 1)[-1]
                result[imported_name] = change
        elif action == "add_from_import":
            for raw_name in change.get("names") or []:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                # Support both plain names and the common textual form "name as alias".
                imported_name = raw_name.strip().split(" as ")[-1].strip()
                if imported_name:
                    result[imported_name] = change
    return result


def _local_imported_names(tree: ast.AST | None) -> set[str]:
    """Return import names declared inside the generated symbol scan tree."""
    if tree is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".", 1)[0]
                if imported_name:
                    names.add(imported_name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported_name = alias.asname or alias.name
                if imported_name:
                    names.add(imported_name)
    return names


def _check_import_changes_usage(
    *,
    scan_tree: ast.AST | None,
    import_changes: list[dict[str, Any]] | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    """Check that imports requested by import_changes are actually used.

    import_changes are part of the generated artifact contract. If the model asks
    codecollector to add a file-level import, the generated symbol should use the
    imported name. Otherwise the artifact is internally inconsistent and should
    be repaired instead of leaving dead imports in the target file.
    """
    added_names = _import_change_added_names(import_changes)
    if scan_tree is None or not added_names:
        return [], {
            "added_names": sorted(added_names),
            "used_names": [],
            "local_imported_names": [],
            "unused_imports": [],
            "duplicated_local_imports": [],
            "skipped": scan_tree is None,
        }

    used_names = _collect_loaded_names(scan_tree)
    local_imports = _local_imported_names(scan_tree)
    issues: list[VerificationIssue] = []
    unused: list[str] = []
    duplicated: list[str] = []

    for name in sorted(added_names):
        if name not in used_names:
            unused.append(name)
            issues.append(
                VerificationIssue(
                    code="unused_import_change",
                    message=(
                        f"import_changes добавляет импорт `{name}`, но generated symbol не использует это имя. "
                        "Для repair: удали лишний import_changes или измени code так, чтобы использовалось импортированное имя."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if name in local_imports:
            duplicated.append(name)
            issues.append(
                VerificationIssue(
                    code="duplicated_import_change_with_local_import",
                    message=(
                        f"Имя `{name}` добавлено через import_changes и одновременно импортируется внутри generated symbol. "
                        "Для repair: оставь один способ импорта; обычно импорт должен быть возвращен через import_changes, "
                        "а import-строки внутри поля code нужно убрать."
                    ),
                    severity="error",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )

    return issues, {
        "added_names": sorted(added_names),
        "used_names": sorted(used_names),
        "local_imported_names": sorted(local_imports),
        "unused_imports": unused,
        "duplicated_local_imports": duplicated,
        "skipped": False,
    }


def _top_level_exported_names_from_source(source: str) -> set[str]:
    tree = _safe_parse(source)
    if tree is None:
        return set()
    names: set[str] = set()
    for node in getattr(tree, 'body', []):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split('.', 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != '*':
                    names.add(alias.asname or alias.name)
    return names


def _module_file_for_import_change(project_root: Path, module_name: str) -> Path | None:
    module_path = str(module_name or '').replace('.', '/')
    if not module_path:
        return None
    module_file = project_root / f'{module_path}.py'
    if module_file.exists():
        return module_file
    package_file = project_root / module_path / '__init__.py'
    if package_file.exists():
        return package_file
    return None


def _check_import_changes_resolvable(
    *,
    project_root: Path | None,
    import_changes: list[dict[str, Any]] | None,
    target_file: str,
    target_qualname: str,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    if project_root is None:
        return [], {'checked': [], 'unresolved': [], 'skipped': True, 'reason': 'project_root_not_provided'}

    issues: list[VerificationIssue] = []
    checked: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for change in import_changes or []:
        if not isinstance(change, dict):
            continue
        action = str(change.get('action') or '').strip()
        module_name = str(change.get('module') or '').strip()
        if action not in {'add_import', 'add_from_import'} or not module_name:
            continue
        module_file = _module_file_for_import_change(project_root, module_name)
        record = {'action': action, 'module': module_name, 'module_file': str(module_file) if module_file else ''}
        if module_file is None:
            record['reason'] = 'module_not_found'
            unresolved.append(record)
            issues.append(
                VerificationIssue(
                    code='unresolved_import_change_module',
                    message=(
                        f"import_changes добавляет импорт из модуля `{module_name}`, но такой проектный модуль "
                        "не найден относительно project root. Для repair: используй только подтвержденный путь импорта "
                        "из contract context, related symbols, full file source или стандартной библиотеки."
                    ),
                    severity='error',
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
            checked.append(record)
            continue
        if action == 'add_from_import':
            try:
                exported = _top_level_exported_names_from_source(module_file.read_text(encoding='utf-8'))
            except OSError:
                exported = set()
            requested_names = [str(name).split(' as ')[0].strip() for name in (change.get('names') or []) if str(name).strip()]
            missing_names = sorted(name for name in requested_names if name and name not in exported)
            record['requested_names'] = requested_names
            record['exported_names'] = sorted(exported)
            record['missing_names'] = missing_names
            if missing_names:
                unresolved.append(record)
                issues.append(
                    VerificationIssue(
                        code='unresolved_import_change_name',
                        message=(
                            f"import_changes добавляет from-import из `{module_name}`, но имена не найдены "
                            f"в этом модуле: {', '.join(missing_names)}. Для repair: возьми точный module path "
                            "и имена symbol из видимого проектного контекста, не нормализуй путь по аналогии."
                        ),
                        severity='error',
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )
        checked.append(record)

    return issues, {'checked': checked, 'unresolved': unresolved, 'skipped': False}


def _contract_call_spec_matches_call(func: ast.expr, spec: dict[str, Any]) -> bool:
    """Return whether an AST call expression can refer to the visible contract spec.

    This avoids matching unrelated calls that happen to share the same short name,
    for example ``json.load(...)`` and ``NoteStorage.load(...)``.
    """
    kind = str(spec.get("kind") or "").strip()
    parent_qualname = str(spec.get("parent_qualname") or "").strip()
    parent_name = parent_qualname.rsplit(".", 1)[-1] if parent_qualname else ""

    if isinstance(func, ast.Name):
        # Direct name calls are safe only for visible free functions. Methods
        # should be called through self/instance/class and must not match by
        # short name alone.
        return kind == "function"

    if isinstance(func, ast.Attribute):
        receiver = _call_display_name(func.value)
        if kind == "function":
            # Module-qualified free function calls are allowed when the module
            # path is visible through the receiver name.
            qualname = str(spec.get("qualname") or "")
            module_name = qualname.rsplit(".", 1)[0]
            return bool(receiver and (module_name.endswith(receiver) or receiver.endswith(module_name.rsplit(".", 1)[-1])))
        if kind == "method":
            if receiver in {"self", "cls"}:
                return True
            if parent_name and receiver.rsplit(".", 1)[-1] == parent_name:
                return True
            # Instance variables are intentionally not matched by short method
            # name here. Dependency method calls are checked by dedicated
            # dependency/contract signature checks.
            return False

    return False

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



def _function_signature_from_source(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return a compact function signature from visible source."""
    segment = ast.get_source_segment(source, node) or ""
    if not segment:
        return ""
    header_lines: list[str] = []
    balance = 0
    for raw_line in segment.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        header_lines.append(stripped)
        balance += stripped.count("(") - stripped.count(")")
        if balance <= 0 and stripped.endswith(":"):
            break
    header = " ".join(header_lines).strip()
    return header[:-1].strip() if header.endswith(":") else header


def _signature_from_symbol_item(item: dict[str, Any], *, name: str) -> str:
    """Prefer visible source signature so default values are preserved."""
    source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
    tree = _safe_parse(source) if source else None
    for node in getattr(tree, "body", []) if tree is not None else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            signature = _function_signature_from_source(source, node)
            if signature:
                return signature
    return str(item.get("signature") or "").strip()


def _related_function_return_types(related_symbols: list[Any] | None) -> dict[str, str]:
    """Map visible free function names to their annotated return type."""
    result: dict[str, str] = {}
    for raw in related_symbols or []:
        item = raw if isinstance(raw, dict) else asdict(raw)
        if str(item.get("kind") or "").strip() != "function":
            continue
        name = str(item.get("name") or item.get("qualname", "").rsplit(".", 1)[-1]).strip()
        if not name:
            continue
        signature = _signature_from_symbol_item(item, name=name)
        parsed = _parse_required_call_args(signature, kind="function") if signature else None
        return_annotation = str((parsed or {}).get("return_annotation") or "")
        if return_annotation:
            result[name] = return_annotation
    return result


def _related_class_method_call_specs(source: str, *, qualname: str, class_name: str) -> list[dict[str, Any]]:
    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return []

    specs: list[dict[str, Any]] = []
    for child in class_node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if child.name == "__init__":
            continue
        signature = _function_signature_from_source(source, child)
        parsed_signature = _parse_required_call_args(signature, kind="method") if signature else None
        if parsed_signature is None:
            continue
        has_visible_types = bool(parsed_signature.get("return_annotation")) or any(
            str(item.get("type") or "") for item in (parsed_signature.get("positional_arg_types") or [])
        )
        if not has_visible_types:
            continue
        specs.append(
            {
                "name": child.name,
                "kind": "method",
                "qualname": f"{qualname}.{child.name}",
                "parent_qualname": qualname,
                "signature": parsed_signature["signature"],
                "required_positional": parsed_signature["required_positional"],
                "required_positional_names": parsed_signature.get("required_positional_names", []),
                "required_keyword_only": parsed_signature["required_keyword_only"],
                "positional_arg_types": parsed_signature.get("positional_arg_types", []),
                "keyword_arg_types": parsed_signature.get("keyword_arg_types", {}),
                "return_annotation": parsed_signature.get("return_annotation", ""),
            }
        )
    return specs


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
        name = str(item.get("name") or "").strip()
        qualname = str(item.get("qualname") or "").strip()
        if not name or not qualname:
            continue

        if kind == "class":
            source = str(item.get("source_code") or item.get("source") or item.get("source_excerpt") or "")
            for spec in _related_class_method_call_specs(source, qualname=qualname, class_name=name):
                raw_specs.setdefault(spec["name"], []).append(spec)
            continue

        if kind not in {"function", "method"}:
            continue

        signature = _signature_from_symbol_item(item, name=name)
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
                "positional_arg_types": parsed_signature.get("positional_arg_types", []),
                "keyword_arg_types": parsed_signature.get("keyword_arg_types", {}),
                "return_annotation": parsed_signature.get("return_annotation", ""),
            }
        )

    # Avoid false positives for overloaded or duplicated short names in the visible context.
    return {name: specs[0] for name, specs in raw_specs.items() if len(specs) == 1}



def _normalized_type_name(value: str) -> str:
    value = str(value or "").strip().strip('"\'')
    if not value:
        return ""
    value = value.replace("typing.", "")
    if "[" in value:
        value = value.split("[", 1)[0]
    value = value.rsplit(".", 1)[-1]
    aliases = {
        "DateTime": "datetime",
        "Datetime": "datetime",
        "List": "list",
        "Dict": "dict",
        "Set": "set",
        "Tuple": "tuple",
        "Sequence": "list",
        "MutableSequence": "list",
        "PathLike": "Path",
        "PurePath": "Path",
        "PurePosixPath": "Path",
        "PureWindowsPath": "Path",
    }
    return aliases.get(value, value)


def _types_are_compatible(*, expected: str, actual: str) -> bool:
    expected_norm = _normalized_type_name(expected)
    actual_norm = _normalized_type_name(actual)
    if not expected_norm or not actual_norm:
        return True
    if expected_norm == actual_norm:
        return True
    path_types = {"Path", "PurePath", "PurePosixPath", "PureWindowsPath"}
    if expected_norm in path_types and actual_norm in path_types:
        return True
    return False


def _serialized_value_is_acceptable_for_expected_type(expected: str) -> bool:
    """Return whether a raw serialized value can be passed without conversion.

    Values read from JSON/dict-like payloads are acceptable for primitive fields,
    but not for structured fields such as datetime or project models unless the
    generated code explicitly converts them first.
    """
    expected_norm = _normalized_type_name(expected)
    return expected_norm in {"str", "int", "float", "bool", "dict", "list", "set", "tuple", "bytes"}


def _types_are_compatible_for_model_constructor(*, expected: str, actual: str) -> bool:
    if actual == "serialized_value":
        return _serialized_value_is_acceptable_for_expected_type(expected)
    return _types_are_compatible(expected=expected, actual=actual)


def _infer_expr_type_for_contract_call(
    expr: ast.AST,
    *,
    local_types: dict[str, str],
    field_types_by_class: dict[str, dict[str, str]],
    self_attribute_types: dict[str, str],
) -> str:
    if isinstance(expr, ast.Constant):
        if expr.value is None:
            return "None"
        if isinstance(expr.value, str):
            return "str"
        if isinstance(expr.value, bool):
            return "bool"
        if isinstance(expr.value, int):
            return "int"
        if isinstance(expr.value, float):
            return "float"
        return ""
    if isinstance(expr, ast.JoinedStr):
        return "str"
    if isinstance(expr, ast.IfExp):
        body_type = _infer_expr_type_for_contract_call(
            expr.body,
            local_types=local_types,
            field_types_by_class=field_types_by_class,
            self_attribute_types=self_attribute_types,
        )
        else_type = _infer_expr_type_for_contract_call(
            expr.orelse,
            local_types=local_types,
            field_types_by_class=field_types_by_class,
            self_attribute_types=self_attribute_types,
        )
        if body_type and body_type != "None":
            return body_type
        if else_type and else_type != "None":
            return else_type
        return body_type or else_type
    if isinstance(expr, ast.Name):
        return local_types.get(expr.id, "")
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Attribute):
            if expr.func.attr == "strftime":
                return "str"
            if expr.func.attr == "fromisoformat":
                if isinstance(expr.func.value, ast.Name):
                    return expr.func.value.id
                if isinstance(expr.func.value, ast.Attribute):
                    return expr.func.value.attr
            if expr.func.attr in {"load", "loads"}:
                if isinstance(expr.func.value, ast.Name) and expr.func.value.id == "json":
                    return "serialized_mapping"
            if expr.func.attr == "get" and isinstance(expr.func.value, ast.Name):
                if local_types.get(expr.func.value.id) == "serialized_mapping":
                    return "serialized_value"
        if isinstance(expr.func, ast.Name):
            if expr.func.id in {"str", "int", "float", "bool", "list", "dict", "set", "tuple"}:
                return expr.func.id
            return expr.func.id
        return ""
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
        if local_types.get(expr.value.id) == "serialized_mapping":
            return "serialized_value"
    if isinstance(expr, ast.Attribute):
        if isinstance(expr.value, ast.Name):
            owner_type = local_types.get(expr.value.id, "")
            if owner_type:
                return (field_types_by_class.get(owner_type) or {}).get(expr.attr, "")
            if expr.value.id == "self":
                return self_attribute_types.get(expr.attr, "")
        if isinstance(expr.value, ast.Attribute):
            owner_type = _infer_expr_type_for_contract_call(
                expr.value,
                local_types=local_types,
                field_types_by_class=field_types_by_class,
                self_attribute_types=self_attribute_types,
            )
            if owner_type:
                return (field_types_by_class.get(owner_type) or {}).get(expr.attr, "")
    return ""


def _infer_function_local_value_types(
    function_node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    related_symbols: list[Any] | None,
    self_attribute_types: dict[str, str],
    same_class_method_return_types: dict[str, str] | None = None,
) -> dict[str, str]:
    field_types_by_class = _related_class_field_type_map(related_symbols)
    local_types: dict[str, str] = {}
    for arg in [*function_node.args.posonlyargs, *function_node.args.args, *function_node.args.kwonlyargs]:
        if arg.arg == "self":
            continue
        type_name = _annotation_name(arg.annotation).rsplit(".", 1)[-1]
        if type_name:
            local_types[arg.arg] = type_name

    def infer(expr: ast.AST) -> str:
        self_method_type = _direct_self_method_call_return_type(expr, same_class_method_return_types or {})
        if self_method_type:
            return self_method_type
        return _infer_expr_type_for_contract_call(
            expr,
            local_types=local_types,
            field_types_by_class=field_types_by_class,
            self_attribute_types=self_attribute_types,
        )

    for node in ast.walk(function_node):
        if isinstance(node, ast.Assign):
            value_type = infer(node.value)
            if not value_type:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_types[target.id] = value_type
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            type_name = _annotation_name(node.annotation).rsplit(".", 1)[-1] or infer(node.value) if node.value else ""
            if type_name:
                local_types[node.target.id] = type_name
    return local_types


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
    parent_class_name = _class_name_from_qualname(target_qualname.rsplit(".", 1)[0])
    self_attribute_types = _self_attribute_type_map(tree, parent_class_name)
    same_class_method_return_types = _same_class_method_return_type_map(tree, parent_class_name)

    if not specs_by_name:
        return issues, {
            "known_contract_call_specs": [],
            "checked_calls": checked_calls,
            "skipped_calls": skipped_calls,
        }

    tree_to_scan = scan_tree or tree
    function_nodes: list[ast.AST] = [
        node for node in ast.walk(tree_to_scan) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not function_nodes:
        function_nodes = [tree_to_scan]

    for function_node in function_nodes:
        local_types = (
            _infer_function_local_value_types(
                function_node,
                related_symbols=related_symbols,
                self_attribute_types=self_attribute_types,
                same_class_method_return_types=same_class_method_return_types,
            )
            if isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else {}
        )
        field_types_by_class = _related_class_field_type_map(related_symbols)

        for node in ast.walk(function_node):
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
            positional_arg_types = list(spec.get("positional_arg_types") or [])
            argument_type_mismatches: list[dict[str, Any]] = []
            placeholder_literals: list[dict[str, Any]] = []

            for index, arg in enumerate(node.args):
                if index < len(positional_arg_types):
                    expected_type = str(positional_arg_types[index].get("type") or "")
                    argument_name = str(positional_arg_types[index].get("name") or f"arg{index + 1}")
                    actual_type = _infer_expr_type_for_contract_call(
                        arg,
                        local_types=local_types,
                        field_types_by_class=field_types_by_class,
                        self_attribute_types=self_attribute_types,
                    )
                    if expected_type and actual_type and not _types_are_compatible(expected=expected_type, actual=actual_type):
                        argument_type_mismatches.append(
                            {
                                "argument": argument_name,
                                "position": index + 1,
                                "expected_type": expected_type,
                                "actual_type": actual_type,
                                "line": getattr(arg, "lineno", getattr(node, "lineno", None)),
                            }
                        )

                if index < required_positional:
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
                "argument_type_mismatches": argument_type_mismatches,
                "local_types": local_types,
            }
            checked_calls.append(checked)

            for mismatch in argument_type_mismatches:
                issues.append(
                    VerificationIssue(
                        code="contract_call_argument_type_mismatch",
                        message=(
                            "В сгенерированном коде вызов "
                            f"{call_name} передает значение типа {mismatch['actual_type']} "
                            f"для аргумента {mismatch['argument']}, но видимая сигнатура "
                            f"проектного контракта {spec['qualname']} ожидает {mismatch['expected_type']}. "
                            f"Сигнатура: {spec['signature']}. Для repair: передай значение, "
                            "тип которого следует из видимого контекста, или используй другой видимый контракт."
                        ),
                        severity="error",
                        file_path=target_file,
                        symbol=target_qualname,
                    )
                )

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
                    "positional_arg_types": spec.get("positional_arg_types", []),
                    "keyword_arg_types": spec.get("keyword_arg_types", {}),
                    "return_annotation": spec.get("return_annotation", ""),
                }
                for name, spec in specs_by_name.items()
            ],
            key=lambda item: item["qualname"],
        ),
        "checked_calls": checked_calls,
        "skipped_calls": skipped_calls,
    }



def _code_artifact_boundary_issues(
    *,
    tree: ast.AST | None,
    operation: str | None,
    target_qualname: str | None,
    target_file: str | None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    """Validate that a replace_symbol artifact contains only the target symbol.

    This check is intentionally limited to code_artifact-level validation. It
    prevents a replacement artifact for one method/function/class from smuggling
    additional sibling symbols or local helper functions that belong to a wider
    change.
    """
    details: dict[str, Any] = {
        "operation": operation,
        "target_qualname": target_qualname,
        "target_name": (target_qualname or "").rsplit(".", 1)[-1],
        "top_level_symbols": [],
        "nested_symbols": [],
        "skipped": True,
    }
    if operation != "replace_symbol" or tree is None or not target_qualname:
        return [], details

    target_name = str(target_qualname).rsplit(".", 1)[-1]
    top_level_defs: list[ast.AST] = [
        node
        for node in getattr(tree, "body", [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    top_level_symbols = [
        {
            "name": getattr(node, "name", ""),
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "line": getattr(node, "lineno", None),
        }
        for node in top_level_defs
    ]
    details["top_level_symbols"] = top_level_symbols
    details["skipped"] = False

    issues: list[VerificationIssue] = []
    if len(top_level_defs) != 1:
        issues.append(
            VerificationIssue(
                code="replace_symbol_artifact_must_contain_single_symbol",
                message=(
                    "Для replace_symbol code_artifact должен содержать ровно один top-level symbol — "
                    f"целевой `{target_name}`. Найдено symbols: "
                    f"{', '.join(item['name'] for item in top_level_symbols) or '<none>'}. "
                    "Для repair: верни только полный код целевого symbol и не добавляй соседние методы, "
                    "функции или классы."
                ),
                severity="error",
                file_path=str(target_file or ""),
                symbol=str(target_qualname or ""),
            )
        )
        return issues, details

    root = top_level_defs[0]
    root_name = str(getattr(root, "name", ""))
    if root_name != target_name:
        issues.append(
            VerificationIssue(
                code="replace_symbol_artifact_target_name_mismatch",
                message=(
                    f"Для replace_symbol ожидался symbol `{target_name}`, но code_artifact содержит `{root_name}`. "
                    "Для repair: верни полный код именно целевого symbol с тем же именем."
                ),
                severity="error",
                file_path=str(target_file or ""),
                symbol=str(target_qualname or ""),
            )
        )

    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)):
        nested: list[dict[str, Any]] = []
        for node in ast.walk(root):
            if node is root:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested.append(
                    {
                        "name": getattr(node, "name", ""),
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "line": getattr(node, "lineno", None),
                    }
                )
        details["nested_symbols"] = nested
        if nested:
            issues.append(
                VerificationIssue(
                    code="replace_symbol_artifact_contains_nested_symbol",
                    message=(
                        f"Для replace_symbol target `{target_name}` code_artifact не должен содержать "
                        "локальные helper-функции, классы или дополнительные symbols внутри целевого метода. "
                        f"Найдены nested symbols: {', '.join(item['name'] for item in nested)}. "
                        "Для repair: реализуй вспомогательную логику внутри тела целевого метода без объявления "
                        "новых def/class, либо используй уже видимый проектный контракт."
                    ),
                    severity="error",
                    file_path=str(target_file or ""),
                    symbol=str(target_qualname or ""),
                )
            )

    return issues, details

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

    boundary_issues, boundary_details = _code_artifact_boundary_issues(
        tree=tree,
        operation=operation,
        target_qualname=target_qualname or expected_target_qualname,
        target_file=target_file,
    )
    issues.extend(boundary_issues)

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
            "artifact_boundary_check": boundary_details,
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
                if spec is None or not _contract_call_spec_matches_call(node.value.func, spec):
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
                if spec is not None and _contract_call_spec_matches_call(value.func, spec):
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
            if spec is None or not _contract_call_spec_matches_call(node.value.func, spec):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned_contract_calls[target.id] = spec

        if not assigned_contract_calls:
            continue

        parent_map: dict[ast.AST, ast.AST] = {
            child: parent
            for parent in ast.walk(function_node)
            for child in ast.iter_child_nodes(parent)
        }

        for node in ast.walk(function_node):
            attr = _result_attribute_name(node)
            if attr is None:
                continue
            parent = parent_map.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                # ``data.get(...)`` is a method call, not a read of a model field named ``get``.
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





def _exception_contract_names(method: ast.AST | None) -> set[str]:
    """Return explicitly visible exception/guard contracts for a method.

    This is intentionally conservative: it does not try to prove full behavior
    equivalence. It only extracts exception names that are visible either as
    explicit raise statements or in a Raises-style docstring block.
    """
    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()

    names: set[str] = set()

    for node in ast.walk(method):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name):
            names.add(exc.id)
        elif isinstance(exc, ast.Attribute):
            names.add(exc.attr)

    docstring = ast.get_docstring(method) or ""
    lines = docstring.splitlines()
    in_raises = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if in_raises:
                continue
            continue
        lower = line.lower().rstrip(':')
        if lower in {"raises", "raise", "исключения", "ошибки"}:
            in_raises = True
            continue
        # Stop at common next-section headings.
        if in_raises and lower in {"args", "arguments", "parameters", "returns", "return", "example", "examples", "note", "todo"}:
            in_raises = False
            continue
        if not in_raises:
            continue
        name = line.split(":", 1)[0].strip().strip("`")
        if not name:
            continue
        # Keep this generic but conservative: exception contracts are normally
        # named *Error or *Exception.
        if name.endswith(("Error", "Exception")):
            names.add(name)

    return names


def _class_method_map(tree: ast.AST | None, class_name: str) -> dict[str, ast.AST]:
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return {}
    return {
        child.name: child
        for child in class_node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _collect_possible_lost_method_contract_warnings(
    *,
    before_tree: ast.AST | None,
    after_tree: ast.AST | None,
    target_name: str,
    target_qualname: str,
    required_class_members: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Collect non-blocking diagnostics for visibly lost method contracts."""
    details: dict[str, Any] = {
        "warnings": [],
        "checked_members": [],
        "skipped": True,
        "reason": "not_class_replace",
    }
    if not required_class_members:
        details["reason"] = "no_required_class_members"
        return details

    before_methods = _class_method_map(before_tree, target_name)
    after_methods = _class_method_map(after_tree, target_name)
    if not before_methods or not after_methods:
        details["reason"] = "class_method_map_unavailable"
        return details

    details["skipped"] = False
    details["reason"] = ""

    required_names = [
        str(item.get("name") or "")
        for item in required_class_members
        if item.get("required", True) is not False and str(item.get("name") or "")
    ]
    for method_name in required_names:
        before_method = before_methods.get(method_name)
        after_method = after_methods.get(method_name)
        if before_method is None or after_method is None:
            continue
        before_contracts = sorted(_exception_contract_names(before_method))
        after_contracts = sorted(_exception_contract_names(after_method))
        missing_contracts = sorted(set(before_contracts) - set(after_contracts))
        details["checked_members"].append(
            {
                "method": method_name,
                "old_exception_contracts": before_contracts,
                "new_exception_contracts": after_contracts,
                "missing_exception_contracts": missing_contracts,
            }
        )
        if missing_contracts:
            details["warnings"].append(
                {
                    "code": "possible_existing_method_contract_lost",
                    "severity": "warning",
                    "method": f"{target_qualname}.{method_name}",
                    "message": (
                        f"Existing public method {target_qualname}.{method_name} had visible "
                        f"exception/guard contracts ({', '.join(missing_contracts)}), "
                        "but the generated method may have removed them. This is advisory and does not block apply."
                    ),
                    "missing_exception_contracts": missing_contracts,
                }
            )

    return details

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
    required_class_members: list[dict[str, Any]] | None = None,
    model_surfaces: list[dict[str, Any]] | None = None,
    project_root: Path | None = None,
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

    required_class_member_details: dict[str, Any] = {
        "required_class_members": [],
        "present_members": [],
        "missing_members": [],
        "skipped": True,
        "reason": "not_class_replace",
    }
    model_surface_details: dict[str, Any] = {
        "model_surfaces": {},
        "checked_constructor_calls": [],
        "checked_attributes": [],
        "skipped": True,
    }
    requested_result_model_details: dict[str, Any] = {
        "requested_models": [],
        "forbids_dict": False,
        "constructed_models": [],
        "dict_literals": 0,
        "dict_return_annotations": 0,
        "skipped": True,
    }
    annotation_name_details: dict[str, Any] = {
        "available_names": [],
        "checked_names": [],
        "unknown_names": [],
        "skipped": True,
    }
    possible_method_contract_details: dict[str, Any] = {
        "warnings": [],
        "checked_members": [],
        "skipped": True,
        "reason": "not_class_replace",
    }

    target_is_class_method = _target_is_class_method(target_file, target_qualname)

    if requested_operation == "replace_symbol":
        if not _target_qualname_exists_in_tree(after_tree, target_file, target_qualname):
            issues.append(
                VerificationIssue(
                    code="target_symbol_missing_after_replace",
                    message="После replace_symbol target symbol отсутствует в ожидаемой структуре файла.",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if target_is_class_method and target_name in after_defs:
            issues.append(
                VerificationIssue(
                    code="target_method_moved_to_module_level",
                    message="После replace_symbol метод target оказался функцией уровня модуля, а не методом исходного класса.",
                    file_path=target_file,
                    symbol=target_qualname,
                )
            )
        if before_defs.get(target_name) == "class" and after_defs.get(target_name) == "class":
            required_member_issues, required_class_member_details = _check_required_class_members_after_replace(
                before_tree=before_tree,
                after_tree=after_tree,
                target_name=target_name,
                target_qualname=target_qualname,
                target_file=target_file,
                required_class_members=required_class_members,
            )
            issues.extend(required_member_issues)
            possible_method_contract_details = _collect_possible_lost_method_contract_warnings(
                before_tree=before_tree,
                after_tree=after_tree,
                target_name=target_name,
                target_qualname=target_qualname,
                required_class_members=required_class_members,
            )

    if requested_operation == "replace_symbol":
        if target_is_class_method:
            parent_name_for_target = _class_name_from_qualname(target_qualname.rsplit(".", 1)[0])
            method_nodes = _class_method_nodes(after_tree, parent_name_for_target, {target_name})
            if method_nodes:
                contract_scan_tree = _module_from_nodes(method_nodes)
        else:
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

    annotation_name_issues, annotation_name_details = _check_unresolved_annotation_names(
        module_tree=after_tree,
        scan_tree=contract_scan_tree,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(annotation_name_issues)

    runtime_name_issues, runtime_name_details = _check_unresolved_runtime_names(
        module_tree=after_tree,
        scan_tree=contract_scan_tree,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(runtime_name_issues)

    import_change_issues, import_change_details = _check_import_changes_usage(
        scan_tree=contract_scan_tree,
        import_changes=import_changes,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(import_change_issues)

    import_change_resolve_issues, import_change_resolve_details = _check_import_changes_resolvable(
        project_root=project_root,
        import_changes=import_changes,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(import_change_resolve_issues)

    model_surface_issues, model_surface_details = _check_model_surface_usage(
        tree=after_tree,
        scan_tree=contract_scan_tree,
        target_file=target_file,
        target_qualname=target_qualname,
        model_surfaces=model_surfaces,
        related_symbols=related_symbols,
    )
    issues.extend(model_surface_issues)

    requested_result_model_issues, requested_result_model_details = _check_requested_result_model_usage(
        scan_tree=contract_scan_tree,
        change_request=change_request,
        model_surfaces=model_surfaces,
        target_file=target_file,
        target_qualname=target_qualname,
    )
    issues.extend(requested_result_model_issues)

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

    self_attribute_issues, self_attribute_details = _check_unknown_self_attribute_usage(
        owner_tree=after_tree,
        scan_tree=contract_scan_tree,
        target_file=target_file,
        target_qualname=target_qualname,
        parent_qualname=parent_qualname,
    )
    issues.extend(self_attribute_issues)

    self_method_signature_issues, self_method_signature_details = _check_same_class_method_call_signatures(
        owner_tree=after_tree,
        scan_tree=contract_scan_tree,
        target_file=target_file,
        target_qualname=target_qualname,
        parent_qualname=parent_qualname,
    )
    issues.extend(self_method_signature_issues)

    incompatible_operator_issues, incompatible_operator_details = _check_incompatible_visible_type_operator_usage(
        owner_tree=after_tree,
        scan_tree=contract_scan_tree,
        related_symbols=related_symbols,
        target_file=target_file,
        target_qualname=target_qualname,
        parent_qualname=parent_qualname,
    )
    issues.extend(incompatible_operator_issues)

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
            "self_attribute_usage_check": self_attribute_details,
            "self_method_call_signature_check": self_method_signature_details,
            "visible_type_operator_check": incompatible_operator_details,
            "dict_return_shape_check": return_shape_details,
            "contract_result_field_check": result_field_details,
            "required_contract_usage_check": required_contract_details,
            "required_class_members_check": required_class_member_details,
            "possible_existing_method_contract_lost": possible_method_contract_details,
            "annotation_name_check": annotation_name_details,
            "runtime_name_check": runtime_name_details,
            "import_changes_usage_check": import_change_details,
            "import_changes_resolvable_check": import_change_resolve_details,
            "model_surface_usage_check": model_surface_details,
            "requested_result_model_usage_check": requested_result_model_details,
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
    signature = _class_constructor_signature_from_source(source, class_name)
    if signature.get('constructor_fields'):
        return set(signature['constructor_fields'])

    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return set()

    fields: set[str] = set()
    for child in class_node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            fields.add(child.target.id)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    fields.add(target.id)
    return fields


def _class_constructor_signature_from_source(source: str, class_name: str) -> dict[str, Any]:
    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return {
            'constructor_fields': [],
            'required_fields': [],
            'positional_fields': [],
            'field_annotations': {},
            'accepts_varargs': False,
            'accepts_kwargs': False,
            'has_explicit_init': False,
        }

    init_node = next(
        (
            child
            for child in class_node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == '__init__'
        ),
        None,
    )
    if init_node is None:
        return {
            'constructor_fields': [],
            'required_fields': [],
            'positional_fields': [],
            'field_annotations': {},
            'accepts_varargs': False,
            'accepts_kwargs': False,
            'has_explicit_init': False,
        }

    positional_args = [*init_node.args.posonlyargs, *init_node.args.args]
    if positional_args and positional_args[0].arg == 'self':
        positional_args = positional_args[1:]
    positional_fields = [arg.arg for arg in positional_args]
    kwonly_fields = [arg.arg for arg in init_node.args.kwonlyargs]
    constructor_fields = [*positional_fields, *kwonly_fields]
    field_annotations = {
        arg.arg: _annotation_name(arg.annotation)
        for arg in [*positional_args, *init_node.args.kwonlyargs]
        if _annotation_name(arg.annotation)
    }

    positional_defaults = list(init_node.args.defaults or [])
    required_positional_count = max(0, len(positional_fields) - len(positional_defaults))
    required_fields = list(positional_fields[:required_positional_count])
    for arg, default in zip(init_node.args.kwonlyargs, init_node.args.kw_defaults or []):
        if default is None:
            required_fields.append(arg.arg)

    return {
        'constructor_fields': constructor_fields,
        'required_fields': required_fields,
        'positional_fields': positional_fields,
        'field_annotations': field_annotations,
        'accepts_varargs': init_node.args.vararg is not None,
        'accepts_kwargs': init_node.args.kwarg is not None,
        'has_explicit_init': True,
    }


def _project_constructor_signatures(
    project_root: Path,
    imported_names: dict[str, str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for imported_name, module_name in sorted(imported_names.items()):
        module_file = _module_file_for_import(project_root, module_name)
        if module_file is None:
            continue
        try:
            source = module_file.read_text(encoding='utf-8')
        except OSError:
            continue
        signature = _class_constructor_signature_from_source(source, imported_name)
        fields = signature.get('constructor_fields') or _class_constructor_keyword_fields_from_source(source, imported_name)
        if not fields and not signature.get('required_fields'):
            continue
        signature = dict(signature)
        signature['constructor_fields'] = sorted({str(item) for item in fields if str(item)})
        signature['required_fields'] = [str(item) for item in signature.get('required_fields') or [] if str(item)]
        signature['positional_fields'] = [str(item) for item in signature.get('positional_fields') or [] if str(item)]
        signature['field_annotations'] = {
            str(name): str(annotation)
            for name, annotation in (signature.get('field_annotations') or {}).items()
            if str(name) and str(annotation)
        }
        result[imported_name] = signature
    return result


def _project_constructor_keyword_fields(
    project_root: Path,
    imported_names: dict[str, str],
) -> dict[str, list[str]]:
    return {
        class_name: list(signature.get('constructor_fields') or [])
        for class_name, signature in _project_constructor_signatures(project_root, imported_names).items()
        if signature.get('constructor_fields')
    }


def _project_class_methods_from_source(source: str, class_name: str) -> set[str]:
    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return set()
    return {
        child.name
        for child in class_node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _project_class_instance_attributes_from_source(source: str, class_name: str) -> set[str]:
    tree = _safe_parse(source)
    class_node = _class_node(tree, class_name)
    if class_node is None:
        return set()
    attrs: set[str] = set()
    for node in ast.walk(class_node):
        target_nodes: list[ast.AST] = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                target_nodes.extend(node.targets)
            else:
                target_nodes.append(node.target)
        for target in target_nodes:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                attrs.add(target.attr)
    return attrs


def _project_class_methods(
    project_root: Path,
    imported_names: dict[str, str],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for imported_name, module_name in sorted(imported_names.items()):
        module_file = _module_file_for_import(project_root, module_name)
        if module_file is None:
            continue
        try:
            source = module_file.read_text(encoding='utf-8')
        except OSError:
            continue
        methods = _project_class_methods_from_source(source, imported_name)
        if methods:
            result[imported_name] = methods
    return result


def _project_class_instance_attributes(
    project_root: Path,
    imported_names: dict[str, str],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for imported_name, module_name in sorted(imported_names.items()):
        module_file = _module_file_for_import(project_root, module_name)
        if module_file is None:
            continue
        try:
            source = module_file.read_text(encoding='utf-8')
        except OSError:
            continue
        attrs = _project_class_instance_attributes_from_source(source, imported_name)
        if attrs:
            result[imported_name] = attrs
    return result


def _infer_generated_test_variable_types(
    tree: ast.AST,
    imported_class_names: set[str],
) -> dict[str, str]:
    """Infer simple local variables that hold imported project class instances."""
    variable_types: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [target for target in node.targets if isinstance(target, ast.Name)]
        if not targets:
            continue

        inferred_type = ""
        value = node.value
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name) and func.id in imported_class_names:
                inferred_type = func.id
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "__new__"
                and isinstance(func.value, ast.Name)
                and func.value.id in imported_class_names
            ):
                inferred_type = func.value.id

        if not inferred_type:
            continue
        for target in targets:
            variable_types[target.id] = inferred_type
    return variable_types


def _check_generated_test_project_method_calls(
    *,
    tree: ast.AST,
    project_root: Path,
    imported_names: dict[str, str],
    test_file_path: Path,
    allowed_new_method_names: set[str] | None = None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    class_methods = _project_class_methods(project_root, imported_names)
    imported_class_names = set(class_methods)
    variable_types = _infer_generated_test_variable_types(tree, imported_class_names)
    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []
    allowed_new_methods = set(allowed_new_method_names or set())

    if not class_methods:
        return issues, {"class_methods": {}, "variable_types": {}, "checked_calls": []}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue

        func = node.func
        owner_type = ""
        owner_name = ""
        if isinstance(func.value, ast.Name):
            owner_name = func.value.id
            owner_type = variable_types.get(owner_name, "")
        elif (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "__new__"
            and isinstance(func.value.value, ast.Name)
        ):
            owner_type = func.value.value.id

        if not owner_type or owner_type not in class_methods:
            continue

        method_name = func.attr
        visible_methods = class_methods.get(owner_type) or set()
        checked_calls.append(
            {
                "owner_name": owner_name,
                "owner_type": owner_type,
                "method": method_name,
                "line": getattr(func, "lineno", None),
                "visible_methods": sorted(visible_methods),
            }
        )

        if method_name in visible_methods or method_name in allowed_new_methods:
            continue
        issues.append(
            VerificationIssue(
                code="generated_test_calls_unknown_project_method",
                message=(
                    f"Generated test вызывает `{method_name}` у объекта {owner_type}, но такой метод "
                    f"не виден в проектном классе. Видимые методы: {', '.join(sorted(visible_methods))}."
                ),
                file_path=str(test_file_path),
                symbol=f"{owner_type}.{method_name}",
            )
        )

    return issues, {
        "class_methods": {name: sorted(methods) for name, methods in class_methods.items()},
        "variable_types": variable_types,
        "allowed_new_method_names": sorted(allowed_new_methods),
        "checked_calls": checked_calls,
    }


def _check_generated_test_unknown_project_attribute_assignments(
    *,
    tree: ast.AST,
    project_root: Path,
    imported_names: dict[str, str],
    test_file_path: Path,
    allowed_new_method_names: set[str] | None = None,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    class_methods = _project_class_methods(project_root, imported_names)
    class_attrs = _project_class_instance_attributes(project_root, imported_names)
    imported_class_names = set(class_methods) | set(class_attrs)
    variable_types = _infer_generated_test_variable_types(tree, imported_class_names)
    allowed_new_methods = set(allowed_new_method_names or set())
    issues: list[VerificationIssue] = []
    checked_assignments: list[dict[str, Any]] = []

    def iter_assignment_targets() -> list[ast.AST]:
        targets: list[ast.AST] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets.extend(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets.append(node.target)
        return targets

    for target in iter_assignment_targets():
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
        ):
            continue
        owner_name = target.value.id
        owner_type = variable_types.get(owner_name, "")
        if not owner_type:
            continue
        visible_methods = class_methods.get(owner_type) or set()
        visible_attrs = class_attrs.get(owner_type) or set()
        attr_name = target.attr
        checked_assignments.append({
            "owner_name": owner_name,
            "owner_type": owner_type,
            "attribute": attr_name,
            "line": getattr(target, "lineno", None),
            "visible_attributes": sorted(visible_attrs),
            "visible_methods": sorted(visible_methods),
        })
        if attr_name in visible_attrs or attr_name in visible_methods or attr_name in allowed_new_methods:
            continue
        issues.append(
            VerificationIssue(
                code="generated_test_assigns_unknown_project_attribute",
                message=(
                    f"Generated test присваивает `{attr_name}` объекту {owner_type}, но такой атрибут/метод "
                    "не виден в проектном классе. Для контроля поведения используй видимый method/helper, "
                    "обычный конструктор или локальный fake/stub."
                ),
                file_path=str(test_file_path),
                symbol=f"{owner_type}.{attr_name}",
            )
        )

    return issues, {
        "class_methods": {name: sorted(methods) for name, methods in class_methods.items()},
        "class_attributes": {name: sorted(attrs) for name, attrs in class_attrs.items()},
        "variable_types": variable_types,
        "allowed_new_method_names": sorted(allowed_new_methods),
        "checked_assignments": checked_assignments,
    }


def _check_generated_test_unsafe_new_usage(
    *,
    tree: ast.AST,
    imported_names: dict[str, str],
    test_file_path: Path,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    project_names = set(imported_names)
    issues: list[VerificationIssue] = []
    usages: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "__new__"
            and isinstance(func.value, ast.Name)
            and func.value.id in project_names
        ):
            continue
        usages.append({"class_name": func.value.id, "line": getattr(func, "lineno", None)})
        issues.append(
            VerificationIssue(
                code="generated_test_uses_project_class_new",
                message=(
                    f"Generated test создает {func.value.id} через __new__, обходя видимый __init__. "
                    "Для теста нужно использовать обычный конструктор с видимыми аргументами или локальный fake/stub."
                ),
                file_path=str(test_file_path),
                symbol=func.value.id,
            )
        )
    return issues, {"usages": usages}


def _missing_required_constructor_arguments(
    *,
    call: ast.Call,
    signature: dict[str, Any],
) -> list[str]:
    required = [str(item) for item in signature.get('required_fields') or [] if str(item)]
    if not required:
        return []
    if signature.get('accepts_varargs') or signature.get('accepts_kwargs'):
        return []
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return []
    if any(keyword.arg is None for keyword in call.keywords):
        return []

    positional_fields = [str(item) for item in signature.get('positional_fields') or [] if str(item)]
    supplied_by_position = set(positional_fields[: len(call.args)])
    supplied_by_keyword = {str(keyword.arg) for keyword in call.keywords if keyword.arg}
    return [name for name in required if name not in supplied_by_position and name not in supplied_by_keyword]



def _literal_type_name(node: ast.AST) -> str:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, str):
            return "str"
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if value is None:
            return "None"
    return ""


def _constructor_argument_type_issues(
    *,
    call: ast.Call,
    class_name: str,
    signature: dict[str, Any],
    test_file_path: Path,
) -> list[VerificationIssue]:
    annotations = {
        str(name): str(annotation).rsplit(".", 1)[-1]
        for name, annotation in (signature.get('field_annotations') or {}).items()
        if str(name) and str(annotation)
    }
    if not annotations:
        return []

    positional_fields = [str(item) for item in signature.get('positional_fields') or [] if str(item)]
    supplied: list[tuple[str, ast.AST]] = []
    for field_name, arg_node in zip(positional_fields, call.args):
        supplied.append((field_name, arg_node))
    for keyword in call.keywords:
        if keyword.arg:
            supplied.append((str(keyword.arg), keyword.value))

    issues: list[VerificationIssue] = []
    for field_name, value_node in supplied:
        expected = annotations.get(field_name, "")
        actual = _literal_type_name(value_node)
        if expected in {"Path", "PurePath"} and actual == "str":
            issues.append(
                VerificationIssue(
                    code='generated_test_constructor_argument_type_mismatch',
                    message=(
                        f'Generated test передает строковый literal в аргумент `{field_name}` конструктора {class_name}, '
                        'но видимая аннотация конструктора требует Path/PurePath. Используй pathlib.Path(...) или pytest tmp_path.'
                    ),
                    file_path=str(test_file_path),
                    symbol=f'{class_name}.{field_name}',
                )
            )
    return issues


def _check_generated_test_constructor_keywords(
    *,
    tree: ast.AST,
    project_root: Path,
    imported_names: dict[str, str],
    test_file_path: Path,
) -> tuple[list[VerificationIssue], dict[str, Any]]:
    constructor_signatures = _project_constructor_signatures(project_root, imported_names)
    constructor_fields = {
        class_name: list(signature.get('constructor_fields') or [])
        for class_name, signature in constructor_signatures.items()
    }
    required_constructor_fields = {
        class_name: list(signature.get('required_fields') or [])
        for class_name, signature in constructor_signatures.items()
        if signature.get('required_fields')
    }
    issues: list[VerificationIssue] = []
    checked_calls: list[dict[str, Any]] = []

    if not constructor_signatures:
        return issues, {'constructor_fields': {}, 'required_constructor_fields': {}, 'checked_calls': []}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        class_name = node.func.id
        signature = constructor_signatures.get(class_name)
        if not signature:
            continue
        allowed_fields = set(signature.get('constructor_fields') or [])
        supplied_keywords = [kw.arg for kw in node.keywords if kw.arg]
        unknown = sorted({name for name in supplied_keywords if name not in allowed_fields}) if allowed_fields else []
        missing_required = _missing_required_constructor_arguments(call=node, signature=signature)
        type_issues = _constructor_argument_type_issues(
            call=node,
            class_name=class_name,
            signature=signature,
            test_file_path=test_file_path,
        )
        checked_calls.append({
            'class_name': class_name,
            'module_name': imported_names.get(class_name, ''),
            'line': getattr(node, 'lineno', None),
            'allowed_keywords': sorted(allowed_fields),
            'required_arguments': list(signature.get('required_fields') or []),
            'supplied_positional_count': len(node.args),
            'supplied_keywords': supplied_keywords,
            'unknown_keywords': unknown,
            'missing_required_arguments': missing_required,
            'field_annotations': dict(signature.get('field_annotations') or {}),
            'type_issue_count': len(type_issues),
        })
        issues.extend(type_issues)
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
        if missing_required:
            issues.append(
                VerificationIssue(
                    code='generated_test_missing_required_constructor_argument',
                    message=(
                        f'Generated test вызывает конструктор {class_name} без обязательных аргументов: '
                        f'{", ".join(missing_required)}. Для теста нужно создать экземпляр по видимой сигнатуре __init__.'
                    ),
                    file_path=str(test_file_path),
                    symbol=class_name,
                )
            )

    return issues, {
        'constructor_fields': constructor_fields,
        'required_constructor_fields': required_constructor_fields,
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
    target_owner_name = ""
    if target_name == "__init__" and "." in target_qualname:
        target_owner_name = target_qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]

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

    project_import_issues, project_import_details = _check_generated_test_project_imports(
        tree=tree,
        project_root=project_root,
        test_file_path=test_file_path,
    )
    issues.extend(project_import_issues)

    imported_names = _collect_project_imported_names(tree, project_root)
    unsafe_new_issues, unsafe_new_details = _check_generated_test_unsafe_new_usage(
        tree=tree,
        imported_names=imported_names,
        test_file_path=test_file_path,
    )
    issues.extend(unsafe_new_issues)

    constructor_keyword_issues, constructor_keyword_details = _check_generated_test_constructor_keywords(
        tree=tree,
        project_root=project_root,
        imported_names=imported_names,
        test_file_path=test_file_path,
    )
    issues.extend(constructor_keyword_issues)

    allowed_generated_method_names = {
        name.rsplit('.', 1)[-1]
        for name in (generated_symbol_names or [])
        if str(name or '').strip()
    }

    project_method_issues, project_method_details = _check_generated_test_project_method_calls(
        tree=tree,
        project_root=project_root,
        imported_names=imported_names,
        test_file_path=test_file_path,
        allowed_new_method_names=allowed_generated_method_names,
    )
    issues.extend(project_method_issues)

    attribute_assignment_issues, attribute_assignment_details = _check_generated_test_unknown_project_attribute_assignments(
        tree=tree,
        project_root=project_root,
        imported_names=imported_names,
        test_file_path=test_file_path,
        allowed_new_method_names=allowed_generated_method_names,
    )
    issues.extend(attribute_assignment_issues)

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
    target_reference_names = {target_name}
    if target_owner_name:
        target_reference_names.add(target_owner_name)
    names_to_check = [*target_reference_names, *concrete_new_symbols]

    referenced_names = {
        name
        for name in names_to_check
        if name and name in source
    }
    used_names = {
        name
        for name in names_to_check
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
        if not any(name in referenced_names for name in target_reference_names):
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_reference_target_name",
                    message=f"Generated test не содержит имени target symbol: {target_name}.",
                    file_path=str(test_file_path),
                    symbol=target_name,
                )
            )

        imported_target_names = {name for name in target_reference_names if name in imported_names}
        if imported_target_names and not any(name in used_names for name in imported_target_names):
            issues.append(
                VerificationIssue(
                    code="generated_test_imports_target_but_does_not_use_it",
                    message=f"Generated test импортирует target symbol, но не использует его в вызове или проверке.",
                    file_path=str(test_file_path),
                    symbol=", ".join(sorted(imported_target_names)),
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
            "target_reference_names": sorted(target_reference_names),
            "project_import_check": project_import_details,
            "unsafe_project_new_usage_check": unsafe_new_details,
            "constructor_keyword_check": constructor_keyword_details,
            "project_method_call_check": project_method_details,
            "project_attribute_assignment_check": attribute_assignment_details,
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
    target_owner_name = ""
    if target_name == "__init__" and "." in target_qualname:
        target_owner_name = target_qualname.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    new_symbol_names = [name.rsplit(".", 1)[-1] for name in generated_symbol_names if name]
    relevant_names = [target_name, *new_symbol_names]
    if target_owner_name:
        relevant_names.append(target_owner_name)

    normalized_source = test_source.lower()
    matched_names = [name for name in relevant_names if name and name.lower() in normalized_source]

    called_names = _collect_called_names(tree)
    asserted_names = _collect_assert_names(tree)
    target_reference_names = {target_name}
    if target_owner_name:
        target_reference_names.add(target_owner_name)

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
        if not any(name in matched_names for name in target_reference_names):
            issues.append(
                VerificationIssue(
                    code="generated_test_does_not_reference_target_symbol",
                    message="Generated test не ссылается на target symbol.",
                    symbol=target_name,
                )
            )
        if not any(name in called_names or name in asserted_names for name in target_reference_names):
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
            "target_reference_names": sorted(target_reference_names),
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