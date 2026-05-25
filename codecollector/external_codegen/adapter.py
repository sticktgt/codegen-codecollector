from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codecollector.config import AppConfig
from codecollector.domain.models import ChangeRequest, ContextPack, PatchArtifact
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)



def _json_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))




def _truncate_source(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    suffix = f"\n# ... truncated, original_chars={len(value)}"
    keep = max(0, limit - len(suffix))
    return value[:keep] + suffix, True


def _select_related_symbols(
    context_pack: ContextPack,
    *,
    limit: int,
    source_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    total_chars = 0

    confidence_rank = {'high': 0, 'medium': 1, 'low': 2}
    direction_rank = {'outbound': 0, 'inbound': 1}
    relation_rank = {
        'calls': 0,
        'exposed_by_controller': 1,
        'imports': 2,
        'belongs_to_layer': 3,
    }

    related_symbols = sorted(
        list(context_pack.related_symbols or []),
        key=lambda item: (
            confidence_rank.get(str(item.relation_confidence), 9),
            direction_rank.get(str(item.relation_direction), 9),
            relation_rank.get(str(item.relation_kind), 9),
            item.file_path,
            item.origin_qualname,
            item.qualname,
        ),
    )

    for item in related_symbols[:max(0, limit)]:
        source_excerpt, truncated = _truncate_source(str(item.source_code or ''), source_chars)
        payload = {
            'qualname': item.qualname,
            'file_path': item.file_path,
            'module_name': item.module_name,
            'name': item.name,
            'kind': item.kind,
            'parent_qualname': item.parent_qualname,
            'role': item.role,
            'origin_qualname': item.origin_qualname,
            'relation_kind': item.relation_kind,
            'relation_direction': item.relation_direction,
            'relation_source': item.relation_source,
            'relation_confidence': item.relation_confidence,
            'signature': item.signature,
            'docstring': item.docstring,
            'source_excerpt': source_excerpt,
            'truncated': truncated,
        }
        selected.append(payload)
        total_chars += len(source_excerpt)

    return selected, total_chars


def _as_related_symbol_payload(item: Any, *, source_chars: int | None = None) -> dict[str, Any]:
    """Normalize a related symbol dataclass/dict into request payload shape."""
    if isinstance(item, dict):
        raw = dict(item)
    else:
        raw = {
            'qualname': getattr(item, 'qualname', ''),
            'file_path': getattr(item, 'file_path', ''),
            'module_name': getattr(item, 'module_name', ''),
            'name': getattr(item, 'name', ''),
            'kind': getattr(item, 'kind', ''),
            'parent_qualname': getattr(item, 'parent_qualname', None),
            'role': getattr(item, 'role', ''),
            'origin_qualname': getattr(item, 'origin_qualname', ''),
            'relation_kind': getattr(item, 'relation_kind', ''),
            'relation_direction': getattr(item, 'relation_direction', ''),
            'relation_source': getattr(item, 'relation_source', ''),
            'relation_confidence': getattr(item, 'relation_confidence', ''),
            'signature': getattr(item, 'signature', ''),
            'docstring': getattr(item, 'docstring', ''),
            'source_excerpt': getattr(item, 'source_code', '') or getattr(item, 'source_excerpt', ''),
        }
    source = str(raw.get('source_excerpt') or raw.get('source_code') or raw.get('source') or '')
    truncated = bool(raw.get('truncated', False))
    if source_chars is not None:
        source, truncated = _truncate_source(source, max(0, int(source_chars or 0)))
    return {
        'qualname': str(raw.get('qualname') or ''),
        'file_path': str(raw.get('file_path') or ''),
        'module_name': str(raw.get('module_name') or ''),
        'name': str(raw.get('name') or ''),
        'kind': str(raw.get('kind') or ''),
        'parent_qualname': raw.get('parent_qualname'),
        'role': str(raw.get('role') or ''),
        'origin_qualname': str(raw.get('origin_qualname') or ''),
        'relation_kind': str(raw.get('relation_kind') or ''),
        'relation_direction': str(raw.get('relation_direction') or ''),
        'relation_source': str(raw.get('relation_source') or ''),
        'relation_confidence': str(raw.get('relation_confidence') or ''),
        'signature': str(raw.get('signature') or ''),
        'docstring': str(raw.get('docstring') or ''),
        'source_excerpt': source,
        'truncated': truncated,
    }


def _dedupe_related_symbol_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get('qualname') or item.get('name') or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _module_file_for_module_name(project_root: Path, module_name: str) -> Path | None:
    module_path = str(module_name or '').replace('.', '/')
    if not module_path:
        return None
    py_path = project_root / f'{module_path}.py'
    if py_path.exists():
        return py_path
    init_path = project_root / module_path / '__init__.py'
    if init_path.exists():
        return init_path
    return None


def _split_qualname_to_module(project_root: Path, qualname: str) -> tuple[str, str, Path] | None:
    parts = [part for part in str(qualname or '').split('.') if part]
    for index in range(len(parts) - 1, 0, -1):
        module_name = '.'.join(parts[:index])
        module_file = _module_file_for_module_name(project_root, module_name)
        if module_file is None:
            continue
        symbol_path = '.'.join(parts[index:])
        if symbol_path:
            return module_name, symbol_path, module_file
    return None


def _ast_signature_for_payload(node: Any) -> str:
    import ast

    if isinstance(node, ast.ClassDef):
        return f'class {node.name}:'
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _method_signature_from_ast(node)
    return str(getattr(node, 'name', '') or '')


def _iter_module_symbol_nodes(tree: Any, module_name: str, parent_qualname: str | None = None):
    import ast

    for node in getattr(tree, 'body', []):
        if isinstance(node, ast.ClassDef):
            qualname = f'{module_name}.{node.name}' if not parent_qualname else f'{parent_qualname}.{node.name}'
            yield qualname, node, 'class', parent_qualname
            for child in getattr(node, 'body', []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f'{qualname}.{child.name}', child, 'method', qualname
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = f'{module_name}.{node.name}' if not parent_qualname else f'{parent_qualname}.{node.name}'
            yield qualname, node, 'function', parent_qualname


def _compact_class_source_for_surface(source_text: str, class_node: Any) -> str:
    """Return compact class source that keeps constructor signature visible."""
    import ast

    if not isinstance(class_node, ast.ClassDef):
        return ''
    init_node = next(
        (
            child
            for child in getattr(class_node, 'body', [])
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == '__init__'
        ),
        None,
    )
    if init_node is None:
        return ''
    init_source = ast.get_source_segment(source_text, init_node) or ''
    if not init_source:
        return ''
    init_source = '\n'.join(
        line if line.startswith((' ', '\t')) else f'    {line}'
        for line in init_source.splitlines()
    )
    bases = ''
    if getattr(class_node, 'bases', None):
        try:
            bases = '(' + ', '.join(ast.unparse(base) for base in class_node.bases) + ')'
        except Exception:
            bases = ''
    return f'class {class_node.name}{bases}:\n{init_source}'


def _symbol_payload_from_ast(
    *,
    module_name: str,
    module_file: Path,
    project_root: Path,
    source_text: str,
    qualname: str,
    node: Any,
    kind: str,
    parent_qualname: str | None,
    source_chars: int,
    role: str,
    reason: str,
) -> dict[str, Any]:
    import ast

    segment = ast.get_source_segment(source_text, node) or ''
    if isinstance(node, ast.ClassDef):
        compact_segment = _compact_class_source_for_surface(source_text, node)
        if compact_segment:
            segment = compact_segment
    segment, truncated = _truncate_source(segment, max(0, int(source_chars or 0)))
    try:
        file_path = str(module_file.resolve().relative_to(project_root.resolve()))
    except ValueError:
        file_path = str(module_file)
    return {
        'qualname': qualname,
        'file_path': file_path,
        'module_name': module_name,
        'name': str(getattr(node, 'name', '') or qualname.rsplit('.', 1)[-1]),
        'kind': kind,
        'parent_qualname': parent_qualname,
        'role': role,
        'origin_qualname': '',
        'relation_kind': 'imports',
        'relation_direction': 'outbound',
        'relation_source': 'index',
        'relation_confidence': 'high',
        'signature': _ast_signature_for_payload(node),
        'docstring': ast.get_docstring(node) or '',
        'source_excerpt': segment,
        'truncated': truncated,
        'selection_reason': reason,
    }


def _request_visible_symbol_names(change_request: ChangeRequest) -> set[str]:
    text = ' '.join([
        str(change_request.title or ''),
        str(change_request.description or ''),
        ' '.join(str(item) for item in (change_request.constraints or [])),
        ' '.join(str(item) for item in (change_request.notes or [])),
    ])
    names = set(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', text))
    return {
        name
        for name in names
        if name[:1].isupper() or '_' in name
    }


def _collect_explicit_project_symbol_contexts(
    *,
    project_root: Path,
    change_request: ChangeRequest,
    reuse_existing_logic: dict[str, Any],
    source_chars: int,
) -> list[dict[str, Any]]:
    """Load exact project symbols named by analyze hints and explicit request text.

    Analyze may mark helper functions/classes as required reuse contracts. Those
    symbols must be visible to codegenerator with their true module paths;
    otherwise the model tends to invent nearby modules such as ``models`` or
    ``utils``. The collection is conservative: exact qualnames are loaded first,
    and short names from the user request are resolved only inside modules that
    were already confirmed by exact reuse contracts.
    """
    exact_qualnames = [
        str(item.get('qualname') or '').strip()
        for item in (reuse_existing_logic.get('contracts') or [])
        if isinstance(item, dict) and str(item.get('qualname') or '').strip()
    ]
    request_names = _request_visible_symbol_names(change_request)
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    confirmed_modules: dict[str, tuple[Path, str]] = {}

    def load_module(module_name: str, module_file: Path) -> tuple[Any, str] | None:
        try:
            source_text = module_file.read_text(encoding='utf-8')
        except OSError:
            return None
        try:
            import ast
            tree = ast.parse(source_text)
        except SyntaxError:
            return None
        return tree, source_text

    for qualname in exact_qualnames:
        split = _split_qualname_to_module(project_root, qualname)
        if split is None:
            LOGGER.info('Explicit project symbol not resolved: qualname=%s reason=module_not_found', qualname)
            continue
        module_name, _symbol_path, module_file = split
        loaded = load_module(module_name, module_file)
        if loaded is None:
            LOGGER.info('Explicit project symbol not resolved: qualname=%s reason=parse_or_read_failed', qualname)
            continue
        tree, source_text = loaded
        confirmed_modules[module_name] = (module_file, source_text)
        for node_qualname, node, kind, parent_qualname in _iter_module_symbol_nodes(tree, module_name):
            if node_qualname != qualname:
                continue
            payload = _symbol_payload_from_ast(
                module_name=module_name,
                module_file=module_file,
                project_root=project_root,
                source_text=source_text,
                qualname=node_qualname,
                node=node,
                kind=kind,
                parent_qualname=parent_qualname,
                source_chars=source_chars,
                role='required_reuse_contract',
                reason='analyze_reuse_existing_logic',
            )
            payloads.append(payload)
            seen.add(node_qualname)
            break

    for module_name, (module_file, source_text) in confirmed_modules.items():
        try:
            import ast
            tree = ast.parse(source_text)
        except SyntaxError:
            continue
        for node_qualname, node, kind, parent_qualname in _iter_module_symbol_nodes(tree, module_name):
            name = str(getattr(node, 'name', '') or '')
            if name not in request_names or node_qualname in seen:
                continue
            payloads.append(
                _symbol_payload_from_ast(
                    module_name=module_name,
                    module_file=module_file,
                    project_root=project_root,
                    source_text=source_text,
                    qualname=node_qualname,
                    node=node,
                    kind=kind,
                    parent_qualname=parent_qualname,
                    source_chars=source_chars,
                    role='explicit_request_symbol',
                    reason='explicit_name_in_request_same_module_as_reuse_contract',
                )
            )
            seen.add(node_qualname)

    if payloads:
        LOGGER.info(
            'Explicit project symbol contexts added: count=%s symbols=%s',
            len(payloads),
            [item.get('qualname') for item in payloads],
        )
    return _dedupe_related_symbol_payloads(payloads)


def _select_related_tests(context_pack: ContextPack, limit: int = 1) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    total_chars = 0

    def sort_key(item):
        source = item.source_code or ''
        is_module_level = item.qualname == item.file_path.replace('/', '.')[:-3] if item.file_path.endswith('.py') else False
        return (is_module_level, len(source), item.qualname)

    for item in sorted(context_pack.related_tests, key=sort_key):
        source = item.source_code or ''
        selected.append({
            'qualname': item.qualname,
            'file_path': item.file_path,
            'source': source,
            'truncated': False,
        })
        total_chars += len(source)
        if len(selected) >= max(0, limit):
            break

    return selected, total_chars



def _request_indicates_new_dataclass_or_class(change_request: ChangeRequest) -> bool:
    text = ' '.join(
        [
            change_request.title or '',
            change_request.description or '',
            *[str(item) for item in (change_request.constraints or [])],
        ]
    ).casefold()
    if 'dataclass' in text or 'data class' in text:
        return True
    return ('класс' in text or 'модел' in text) and ('адрес' in text or 'address' in text)


def _module_parent_qualname_for_insert_anchor(target: SymbolRecord) -> str:
    if target.kind == 'module':
        return target.qualname
    return str(target.parent_qualname or target.module_name or '')


def _normalize_insert_target_metadata(
    *,
    change_request: ChangeRequest,
    target: SymbolRecord,
    operation: str,
    insert_scope: str | None,
) -> tuple[str | None, str, str]:
    normalized_insert_scope = str(insert_scope or '').strip() or None
    parent_qualname = ''
    expected_new_symbol_kind = ''

    if operation != 'insert_after_symbol':
        return normalized_insert_scope, parent_qualname, expected_new_symbol_kind

    request_wants_new_class = _request_indicates_new_dataclass_or_class(change_request)
    if normalized_insert_scope == 'class_body' and request_wants_new_class:
        LOGGER.info(
            'Normalizing insert metadata for new class/dataclass: target=%s target_kind=%s insert_scope=class_body -> module_body',
            target.qualname,
            target.kind,
        )
        normalized_insert_scope = 'module_body'

    if normalized_insert_scope == 'class_body':
        if target.kind == 'method':
            parent_qualname = str(target.parent_qualname or '')
        elif target.kind == 'class':
            parent_qualname = target.qualname
        expected_new_symbol_kind = 'method'
    elif normalized_insert_scope == 'module_body':
        parent_qualname = _module_parent_qualname_for_insert_anchor(target)
        expected_new_symbol_kind = 'class' if request_wants_new_class else 'function'

    return normalized_insert_scope, parent_qualname, expected_new_symbol_kind

def _same_file_module_source(context_pack: ContextPack) -> str:
    target = context_pack.target
    for item in [target, *context_pack.neighbors]:
        if item.file_path == target.file_path and item.kind == 'module' and item.source_code:
            return item.source_code
    return ''


def _build_parent_context_payload(
    context_pack: ContextPack,
    *,
    parent_qualname: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not parent_qualname:
        return None, []

    target = context_pack.target
    parent_symbol = next(
        (item for item in [target, *context_pack.neighbors] if item.qualname == parent_qualname),
        None,
    )
    parent_symbol_payload = None
    if parent_symbol is not None:
        parent_symbol_payload = {
            'qualname': parent_symbol.qualname,
            'name': parent_symbol.name,
            'kind': parent_symbol.kind,
            'docstring': parent_symbol.docstring,
            'source': parent_symbol.source_code,
        }

    class_members: list[dict[str, Any]] = []
    for item in [target, *context_pack.neighbors]:
        if item.parent_qualname == parent_qualname and item.kind == 'method':
            first_line = (item.source_code or '').strip().splitlines()[0] if item.source_code else item.name
            class_members.append({
                'qualname': item.qualname,
                'name': item.name,
                'kind': item.kind,
                'signature': first_line.strip(),
                'docstring': item.docstring,
            })

    return parent_symbol_payload, class_members


def _allowed_api_surface_from_previous_request(previous_generation_request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(previous_generation_request, dict):
        return None
    project_context = previous_generation_request.get('project_context')
    if not isinstance(project_context, dict):
        return None
    allowed_api_surface = project_context.get('allowed_api_surface')
    if not isinstance(allowed_api_surface, dict):
        return None
    if not allowed_api_surface.get('dependencies') and not allowed_api_surface.get('free_functions'):
        return None
    return allowed_api_surface


def _annotation_base_name(annotation: Any) -> str:
    name = _annotation_name_for_allowed_surface(annotation)
    return str(name or '').rsplit('.', 1)[-1]


def _annotation_item_type_name(annotation: Any) -> str:
    import ast

    if isinstance(annotation, ast.Subscript):
        value_name = _annotation_base_name(annotation.value)
        if value_name in {'list', 'List', 'Sequence', 'Iterable', 'set', 'Set', 'tuple', 'Tuple'}:
            slice_node = annotation.slice
            if isinstance(slice_node, ast.Tuple) and slice_node.elts:
                slice_node = slice_node.elts[0]
            return _annotation_base_name(slice_node)
    return ''


def _literal_default_for_requirement(node: Any) -> str:
    import ast

    if node is None:
        return ''
    try:
        return ast.unparse(node)
    except Exception:
        return ''


def _class_field_info_from_source(source: str) -> dict[str, Any]:
    import ast

    tree = _parse_python_tree_for_allowed_surface(source)
    if tree is None:
        return {}

    class_node = next((node for node in getattr(tree, 'body', []) if isinstance(node, ast.ClassDef)), None)
    if class_node is None:
        return {}

    fields: dict[str, dict[str, Any]] = {}
    for child in class_node.body:
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            name = child.target.id
            fields[name] = {
                'name': name,
                'annotation': _annotation_name_for_allowed_surface(child.annotation),
                'has_default': child.value is not None,
                'default': _literal_default_for_requirement(child.value),
            }
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    fields.setdefault(name, {
                        'name': name,
                        'annotation': '',
                        'has_default': True,
                        'default': _literal_default_for_requirement(child.value),
                    })

    init_node = next(
        (
            child
            for child in class_node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == '__init__'
        ),
        None,
    )
    init_args: list[dict[str, Any]] = []
    if init_node is not None:
        positional = [arg for arg in [*init_node.args.posonlyargs, *init_node.args.args] if arg.arg != 'self']
        defaults = list(init_node.args.defaults or [])
        defaults_by_arg: dict[str, Any] = {}
        if defaults:
            for arg, default in zip(positional[-len(defaults):], defaults):
                defaults_by_arg[arg.arg] = default
        for arg in [*positional, *init_node.args.kwonlyargs]:
            has_default = arg.arg in defaults_by_arg
            default_node = defaults_by_arg.get(arg.arg)
            init_args.append({
                'name': arg.arg,
                'annotation': _annotation_name_for_allowed_surface(arg.annotation),
                'has_default': has_default,
                'default': _literal_default_for_requirement(default_node),
            })

    constructor_fields = init_args if init_args else list(fields.values())
    return {
        'class_name': class_node.name,
        'fields': list(fields.values()),
        'field_names': sorted(fields),
        'constructor_fields': constructor_fields,
        'constructor_field_names': [item['name'] for item in constructor_fields if item.get('name')],
        'required_constructor_fields': [
            item['name']
            for item in constructor_fields
            if item.get('name') and not item.get('has_default')
        ],
    }


def _raw_related_symbol_payloads(context_pack: ContextPack) -> list[dict[str, Any]]:
    """Return untruncated related symbol payloads for structural context blocks."""
    result: list[dict[str, Any]] = []
    for item in context_pack.related_symbols or []:
        result.append({
            'qualname': item.qualname,
            'file_path': item.file_path,
            'module_name': item.module_name,
            'name': item.name,
            'kind': item.kind,
            'parent_qualname': item.parent_qualname,
            'role': item.role,
            'origin_qualname': item.origin_qualname,
            'relation_kind': item.relation_kind,
            'relation_direction': item.relation_direction,
            'relation_source': item.relation_source,
            'relation_confidence': item.relation_confidence,
            'signature': item.signature,
            'docstring': item.docstring,
            'source_excerpt': item.source_code or '',
            'truncated': False,
        })
    return result


def _build_model_surfaces(symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build visible model/class surface from related class symbols.

    The block is used by code generation and test generation to avoid alias
    fields like title when the visible model constructor accepts topic.
    """
    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in symbols or []:
        if str(item.get('kind') or '') != 'class':
            continue
        source = str(item.get('source_excerpt') or item.get('source_code') or item.get('source') or '')
        info = _class_field_info_from_source(source)
        class_name = str(info.get('class_name') or item.get('name') or item.get('qualname', '').rsplit('.', 1)[-1]).strip()
        qualname = str(item.get('qualname') or '').strip()
        key = qualname or class_name
        if not class_name or not key or key in seen:
            continue
        field_names = list(info.get('field_names') or [])
        constructor_names = list(info.get('constructor_field_names') or [])
        # Keep the surface only when we have some concrete structural data.
        if not field_names and not constructor_names:
            continue
        seen.add(key)
        all_fields = sorted(set(field_names) | set(constructor_names))
        surfaces.append({
            'name': class_name,
            'qualname': qualname,
            'fields': all_fields,
            'model_fields': field_names,
            'constructor_fields': constructor_names,
            'required_constructor_fields': list(info.get('required_constructor_fields') or []),
            'source': 'related_class_symbol',
        })

    return surfaces


def _build_contract_attribute_requirements(related_symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Infer fields read by production contracts from visible source excerpts.

    This is intentionally conservative: if we cannot connect a collection item
    variable to a visible item type and class/model source, we do not emit a
    requirement instead of guessing.
    """
    import ast

    symbols = [dict(item) for item in related_symbols if isinstance(item, dict)]
    class_info_by_name: dict[str, dict[str, Any]] = {}
    class_qualname_by_name: dict[str, str] = {}
    for item in symbols:
        if str(item.get('kind') or '') != 'class':
            continue
        source = str(item.get('source_excerpt') or item.get('source_code') or item.get('source') or '')
        info = _class_field_info_from_source(source)
        class_name = str(info.get('class_name') or item.get('name') or item.get('qualname', '').rsplit('.', 1)[-1])
        if not class_name or not info.get('field_names'):
            continue
        class_info_by_name[class_name] = info
        class_qualname_by_name[class_name] = str(item.get('qualname') or '')

    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for item in symbols:
        if str(item.get('kind') or '') not in {'function', 'method'}:
            continue
        source = str(item.get('source_excerpt') or item.get('source_code') or item.get('source') or '')
        if not source:
            continue
        tree = _parse_python_tree_for_allowed_surface(source)
        if tree is None:
            continue
        fn = next((node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if fn is None:
            continue

        collection_param_items: dict[str, str] = {}
        object_param_types: dict[str, str] = {}
        for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
            if arg.arg == 'self':
                continue
            item_type = _annotation_item_type_name(arg.annotation)
            if item_type:
                collection_param_items[arg.arg] = item_type
            else:
                type_name = _annotation_base_name(arg.annotation)
                if type_name:
                    object_param_types[arg.arg] = type_name

        var_bindings: dict[str, dict[str, str]] = {}
        for param_name, type_name in object_param_types.items():
            if type_name in class_info_by_name:
                var_bindings[param_name] = {'parameter': param_name, 'item_type': type_name}

        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name) and isinstance(node.iter, ast.Name):
                item_type = collection_param_items.get(node.iter.id)
                if item_type in class_info_by_name:
                    var_bindings[node.target.id] = {'parameter': node.iter.id, 'item_type': item_type}
            elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for generator in node.generators:
                    if isinstance(generator.target, ast.Name) and isinstance(generator.iter, ast.Name):
                        item_type = collection_param_items.get(generator.iter.id)
                        if item_type in class_info_by_name:
                            var_bindings[generator.target.id] = {'parameter': generator.iter.id, 'item_type': item_type}

        fields_by_key: dict[tuple[str, str], set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            binding = var_bindings.get(node.value.id)
            if not binding:
                continue
            field_name = str(node.attr or '').strip()
            if not field_name or field_name.startswith('_'):
                continue
            fields_by_key.setdefault((binding['parameter'], binding['item_type']), set()).add(field_name)

        for (parameter, item_type), fields in sorted(fields_by_key.items()):
            info = class_info_by_name.get(item_type) or {}
            known_fields = set(info.get('field_names') or [])
            required_fields = sorted(field for field in fields if not known_fields or field in known_fields)
            if not required_fields:
                continue
            key = (str(item.get('qualname') or ''), parameter, item_type)
            if key in seen:
                continue
            seen.add(key)
            requirements.append({
                'contract_qualname': str(item.get('qualname') or ''),
                'contract_name': str(item.get('name') or ''),
                'parameter': parameter,
                'item_type': item_type,
                'item_qualname': class_qualname_by_name.get(item_type, ''),
                'required_fields': required_fields,
                'model_fields': list(info.get('field_names') or []),
                'constructor_fields': list(info.get('constructor_field_names') or []),
                'required_constructor_fields': list(info.get('required_constructor_fields') or []),
                'source': 'contract_source_attribute_reads',
            })

    return requirements



def _request_mentions_member_removal(change_request: ChangeRequest, member_name: str) -> bool:
    """Return True only for explicit, local removal instructions.

    This is intentionally narrow: by default replace_symbol for a class must
    preserve the public class surface. Phrases like ``убрать NotImplementedError
    из save_note`` mean "implement the method", not "remove save_note".
    """
    import re

    if not member_name:
        return False

    raw_text = " ".join([
        str(change_request.title or ""),
        str(change_request.description or ""),
        " ".join(str(item) for item in (change_request.constraints or [])),
        " ".join(str(item) for item in (change_request.notes or [])),
    ])
    text = raw_text.casefold()
    member = re.escape(member_name.casefold())

    if not re.search(rf"(?<![\w.]){member}(?![\w.])", text):
        return False

    # Explicit removal patterns. Keep these narrow to avoid interpreting
    # "убрать NotImplementedError из <method>" as a request to delete a method.
    explicit_patterns = [
        rf"(?:удалить|удали|удаляем|remove|delete|drop)\s+(?:метод|method|member)?\s*{member}(?![\w.])",
        rf"(?:убрать|убери|исключить|исключи)\s+(?:метод|method|member)\s+{member}(?![\w.])",
        rf"(?<![\w.]){member}(?![\w.])\s+(?:больше\s+)?(?:не\s+нужен|не\s+нужна|не\s+нужно|не\s+использовать)",
        rf"(?:не\s+реализовывать|не\s+оставлять|не\s+сохранять)\s+(?:метод|method|member)?\s*{member}(?![\w.])",
    ]
    for pattern in explicit_patterns:
        if re.search(pattern, text):
            return True

    return False


def _public_class_member_names_from_source(source: str, class_name: str) -> list[str]:
    import ast

    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        names: list[str] = []
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = str(child.name or "")
                if name == "__init__" or (name and not name.startswith("_")):
                    names.append(name)
        return names
    return []


def _build_required_class_members(
    *,
    context_pack: ContextPack,
    change_request: ChangeRequest,
    operation: str,
) -> list[dict[str, Any]]:
    """Build a conservative public-surface contract for class replacement.

    For replace_symbol on a class, generated code should not silently delete
    existing public methods. The class source/index is the source of truth; the
    user request may only explicitly opt out of keeping a member.
    """
    target = context_pack.target
    if str(operation or "") != "replace_symbol" or target.kind != "class":
        return []

    names: list[str] = []
    seen: set[str] = set()
    for item in [target, *context_pack.neighbors]:
        if item.parent_qualname != target.qualname or item.kind != "method":
            continue
        name = str(item.name or "")
        if not name or (name.startswith("_") and name != "__init__"):
            continue
        if name not in seen:
            seen.add(name)
            names.append(name)

    if not names:
        for name in _public_class_member_names_from_source(target.source_code or "", target.name):
            if name not in seen:
                seen.add(name)
                names.append(name)

    members: list[dict[str, Any]] = []
    for name in names:
        user_requested_removal = _request_mentions_member_removal(change_request, name)
        payload = {
            "name": name,
            "kind": "method",
            "sources": ["existing_class_member"],
            "required": not user_requested_removal,
        }
        if user_requested_removal:
            payload["exclusion_reason"] = "user_requested_removal"
        members.append(payload)

    return members


def _has_required_contract_reuse_signal(change_request: ChangeRequest) -> bool:
    text = " ".join([
        str(change_request.title or ""),
        str(change_request.description or ""),
        " ".join(str(item) for item in (change_request.constraints or [])),
        " ".join(str(item) for item in (change_request.notes or [])),
    ]).casefold()
    if not text.strip():
        return False
    reuse_markers = (
        "использ",
        "переиспольз",
        "reuse",
        "use existing",
        "existing",
    )
    existing_contract_markers = (
        "существ",
        "сервис",
        "service",
        "контракт",
        "contract",
        "функц",
        "helper",
    )
    avoid_duplicate_markers = (
        "не дублир",
        "не копир",
        "не повтор",
        "do not duplicate",
        "without duplicating",
    )
    return (
        any(marker in text for marker in reuse_markers)
        and any(marker in text for marker in existing_contract_markers)
    ) or any(marker in text for marker in avoid_duplicate_markers)


def _request_contract_topic_tokens(change_request: ChangeRequest) -> set[str]:
    text = " ".join([
        str(change_request.title or ""),
        str(change_request.description or ""),
        " ".join(str(item) for item in (change_request.constraints or [])),
        " ".join(str(item) for item in (change_request.notes or [])),
    ]).casefold()
    tokens: set[str] = set()
    if any(item in text for item in ("стат", "summary", "summar", "свод", "отчет", "отчёт", "report")):
        tokens.update({"stat", "stats", "statistics", "summary", "summar", "report"})
    if any(item in text for item in ("тикет", "ticket")):
        tokens.add("ticket")
    return tokens


def _contract_matches_request_topic(contract: dict[str, Any], topic_tokens: set[str]) -> bool:
    if not topic_tokens:
        return True
    haystack = " ".join(
        str(contract.get(key) or "")
        for key in ("name", "qualname", "signature", "origin_qualname")
    ).casefold()
    return any(token in haystack for token in topic_tokens)


def _normalize_reuse_existing_logic_hint(change_request: ChangeRequest) -> dict[str, Any]:
    """Return analyze-provided reuse recommendation without making it mandatory.

    The hint is produced by analyze and is used only to enrich generation context.
    It must not be inferred from raw request text here.
    """
    raw_hints = getattr(change_request, 'context_hints', {}) or {}
    raw = raw_hints.get('reuse_existing_logic') if isinstance(raw_hints, dict) else {}
    if not isinstance(raw, dict):
        return {'mode': 'none', 'confidence': 0.0, 'reason': '', 'contracts': []}
    mode = str(raw.get('mode') or 'none').strip().lower()
    if mode not in {'required', 'recommended', 'none'}:
        mode = 'none'
    try:
        confidence = float(raw.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    contracts: list[dict[str, Any]] = []
    for item in raw.get('contracts') or []:
        if not isinstance(item, dict):
            continue
        qualname = str(item.get('qualname') or '').strip()
        if not qualname:
            continue
        contracts.append({
            'qualname': qualname,
            'role': str(item.get('role') or '').strip(),
            'reason': str(item.get('reason') or '').strip(),
        })
    if not contracts:
        mode = 'none'
    return {
        'mode': mode,
        'confidence': max(0.0, min(1.0, confidence)),
        'reason': str(raw.get('reason') or '').strip(),
        'contracts': contracts,
        'source': 'analyze',
    }


def _method_signature_from_ast(node: Any) -> str:
    try:
        import ast
        args = []
        for arg in list(getattr(node.args, 'posonlyargs', [])) + list(getattr(node.args, 'args', [])):
            name = str(getattr(arg, 'arg', '') or '')
            if not name:
                continue
            if getattr(arg, 'annotation', None) is not None:
                try:
                    name += f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            args.append(name)
        if getattr(node.args, 'vararg', None) is not None:
            args.append('*' + str(node.args.vararg.arg))
        if getattr(node.args, 'kwonlyargs', None):
            if not getattr(node.args, 'vararg', None):
                args.append('*')
            for arg in node.args.kwonlyargs:
                name = str(getattr(arg, 'arg', '') or '')
                if getattr(arg, 'annotation', None) is not None:
                    try:
                        name += f": {ast.unparse(arg.annotation)}"
                    except Exception:
                        pass
                args.append(name)
        if getattr(node.args, 'kwarg', None) is not None:
            args.append('**' + str(node.args.kwarg.arg))
        suffix = ''
        if getattr(node, 'returns', None) is not None:
            try:
                suffix = f" -> {ast.unparse(node.returns)}"
            except Exception:
                suffix = ''
        return f"def {node.name}({', '.join(args)}){suffix}:"
    except Exception:
        return f"def {getattr(node, 'name', '')}(...)"


def _same_class_methods_from_source(
    *,
    project_root: Path,
    target: Any,
    max_items: int = 12,
    source_chars: int = 900,
) -> list[dict[str, Any]]:
    """Extract sibling methods of the target class from the current source file.

    This gives generation a compact view of same-class helpers even when graph
    relations do not yet connect the target method to newly-added helpers.
    """
    parent_qualname = str(getattr(target, 'parent_qualname', '') or '')
    target_qualname = str(getattr(target, 'qualname', '') or '')
    if str(getattr(target, 'kind', '') or '') == 'class':
        parent_qualname = target_qualname
    if not parent_qualname:
        return []
    class_name = parent_qualname.rsplit('.', 1)[-1]
    source_path = (project_root / str(getattr(target, 'file_path', '') or '')).resolve()
    try:
        text = source_path.read_text(encoding='utf-8')
    except Exception:
        return []
    try:
        import ast
        tree = ast.parse(text)
    except Exception:
        return []
    module_name = str(getattr(target, 'module_name', '') or parent_qualname.rsplit('.', 1)[0])
    methods: list[dict[str, Any]] = []
    for node in getattr(tree, 'body', []):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for child in getattr(node, 'body', []):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualname = f'{module_name}.{class_name}.{child.name}'
            if qualname == target_qualname:
                continue
            segment = ast.get_source_segment(text, child) or ''
            if source_chars > 0 and len(segment) > source_chars:
                segment = segment[:source_chars] + f"\n# ... truncated, original_chars={len(ast.get_source_segment(text, child) or '')}"
            methods.append({
                'qualname': qualname,
                'name': child.name,
                'kind': 'method',
                'signature': _method_signature_from_ast(child),
                'docstring': ast.get_docstring(child) or '',
                'source_excerpt': segment,
            })
        break
    return methods[:max_items]


def _build_required_contracts(
    *,
    change_request: ChangeRequest,
    allowed_api_surface: dict[str, Any],
    related_symbols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Infer production contracts that generated code must call.

    This is intentionally conservative. It only emits requirements when the
    user explicitly asks to reuse an existing service/contract or avoid
    duplicated business logic, and when a visible free production function is
    available in the allowed API surface. Dependency methods are not made
    mandatory here because they are often plumbing calls rather than the
    business contract requested by the user.
    """
    if not _has_required_contract_reuse_signal(change_request):
        return []

    topic_tokens = _request_contract_topic_tokens(change_request)
    free_functions = [
        dict(item)
        for item in (allowed_api_surface.get('free_functions') or [])
        if isinstance(item, dict) and str(item.get('name') or item.get('qualname') or '').strip()
    ]
    if not free_functions:
        return []

    selected = [item for item in free_functions if _contract_matches_request_topic(item, topic_tokens)]
    if not selected and len(free_functions) == 1:
        selected = free_functions
    if not selected:
        return []

    related_by_qualname = {str(item.get('qualname') or ''): item for item in related_symbols if isinstance(item, dict)}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        qualname = str(item.get('qualname') or '').strip()
        name = str(item.get('name') or qualname.rsplit('.', 1)[-1]).strip()
        if not qualname and not name:
            continue
        key = qualname or name
        if key in seen:
            continue
        seen.add(key)
        related = related_by_qualname.get(qualname, {}) if qualname else {}
        result.append({
            'qualname': qualname,
            'name': name,
            'signature': str(item.get('signature') or related.get('signature') or ''),
            'reason': 'user_requested_existing_contract_reuse',
            'source': 'allowed_api_surface.free_functions',
            'origin_qualname': str(item.get('origin_qualname') or ''),
        })
    return result


@dataclass(slots=True)
class CodeGeneratorCallResult:
    request_path: str
    result_path: str
    command: list[str]
    request_payload: dict[str, Any]
    result_payload: dict[str, Any]
    trace_path: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None





def _parse_python_tree_for_allowed_surface(source: str) -> Any | None:
    import ast

    try:
        return ast.parse(source or "")
    except SyntaxError:
        return None


def _annotation_name_for_allowed_surface(annotation: Any) -> str:
    import ast

    if annotation is None:
        return ""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        parts = [annotation.attr]
        value = annotation.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    if isinstance(annotation, ast.Subscript):
        return _annotation_name_for_allowed_surface(annotation.value)
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value
    try:
        return ast.unparse(annotation)
    except Exception:
        return ""


def _self_attribute_types_from_source(source: str) -> dict[str, str]:
    import ast

    tree = _parse_python_tree_for_allowed_surface(source)
    if tree is None:
        return {}

    result: dict[str, str] = {}
    for class_node in getattr(tree, "body", []):
        if not isinstance(class_node, ast.ClassDef):
            continue

        init_node = next(
            (
                child
                for child in class_node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "__init__"
            ),
            None,
        )
        if init_node is None:
            continue

        arg_types = {
            arg.arg: _annotation_name_for_allowed_surface(arg.annotation).rsplit(".", 1)[-1]
            for arg in [
                *init_node.args.posonlyargs,
                *init_node.args.args,
                *init_node.args.kwonlyargs,
            ]
            if arg.arg != "self"
        }

        for node in ast.walk(init_node):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
                continue
            type_name = arg_types.get(node.value.id, "")
            if not type_name:
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    result[target.attr] = type_name

    return result


def _call_display_name_for_allowed_surface(func: Any) -> str:
    import ast

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
    return ""


def _visible_call_paths_from_sources(sources: list[str]) -> list[dict[str, str]]:
    import ast

    calls: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for source in sources:
        tree = _parse_python_tree_for_allowed_surface(source)
        if tree is None:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            display = _call_display_name_for_allowed_surface(node.func)
            if not display or not display.startswith("self."):
                continue

            parts = display.split(".")
            if len(parts) < 3:
                continue

            access_path = ".".join(parts[:-1])
            method_name = parts[-1]
            key = (access_path, method_name)
            if key in seen:
                continue

            seen.add(key)
            calls.append(
                {
                    "access_path": access_path,
                    "method": method_name,
                    "example": display,
                    "line": str(getattr(node, "lineno", "")),
                }
            )

    return calls


def _allowed_method_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(item.get("name") or "").strip(),
        "qualname": str(item.get("qualname") or "").strip(),
        "signature": str(item.get("signature") or "").strip(),
        "file_path": str(item.get("file_path") or "").strip(),
        "origin_qualname": str(item.get("origin_qualname") or "").strip(),
        "relation": "/".join(
            part
            for part in [
                str(item.get("relation_direction") or "").strip(),
                str(item.get("relation_kind") or "").strip(),
                str(item.get("relation_confidence") or "").strip(),
            ]
            if part
        ),
    }


def _build_allowed_api_surface(
    *,
    target_source: str,
    parent_source: str,
    neighbor_sources: list[str],
    related_symbols: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build conservative allowed API surface from visible context only.

    Rules:
    - self.<attr> dependencies are included only when the type is visible in __init__ annotations.
    - methods are included only when visible in related_symbols.
    - nested paths such as self.service.repository are included only when such calls already appear
      in visible source snippets.
    - no guessing.
    """
    sources = [source for source in [target_source, parent_source, *neighbor_sources] if source]

    self_attr_types: dict[str, str] = {}
    for source in sources:
        self_attr_types.update(_self_attribute_types_from_source(source))

    methods_by_type: dict[str, list[dict[str, Any]]] = {}
    free_functions: list[dict[str, Any]] = []

    for raw_item in related_symbols:
        item = dict(raw_item)
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name:
            continue

        if kind == "method":
            parent_type = str(item.get("parent_qualname") or "").rsplit(".", 1)[-1]
            if parent_type:
                methods_by_type.setdefault(parent_type, []).append(_allowed_method_payload(item))
        elif kind == "function":
            free_functions.append(_allowed_method_payload(item))

    dependency_by_path: dict[str, dict[str, Any]] = {}

    for attr_name, type_name in self_attr_types.items():
        allowed_methods = methods_by_type.get(type_name, [])
        if allowed_methods:
            dependency_by_path[f"self.{attr_name}"] = {
                "access_path": f"self.{attr_name}",
                "type_name": type_name,
                "source": "target_or_parent_init",
                "allowed_methods": allowed_methods,
                "origin_examples": [],
            }

    visible_calls = _visible_call_paths_from_sources(sources)
    for call in visible_calls:
        access_path = call["access_path"]
        method_name = call["method"]

        if access_path in dependency_by_path:
            dependency_by_path[access_path]["origin_examples"].append(call)
            continue

        owner_types = [
            type_name
            for type_name, methods in methods_by_type.items()
            if any(method.get("name") == method_name for method in methods)
        ]
        if len(owner_types) != 1:
            continue

        owner_type = owner_types[0]
        dependency_by_path[access_path] = {
            "access_path": access_path,
            "type_name": owner_type,
            "source": "visible_call_path",
            "allowed_methods": methods_by_type.get(owner_type, []),
            "origin_examples": [call],
        }

    return {
        "dependencies": list(dependency_by_path.values()),
        "free_functions": free_functions,
    }




def build_generation_request(
    project_root: Path,
    change_request: ChangeRequest,
    target_qualname: str,
    context_pack: ContextPack,
    config: AppConfig,
    generated_code_artifact: dict[str, Any] | None = None,
    mode: str = 'generate',
    operation: str = 'replace_symbol',
    insert_scope: str | None = None,
) -> dict[str, Any]:
    target = context_pack.target
    full_file_source = (project_root / target.file_path).read_text(encoding='utf-8')
    full_file_included = False
    if mode == 'generate_test':
        full_file_included = bool(config.codegenerator_include_full_file_for_generate_test)
    elif target.kind not in {'function', 'method'}:
        full_file_included = bool(config.codegenerator_include_full_file_for_non_symbol_targets)
    if not full_file_included:
        full_file_source = ''

    target_source = target.source_code
    target_truncated = False

    related_tests: list[dict[str, Any]] = []
    related_test_chars = 0
    related_symbols: list[dict[str, Any]] = []
    related_symbol_chars = 0
    reference_artifacts: list[dict[str, Any]] = []
    reference_chars = 0

    related_test_limits = {
        'generate': config.codegenerator_generate_related_tests_max_items,
        'generate_test': config.codegenerator_generate_test_related_tests_max_items,
        'repair': config.codegenerator_repair_related_tests_max_items,
    }
    related_symbol_limits = {
        'generate': config.codegenerator_generate_related_symbols_max_items,
        'generate_test': config.codegenerator_generate_test_related_symbols_max_items,
        'repair': config.codegenerator_repair_related_symbols_max_items,
    }
    related_symbol_char_limits = {
        'generate': config.codegenerator_generate_related_symbol_chars,
        'generate_test': config.codegenerator_generate_test_related_symbol_chars,
        'repair': config.codegenerator_repair_related_symbol_chars,
    }
    reference_enabled = bool(getattr(config, 'codegenerator_include_reference_artifacts', False))
    reference_limits = {
        'generate': config.codegenerator_generate_reference_max_items if reference_enabled else 0,
        'generate_test': config.codegenerator_generate_test_reference_max_items if reference_enabled else 0,
        'repair': config.codegenerator_repair_reference_max_items if reference_enabled else 0,
    }

    related_test_limit = max(0, int(related_test_limits.get(mode, 0) or 0))
    if related_test_limit > 0:
        related_tests, related_test_chars = _select_related_tests(
            context_pack,
            limit=related_test_limit,
        )

    related_symbol_limit = max(0, int(related_symbol_limits.get(mode, 0) or 0))
    related_symbol_char_limit = max(0, int(related_symbol_char_limits.get(mode, 0) or 0))
    if related_symbol_limit > 0 and related_symbol_char_limit > 0:
        related_symbols, related_symbol_chars = _select_related_symbols(
            context_pack,
            limit=related_symbol_limit,
            source_chars=related_symbol_char_limit,
        )

    all_related_symbols, _all_related_symbol_chars = _select_related_symbols(
        context_pack,
        limit=len(context_pack.related_symbols or []),
        source_chars=related_symbol_char_limit or 700,
    )

    reuse_existing_logic = _normalize_reuse_existing_logic_hint(change_request)
    explicit_related_symbols = _collect_explicit_project_symbol_contexts(
        project_root=project_root,
        change_request=change_request,
        reuse_existing_logic=reuse_existing_logic,
        source_chars=related_symbol_char_limit or 700,
    )
    if explicit_related_symbols:
        related_symbols = _dedupe_related_symbol_payloads([*explicit_related_symbols, *related_symbols])
        all_related_symbols = _dedupe_related_symbol_payloads([*explicit_related_symbols, *all_related_symbols])
        explicit_related_chars = sum(len(str(item.get('source_excerpt') or '')) for item in explicit_related_symbols)
        related_symbol_chars += explicit_related_chars
        LOGGER.info(
            'Explicit project symbols included in generation context: mode=%s count=%s chars=%s',
            mode,
            len(explicit_related_symbols),
            explicit_related_chars,
        )

    selected_reference_items = list(context_pack.reference_artifacts[:max(0, int(reference_limits.get(mode, 0) or 0))])
    LOGGER.info(
        'Reference artifact candidates: count=%s items=%s',
        len(selected_reference_items),
        [
            {
                'artifact_id': item.artifact_id,
                'title': item.title,
                'content_chars': len(item.content or ''),
                'usage_mode': item.usage_mode,
                'content_mode': item.content_mode,
            }
            for item in selected_reference_items
        ],
    )

    include_reference = mode in {'generate', 'generate_test'}
    if include_reference:
        for item in selected_reference_items:
            content = item.content
            content_chars = len(content)
            reference_artifacts.append({
                'artifact_id': item.artifact_id,
                'title': item.title,
                'artifact_type': item.artifact_type,
                'usage_mode': item.usage_mode,
                'content_mode': item.content_mode,
                'why_selected': item.why_selected,
                'source_path': item.source_path,
                'content': content,
                'selected_span': item.selected_span,
                'truncated': False,
            })
            reference_chars += content_chars
            LOGGER.info(
                'Selected reference artifact %s content_chars=%s running_total_chars=%s',
                item.artifact_id,
                content_chars,
                reference_chars,
            )
    else:
        if selected_reference_items:
            LOGGER.info(
                'Skipping reference artifacts for mode=%s by structural policy',
                mode,
            )

    if not reference_artifacts and mode == 'generate':
        LOGGER.info('No reference artifacts selected for request payload')
    if not related_tests:
        LOGGER.info('No related tests selected for request payload')
    if not related_symbols:
        LOGGER.info('No related production symbols selected for request payload')

    estimated_context_chars = len(target_source) + related_test_chars + related_symbol_chars + reference_chars + len(full_file_source)
    LOGGER.info(
        'Context assembly mode=%s estimated_context_chars=%s related_test_limit=%s related_symbol_limit=%s reference_limit=%s related_test_chars=%s related_symbol_chars=%s reference_chars=%s full_file_chars=%s',
        mode,
        estimated_context_chars,
        related_test_limit,
        related_symbol_limit,
        max(0, int(reference_limits.get(mode, 0) or 0)),
        related_test_chars,
        related_symbol_chars,
        reference_chars,
        len(full_file_source),
    )

    normalized_operation = _validate_operation(operation)
    normalized_insert_scope, parent_qualname, expected_new_symbol_kind = _normalize_insert_target_metadata(
        change_request=change_request,
        target=target,
        operation=normalized_operation,
        insert_scope=insert_scope,
    )

    parent_symbol_payload = None
    class_members: list[dict[str, Any]] = []
    if parent_qualname:
        parent_symbol = next((item for item in [target, *context_pack.neighbors] if item.qualname == parent_qualname), None)
        if parent_symbol is not None:
            parent_symbol_payload = {
                'qualname': parent_symbol.qualname,
                'name': parent_symbol.name,
                'kind': parent_symbol.kind,
                'docstring': parent_symbol.docstring,
                'source': parent_symbol.source_code,
            }
        for item in [target, *context_pack.neighbors]:
            if item.parent_qualname == parent_qualname and item.kind == 'method':
                first_line = (item.source_code or '').strip().splitlines()[0] if item.source_code else item.name
                class_members.append({
                    'qualname': item.qualname,
                    'name': item.name,
                    'kind': item.kind,
                    'signature': first_line.strip(),
                    'docstring': item.docstring,
                })

    allowed_api_surface = _build_allowed_api_surface(
        target_source=target.source_code or '',
        parent_source=(parent_symbol_payload or {}).get('source', '') if parent_symbol_payload else '',
        neighbor_sources=[str(item.source_code or '') for item in context_pack.neighbors],
        related_symbols=all_related_symbols,
    )
    raw_related_symbols = _dedupe_related_symbol_payloads([*_raw_related_symbol_payloads(context_pack), *explicit_related_symbols])
    model_surfaces = _build_model_surfaces(raw_related_symbols)
    if model_surfaces:
        LOGGER.info(
            'Model surfaces inferred: mode=%s count=%s models=%s',
            mode,
            len(model_surfaces),
            [item.get('qualname') or item.get('name') for item in model_surfaces],
        )
    contract_attribute_requirements = _build_contract_attribute_requirements(raw_related_symbols)
    if contract_attribute_requirements:
        LOGGER.info(
            'Contract attribute requirements inferred: mode=%s count=%s contracts=%s',
            mode,
            len(contract_attribute_requirements),
            [item.get('contract_qualname') for item in contract_attribute_requirements],
        )
    required_class_members = _build_required_class_members(
        context_pack=context_pack,
        change_request=change_request,
        operation=normalized_operation,
    )
    if required_class_members:
        LOGGER.info(
            'Required class members inferred: mode=%s target=%s count=%s members=%s',
            mode,
            target.qualname,
            len(required_class_members),
            [item.get('name') for item in required_class_members if item.get('required', True)],
        )
    required_contracts = _build_required_contracts(
        change_request=change_request,
        allowed_api_surface=allowed_api_surface,
        related_symbols=all_related_symbols,
    )
    if required_contracts:
        LOGGER.info(
            'Required contracts inferred: mode=%s count=%s contracts=%s',
            mode,
            len(required_contracts),
            [item.get('qualname') or item.get('name') for item in required_contracts],
        )

    same_class_methods = _same_class_methods_from_source(project_root=project_root, target=target)
    if same_class_methods:
        LOGGER.info(
            'Same-class method context inferred: mode=%s target=%s count=%s reuse_mode=%s reuse_contracts=%s',
            mode,
            target.qualname,
            len(same_class_methods),
            reuse_existing_logic.get('mode'),
            [item.get('qualname') for item in (reuse_existing_logic.get('contracts') or [])],
        )

    project_context = {
        'module_outline': [
            {
                'qualname': item.qualname,
                'kind': item.kind,
                'name': item.name,
                'docstring': item.docstring,
            }
            for item in context_pack.neighbors
        ],
        'full_file_source': full_file_source,
        'target_symbol': {
            'qualname': target.qualname,
            'name': target.name,
            'kind': target.kind,
            'docstring': target.docstring,
            'source': target_source,
            'truncated': target_truncated,
        },
        'parent_symbol': parent_symbol_payload,
        'class_members': class_members,
        'related_tests': related_tests,
        'related_symbols': related_symbols,
        'contract_context': {
            'related_symbols': related_symbols,
            'previous_changes': [],
            'contract_attribute_requirements': contract_attribute_requirements,
            'required_contracts': required_contracts,
            'model_surfaces': model_surfaces,
        },
        'contract_attribute_requirements': contract_attribute_requirements,
        'required_contracts': required_contracts,
        'required_class_members': required_class_members,
        'model_surfaces': model_surfaces,
        'allowed_api_surface': allowed_api_surface,
        'same_class_methods': same_class_methods,
        'reuse_existing_logic': reuse_existing_logic,
        'recommended_tests': list(context_pack.recommended_tests),
    }
    context_pack.reference_summary = {
        'count': len(reference_artifacts),
        'titles': [item.get('title', '') for item in reference_artifacts],
        'content_modes': [item.get('content_mode', '') for item in reference_artifacts],
    }
    context_pack.reference_artifacts = list(selected_reference_items[:len(reference_artifacts)])
    reference_context = {
        'reference_artifacts': reference_artifacts,
    }
    request = {
        'request_id': f'generate-{target_qualname.split(".")[-1]}',
        'mode': mode,
        'change_request': {
            'title': change_request.title,
            'description': change_request.description,
            'constraints': list(change_request.constraints),
            'notes': list(change_request.notes),
        },
        'target': {
            'qualname': target_qualname,
            'file_path': target.file_path,
            'operation': normalized_operation,
            'insert_scope': normalized_insert_scope,
            'expected_new_symbol_kind': expected_new_symbol_kind,
            'parent_qualname': parent_qualname,
        },
        'project_context': project_context,
        'reference_context': reference_context,
        'generated_code_artifact': generated_code_artifact or {},
        'options': {
            'generate_test_mode': config.codegenerator_test_generation_mode,
            'required_contracts_count': len(required_contracts),
            'required_class_members_count': len(required_class_members),
        },
    }
    metrics = {
        'target_source_chars': len(target_source),
        'target_source_truncated': target_truncated,
        'full_file_chars': len(full_file_source),
        'full_file_included': full_file_included,
        'related_tests_count': len(related_tests),
        'related_test_chars': related_test_chars,
        'related_symbols_count': len(related_symbols),
        'related_symbol_chars': related_symbol_chars,
        'related_symbol_limit': related_symbol_limit,
        'related_symbol_char_limit': related_symbol_char_limit,
        'reference_artifacts_count': len(reference_artifacts),
        'reference_chars': reference_chars,
        'contract_attribute_requirements_count': len(contract_attribute_requirements),
        'required_contracts_count': len(required_contracts),
        'required_class_members_count': len(required_class_members),
        'request_chars': _json_size(request),
        'estimated_context_chars': estimated_context_chars,
        'related_test_limit': related_test_limit,
        'reference_limit': max(0, int(reference_limits.get(mode, 0) or 0)),
    }
    LOGGER.info(
        'Prepared generation request: mode=%s request_chars=%s target_source_chars=%s full_file_included=%s full_file_chars=%s related_tests=%s related_test_chars=%s related_symbols=%s related_symbol_chars=%s reference_artifacts=%s reference_chars=%s related_test_limit=%s related_symbol_limit=%s reference_limit=%s estimated_context_chars=%s',
        mode,
        metrics['request_chars'],
        metrics['target_source_chars'],
        full_file_included,
        len(full_file_source),
        len(request['project_context']['related_tests']),
        metrics['related_test_chars'],
        len(request['project_context']['contract_context']['related_symbols']),
        metrics['related_symbol_chars'],
        len(request['reference_context']['reference_artifacts']),
        metrics['reference_chars'],
        related_test_limit,
        related_symbol_limit,
        max(0, int(reference_limits.get(mode, 0) or 0)),
        estimated_context_chars,
    )
    return request



def invoke_generate(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    if request_format not in {'json', 'yaml'}:
        raise ValueError(f'Unsupported codegenerator request format: {request_format}')
    request_path = run_dir / f'generation_request.{request_format}'
    result_path = run_dir / 'generation_result.json'
    stdout_path = run_dir / 'codegenerator_stdout.txt'
    stderr_path = run_dir / 'codegenerator_stderr.txt'
    _write_payload(request_path, request_payload, request_format)
    command = [
        config.codegenerator_python,
        '-m',
        'codegenerator',
        'generate',
        '--request-file',
        str(request_path),
        '--config',
        str((codegen_root / config.codegenerator_config_path).resolve()),
    ]
    LOGGER.info('Invoking codegenerator: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if stderr.strip():
        LOGGER.info('codegenerator stderr saved to %s', stderr_path)
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator generate failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator generate returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(
        request_path=str(request_path),
        result_path=str(result_path),
        command=command,
        request_payload=request_payload,
        result_payload=result_payload,
        trace_path=result_payload.get('trace_path'),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )





def invoke_generate_test(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    if request_format not in {'json', 'yaml'}:
        raise ValueError(f'Unsupported codegenerator request format: {request_format}')
    request_path = run_dir / f'generation_test_request.{request_format}'
    result_path = run_dir / 'generation_test_result.json'
    stdout_path = run_dir / 'codegenerator_test_stdout.txt'
    stderr_path = run_dir / 'codegenerator_test_stderr.txt'

    request_payload = dict(request_payload)
    if request_payload.get('mode') != 'generate_test':
        LOGGER.info(
            'Normalizing test-generation request mode from %s to generate_test before sending to codegenerator',
            request_payload.get('mode'),
        )
        request_payload['mode'] = 'generate_test'

    request_chars = _json_size(request_payload)

    LOGGER.info(
        'Prepared generation request for test generation: mode=%s request_chars=%s related_tests=%s related_symbols=%s reference_artifacts=%s',
        request_payload.get('mode'),
        request_chars,
        len(((request_payload.get('project_context') or {}).get('related_tests') or [])),
        len((((request_payload.get('project_context') or {}).get('contract_context') or {}).get('related_symbols') or [])),
        len(((request_payload.get('reference_context') or {}).get('reference_artifacts') or [])),
    )

    _write_payload(request_path, request_payload, request_format)
    command = [
        config.codegenerator_python,
        '-m',
        'codegenerator',
        'generate-test',
        '--request-file',
        str(request_path),
        '--config',
        str((codegen_root / config.codegenerator_config_path).resolve()),
    ]
    LOGGER.info('Invoking codegenerator test generation: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if stderr.strip():
        LOGGER.info('codegenerator test stderr saved to %s', stderr_path)
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator generate-test failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator generate-test returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator generate-test returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(
        request_path=str(request_path),
        result_path=str(result_path),
        command=command,
        request_payload=request_payload,
        result_payload=result_payload,
        trace_path=result_payload.get('trace_path'),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )

def build_repair_request(
    change_request: ChangeRequest,
    target_qualname: str,
    previous_result_payload: dict[str, Any],
    context_pack: ContextPack,
    verification_summary: dict[str, Any],
    config: AppConfig | None = None,
    requested_operation: str = 'replace_symbol',
    insert_scope: str | None = None,
    previous_generation_request: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    target = context_pack.target
    failure_summary = verification_summary.get('failure_summary', {}) if isinstance(verification_summary, dict) else {}
    stage = str(failure_summary.get('stage', 'verification'))
    summary_text = 'Apply failed before verification' if stage == 'apply' else 'Verification failed after apply'
    error_type = 'apply_failed' if stage == 'apply' else 'verification_failed'
    normalized_operation = _validate_operation(requested_operation)

    normalized_insert_scope, parent_qualname, expected_new_symbol_kind = _normalize_insert_target_metadata(
        change_request=change_request,
        target=target,
        operation=normalized_operation,
        insert_scope=insert_scope,
    )

    related_tests_limit = max(
        0,
        int((config.codegenerator_repair_related_tests_max_items if config is not None else 1) or 0),
    )
    repair_reference_limit = max(
        0,
        int((
            config.codegenerator_repair_reference_max_items
            if config is not None and bool(getattr(config, 'codegenerator_include_reference_artifacts', False))
            else 0
        ) or 0),
    )
    related_symbol_limit = max(
        0,
        int((config.codegenerator_repair_related_symbols_max_items if config is not None else 3) or 0),
    )
    related_symbol_char_limit = max(
        0,
        int((config.codegenerator_repair_related_symbol_chars if config is not None else 500) or 0),
    )

    related_tests = _select_related_tests(
        context_pack,
        limit=related_tests_limit,
    )[0]
    related_symbols = _select_related_symbols(
        context_pack,
        limit=related_symbol_limit,
        source_chars=related_symbol_char_limit,
    )[0]

    selected_reference_artifacts = list(context_pack.reference_artifacts[:repair_reference_limit])

    parent_symbol_payload, class_members = _build_parent_context_payload(
        context_pack,
        parent_qualname=parent_qualname,
    )

    full_file_source = ''
    full_file_truncated = False
    if config is not None and bool(config.codegenerator_include_full_file_for_repair):
        full_file_source, full_file_truncated = _truncate_source(
            _same_file_module_source(context_pack),
            max(0, int(config.codegenerator_repair_full_file_chars or 0)),
        )

    all_related_symbols, _all_related_symbol_chars = _select_related_symbols(
        context_pack,
        limit=len(context_pack.related_symbols or []),
        source_chars=related_symbol_char_limit or 700,
    )
    explicit_related_symbols: list[dict[str, Any]] = []
    if project_root is not None:
        reuse_existing_logic_from_request = _normalize_reuse_existing_logic_hint(change_request)
        explicit_related_symbols = _collect_explicit_project_symbol_contexts(
            project_root=project_root,
            change_request=change_request,
            reuse_existing_logic=reuse_existing_logic_from_request,
            source_chars=related_symbol_char_limit or 700,
        )
        if explicit_related_symbols:
            related_symbols = _dedupe_related_symbol_payloads([*explicit_related_symbols, *related_symbols])
            all_related_symbols = _dedupe_related_symbol_payloads([*explicit_related_symbols, *all_related_symbols])
            LOGGER.info(
                'Explicit project symbols included in repair context: count=%s symbols=%s',
                len(explicit_related_symbols),
                [item.get('qualname') for item in explicit_related_symbols],
            )

    allowed_api_surface = _allowed_api_surface_from_previous_request(previous_generation_request)
    allowed_api_surface_source = 'previous_generation_request' if allowed_api_surface else 'rebuilt_from_context_pack'
    if allowed_api_surface is None:
        allowed_api_surface = _build_allowed_api_surface(
            target_source=target.source_code or '',
            parent_source=(parent_symbol_payload or {}).get('source', '') if parent_symbol_payload else '',
            neighbor_sources=[str(item.source_code or '') for item in context_pack.neighbors],
            related_symbols=all_related_symbols,
        )
    raw_related_symbols = _dedupe_related_symbol_payloads([*_raw_related_symbol_payloads(context_pack), *explicit_related_symbols])
    model_surfaces = []
    previous_project_context = (previous_generation_request or {}).get('project_context') if isinstance(previous_generation_request, dict) else {}
    if isinstance(previous_project_context, dict):
        model_surfaces = [
            dict(item)
            for item in (previous_project_context.get('model_surfaces') or [])
            if isinstance(item, dict)
        ]
    if not model_surfaces:
        model_surfaces = _build_model_surfaces(raw_related_symbols)
    if model_surfaces:
        LOGGER.info(
            'Model surfaces inferred for repair: count=%s models=%s',
            len(model_surfaces),
            [item.get('qualname') or item.get('name') for item in model_surfaces],
        )
    contract_attribute_requirements = _build_contract_attribute_requirements(raw_related_symbols)
    required_class_members: list[dict[str, Any]] = []
    if isinstance(previous_project_context, dict):
        required_class_members = [
            dict(item)
            for item in (previous_project_context.get('required_class_members') or [])
            if isinstance(item, dict)
        ]
    if not required_class_members:
        required_class_members = _build_required_class_members(
            context_pack=context_pack,
            change_request=change_request,
            operation=normalized_operation,
        )
    required_contracts = []
    if isinstance(previous_project_context, dict):
        required_contracts = [
            dict(item)
            for item in (previous_project_context.get('required_contracts') or [])
            if isinstance(item, dict)
        ]
    if not required_contracts:
        required_contracts = _build_required_contracts(
            change_request=change_request,
            allowed_api_surface=allowed_api_surface,
            related_symbols=all_related_symbols,
        )

    same_class_methods: list[dict[str, Any]] = []
    reuse_existing_logic: dict[str, Any] = {'mode': 'none', 'confidence': 0.0, 'reason': '', 'contracts': []}
    if isinstance(previous_project_context, dict):
        same_class_methods = [
            dict(item)
            for item in (previous_project_context.get('same_class_methods') or [])
            if isinstance(item, dict)
        ]
        raw_reuse_existing_logic = previous_project_context.get('reuse_existing_logic')
        if isinstance(raw_reuse_existing_logic, dict):
            reuse_existing_logic = dict(raw_reuse_existing_logic)
            reuse_existing_logic.setdefault('mode', 'none')
            reuse_existing_logic.setdefault('confidence', 0.0)
            reuse_existing_logic.setdefault('reason', '')
            if not isinstance(reuse_existing_logic.get('contracts'), list):
                reuse_existing_logic['contracts'] = []
    if same_class_methods:
        LOGGER.info(
            'Same-class method context reused for repair: target=%s count=%s reuse_mode=%s reuse_contracts=%s',
            target_qualname,
            len(same_class_methods),
            reuse_existing_logic.get('mode'),
            [item.get('qualname') for item in (reuse_existing_logic.get('contracts') or [])],
        )

    previous_artifact = dict(previous_result_payload.get('code_artifact') or {})
    previous_artifact['operation'] = normalized_operation
    previous_artifact.setdefault('target_qualname', target_qualname)
    previous_artifact.setdefault('target_file', target.file_path)
    if normalized_operation == 'insert_after_symbol':
        previous_artifact['insert_after'] = previous_artifact.get('insert_after') or target_qualname
        previous_artifact['insert_scope'] = normalized_insert_scope
        if expected_new_symbol_kind:
            previous_artifact['expected_new_symbol_kind'] = expected_new_symbol_kind
        if parent_qualname:
            previous_artifact['parent_qualname'] = parent_qualname
    else:
        previous_artifact['insert_scope'] = previous_artifact.get('insert_scope') or normalized_insert_scope

    LOGGER.info(
        'Prepared repair request: target=%s requested_operation=%s stage=%s related_tests=%s related_symbols=%s reference_artifacts=%s previous_artifact_has_code=%s full_file_chars=%s full_file_truncated=%s allowed_surface_source=%s dependencies=%s free_functions=%s',
        target_qualname,
        normalized_operation,
        stage,
        len(related_tests),
        len(related_symbols),
        len(selected_reference_artifacts),
        bool(previous_artifact.get('code')),
        len(full_file_source),
        full_file_truncated,
        allowed_api_surface_source,
        len(allowed_api_surface.get('dependencies') or []),
        len(allowed_api_surface.get('free_functions') or []),
    )

    return {
        'request_id': f'repair-{target_qualname.split(".")[-1]}',
        'mode': 'repair',
        'previous_generation_request_id': previous_result_payload.get('request_id', ''),
        'change_request': {
            'title': change_request.title,
            'description': change_request.description,
            'constraints': list(change_request.constraints),
            'notes': list(change_request.notes),
        },
        'target': {
            'qualname': target_qualname,
            'file_path': target.file_path,
            'operation': normalized_operation,
            'insert_scope': normalized_insert_scope,
            'expected_new_symbol_kind': expected_new_symbol_kind,
            'parent_qualname': parent_qualname,
        },        
        'error_context': {
            'type': error_type,
            'summary': summary_text,
            'verification_summary': verification_summary,
        },
        'previous_artifact': previous_artifact,
        'project_context': {
            'module_outline': [
                {
                    'qualname': item.qualname,
                    'kind': item.kind,
                    'name': item.name,
                    'docstring': item.docstring,
                }
                for item in context_pack.neighbors
            ],
            'full_file_source': full_file_source,
            'full_file_truncated': full_file_truncated,
            'target_symbol': {
                'qualname': target.qualname,
                'name': target.name,
                'kind': target.kind,
                'docstring': target.docstring,
                'source': target.source_code,
                'truncated': False,
            },
            'parent_symbol': parent_symbol_payload,
            'class_members': class_members,
            'related_tests': related_tests,
            'related_symbols': related_symbols,
            'contract_context': {
                'related_symbols': related_symbols,
                'previous_changes': [],
                'contract_attribute_requirements': contract_attribute_requirements,
                'required_contracts': required_contracts,
                'model_surfaces': model_surfaces,
            },
            'contract_attribute_requirements': contract_attribute_requirements,
            'required_contracts': required_contracts,
            'required_class_members': required_class_members,
            'model_surfaces': model_surfaces,
            'allowed_api_surface': allowed_api_surface,
            'same_class_methods': same_class_methods,
            'reuse_existing_logic': reuse_existing_logic,
            'recommended_tests': list(context_pack.recommended_tests),
        },
        'reference_context': {
            'reference_summary': {
                'count': len(selected_reference_artifacts),
                'titles': [item.title for item in selected_reference_artifacts],
                'content_modes': [item.content_mode for item in selected_reference_artifacts],
            },
            'reference_artifacts': [
                {
                    'artifact_id': item.artifact_id,
                    'title': item.title,
                    'artifact_type': item.artifact_type,
                    'usage_mode': item.usage_mode,
                    'content_mode': item.content_mode,
                    'why_selected': item.why_selected,
                    'source_path': item.source_path,
                    'content': item.content,
                    'selected_span': item.selected_span,
                    'truncated': False,
                }
                for item in selected_reference_artifacts
            ],
        },
        'options': {
            'requested_operation': normalized_operation,
            'insert_scope': normalized_insert_scope,
            'expected_new_symbol_kind': expected_new_symbol_kind,
            'parent_qualname': parent_qualname,
            'allowed_api_surface_source': allowed_api_surface_source,
            'allowed_api_surface_dependencies_count': len(allowed_api_surface.get('dependencies') or []),
            'allowed_api_surface_free_functions_count': len(allowed_api_surface.get('free_functions') or []),
            'contract_attribute_requirements_count': len(contract_attribute_requirements),
            'required_contracts_count': len(required_contracts),
            'required_class_members_count': len(required_class_members),
            'model_surfaces_count': len(model_surfaces),
            'full_file_included': bool(full_file_source),
            'full_file_truncated': full_file_truncated,
        },
    }


def invoke_repair(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    request_path = run_dir / f'repair_request.{request_format}'
    result_path = run_dir / 'repair_result.json'
    stdout_path = run_dir / 'codegenerator_repair_stdout.txt'
    stderr_path = run_dir / 'codegenerator_repair_stderr.txt'
    _write_payload(request_path, request_payload, request_format)
    command = [config.codegenerator_python, '-m', 'codegenerator', 'repair', '--request-file', str(request_path), '--config', str((codegen_root / config.codegenerator_config_path).resolve())]
    LOGGER.info('Invoking codegenerator repair: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator repair failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator repair returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator repair returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info('codegenerator usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs', llm_usage.get('prompt_tokens'), llm_usage.get('output_tokens'), llm_usage.get('total_tokens'), llm_usage.get('calls'), float(llm_usage.get('total_duration_sec', 0.0) or 0.0))
    return CodeGeneratorCallResult(request_path=str(request_path), result_path=str(result_path), command=command, request_payload=request_payload, result_payload=result_payload, trace_path=result_payload.get('trace_path'), stdout_path=str(stdout_path), stderr_path=str(stderr_path))


def patch_artifact_from_result(result_payload: dict[str, Any], fallback_target_qualname: str) -> PatchArtifact:
    status = result_payload.get('status')
    if status != 'ok':
        planner_result = result_payload.get('planner_result') or {}
        reason = (
            result_payload.get('message')
            or planner_result.get('reason')
            or result_payload.get('error_type')
            or status
        )
        next_step = planner_result.get('suggested_next_step')
        if next_step and str(next_step) not in str(reason):
            reason = f'{reason}. Suggested next step: {next_step}'
        raise ValueError(f"codegenerator returned non-ok status: {status} ({reason})")

    artifact = result_payload.get('code_artifact') or {}
    operation = _validate_operation(str(artifact.get('operation', 'replace_symbol')))
    code = artifact.get('code')
    if not code:
        raise ValueError('codegenerator result does not contain code_artifact.code')
    return PatchArtifact(
        target_qualname=str(artifact.get('target_qualname') or fallback_target_qualname),
        replacement_code=str(code),
        operation=operation,
        insert_scope=str(artifact.get('insert_scope') or '') or None,
        expected_new_symbol_kind=str(artifact.get('expected_new_symbol_kind') or '') or None,
        parent_qualname=str(artifact.get('parent_qualname') or '') or None,
        import_changes=list(artifact.get('import_changes') or []),
    )

_ALLOWED_OPERATIONS = {'replace_symbol', 'insert_after_symbol'}

def _validate_operation(operation: str) -> str:
    value = operation.strip().lower()
    if value not in _ALLOWED_OPERATIONS:
        raise ValueError(f'Unsupported patch operation from codegenerator: {operation}')
    return value

def ensure_expected_operation(
    result_payload: dict[str, Any],
    expected_operation: str,
) -> str:
    artifact = result_payload.get('code_artifact') or {}
    actual_operation = _validate_operation(str(artifact.get('operation', 'replace_symbol')))
    expected = _validate_operation(expected_operation)
    if actual_operation != expected:
        raise ValueError(
            f'Generator returned operation {actual_operation}, expected {expected}'
        )
    return actual_operation

def _write_payload(path: Path, payload: dict[str, Any], request_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if request_format == 'yaml':
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding='utf-8')
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def invoke_generated_test_failure_review(run_dir: Path, config: AppConfig, request_payload: dict[str, Any]) -> CodeGeneratorCallResult:
    """Invoke codegenerator advisory review for generated-test-only failures."""
    codegen_root = Path(config.codegenerator_root_dir).resolve()
    request_format = config.codegenerator_request_format.lower()
    if request_format not in {'json', 'yaml'}:
        raise ValueError(f'Unsupported codegenerator request format: {request_format}')
    request_path = run_dir / f'generated_test_review_request.{request_format}'
    result_path = run_dir / 'generated_test_review_result.json'
    stdout_path = run_dir / 'codegenerator_generated_test_review_stdout.txt'
    stderr_path = run_dir / 'codegenerator_generated_test_review_stderr.txt'
    _write_payload(request_path, request_payload, request_format)
    command = [
        config.codegenerator_python,
        '-m',
        'codegenerator',
        'review-generated-test-failure',
        '--request-file',
        str(request_path),
        '--config',
        str((codegen_root / config.codegenerator_config_path).resolve()),
    ]
    LOGGER.info('Invoking codegenerator generated-test failure review: %s', ' '.join(command))
    completed = subprocess.run(command, cwd=str(codegen_root), capture_output=True, text=True)
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    stdout_path.write_text(stdout, encoding='utf-8')
    stderr_path.write_text(stderr, encoding='utf-8')
    if stderr.strip():
        LOGGER.info('codegenerator generated-test review stderr saved to %s', stderr_path)
    if completed.returncode != 0:
        raise RuntimeError(f'codegenerator generated-test review failed with exit code {completed.returncode}. stdout={stdout_path} stderr={stderr_path}')
    if not stdout.strip():
        raise RuntimeError(f'codegenerator generated-test review returned empty stdout. stderr={stderr_path}')
    try:
        result_payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'codegenerator generated-test review returned invalid JSON on stdout: {exc}. stdout={stdout_path} stderr={stderr_path}') from exc
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    llm_usage = result_payload.get('llm_usage') or {}
    if llm_usage:
        LOGGER.info(
            'codegenerator generated-test review usage prompt_tokens=%s output_tokens=%s total_tokens=%s calls=%s total_duration=%.2fs',
            llm_usage.get('prompt_tokens'),
            llm_usage.get('output_tokens'),
            llm_usage.get('total_tokens'),
            llm_usage.get('calls'),
            float(llm_usage.get('total_duration_sec', 0.0) or 0.0),
        )
    return CodeGeneratorCallResult(
        request_path=str(request_path),
        result_path=str(result_path),
        command=command,
        request_payload=request_payload,
        result_payload=result_payload,
        trace_path=result_payload.get('trace_path'),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )
