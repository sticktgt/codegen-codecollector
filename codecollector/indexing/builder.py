from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from codecollector.domain.models import RelationRecord, SymbolRecord
from codecollector.indexing.base import IndexStore
from codecollector.logger import get_logger

IGNORE_DIR_NAMES = {'.git', '.venv', '__pycache__', '.mypy_cache', '.pytest_cache', '.codecollector', '.workspaces'}
LOGGER = get_logger(__name__)


@dataclass(slots=True)
class BuildReport:
    indexed_files: int
    unchanged_files: int
    deleted_files: int
    full_rebuild: bool
    initial_build: bool = False
    known_files_before_build: int = 0
    graph_indexing_ms: int = 0
    search_documents_sync_ms: int = 0
    vector_index_sync_ms: int = 0
    search_documents_count: int = 0
    search_documents_changed: bool = False
    reference_documents_count: int = 0
    reference_documents_changed: bool = False
    reference_sync_ms: int = 0
    reference_vector_sync_ms: int = 0


class PythonIndexBuilder:
    def __init__(self, project_root: Path, store: IndexStore) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.project_key = str(self.project_root)

    def build(self, full_rebuild: bool = False) -> BuildReport:
        LOGGER.info('Index build started for %s (full_rebuild=%s)', self.project_root, full_rebuild)
        known = {} if full_rebuild else self.store.get_known_files(self.project_key)
        known_files_before_build = len(known)
        current_files = self._scan_python_files()
        indexed_files = 0
        unchanged_files = 0

        for file_path in current_files:
            if self._reindex_single_file(file_path, known):
                indexed_files += 1
            else:
                unchanged_files += 1

        deleted_files = 0
        current_rel_paths = {str(path.relative_to(self.project_root)) for path in current_files}
        for stale_path in set(known) - current_rel_paths:
            self.store.delete_file(self.project_key, stale_path)
            deleted_files += 1

        report = BuildReport(
            indexed_files=indexed_files,
            unchanged_files=unchanged_files,
            deleted_files=deleted_files,
            full_rebuild=full_rebuild,
            initial_build=(not full_rebuild and known_files_before_build == 0 and indexed_files > 0),
            known_files_before_build=known_files_before_build,
        )
        LOGGER.info(
            'Index build finished for %s: indexed=%s unchanged=%s deleted=%s',
            self.project_root,
            report.indexed_files,
            report.unchanged_files,
            report.deleted_files,
        )
        return report

    def build_changed_files(self, changed_files: list[Path]) -> BuildReport:
        LOGGER.info('Targeted reindex started for %s files in %s', len(changed_files), self.project_root)
        known = self.store.get_known_files(self.project_key)
        indexed_files = 0
        unchanged_files = 0

        for file_path in sorted({path.resolve() for path in changed_files}):
            if not file_path.exists() or file_path.suffix != '.py':
                continue
            if self._reindex_single_file(file_path, known):
                indexed_files += 1
            else:
                unchanged_files += 1

        report = BuildReport(
            indexed_files=indexed_files,
            unchanged_files=unchanged_files,
            deleted_files=0,
            full_rebuild=False,
            initial_build=False,
            known_files_before_build=len(known),
        )
        LOGGER.info(
            'Targeted reindex finished for %s: indexed=%s unchanged=%s',
            self.project_root,
            report.indexed_files,
            report.unchanged_files,
        )
        return report

    def _reindex_single_file(self, file_path: Path, known: dict[str, str]) -> bool:
        relative_path = str(file_path.relative_to(self.project_root))
        file_hash = self._sha256(file_path)
        if known.get(relative_path) == file_hash:
            return False
        module_name = self._module_name(relative_path)
        symbols, relations = self._extract_file(relative_path, file_path, module_name)
        self.store.replace_file_contents(
            self.project_key,
            relative_path,
            file_hash,
            module_name,
            symbols,
            relations,
        )
        return True

    def _scan_python_files(self) -> list[Path]:
        result: list[Path] = []
        for path in self.project_root.rglob('*.py'):
            relative_parts = path.relative_to(self.project_root).parts
            if any(part in IGNORE_DIR_NAMES for part in relative_parts):
                continue
            result.append(path)
        return sorted(result)

    def _extract_file(self, relative_path: str, file_path: Path, module_name: str) -> tuple[list[SymbolRecord], list[RelationRecord]]:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        lines = source.splitlines()
        symbols: list[SymbolRecord] = []
        relations: list[RelationRecord] = []

        module_symbol = SymbolRecord(
            file_path=relative_path,
            module_name=module_name,
            name=module_name.split('.')[-1],
            qualname=module_name,
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=max(1, len(lines)),
            docstring=ast.get_docstring(tree) or '',
            source_code=source,
        )
        symbols.append(module_symbol)

        imports = self._collect_imports(tree, module_name)
        for alias_name, resolved_target in imports.items():
            relations.append(RelationRecord(
                source_qualname=module_name,
                relation_kind='imports',
                target_ref=alias_name,
                target_qualname=resolved_target,
                file_path=relative_path,
            ))

        top_level_functions: dict[str, str] = {}
        class_method_qualnames: dict[str, dict[str, str]] = {}
        class_method_nodes: list[tuple[str, ast.FunctionDef, str, dict[str, str]]] = []
        function_nodes: list[tuple[str, ast.FunctionDef]] = []

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_symbol = self._symbol_from_node(
                    relative_path, module_name, node, 'class', parent_qualname=module_name, lines=lines
                )
                symbols.append(class_symbol)
                relations.append(RelationRecord(
                    source_qualname=module_name,
                    relation_kind='contains',
                    target_ref=class_symbol.name,
                    target_qualname=class_symbol.qualname,
                    file_path=relative_path,
                ))

                method_map: dict[str, str] = {}
                init_attr_types: dict[str, str] = {}
                for child in node.body:
                    if isinstance(child, ast.FunctionDef):
                        method_symbol = self._symbol_from_node(
                            relative_path,
                            module_name,
                            child,
                            'method',
                            parent_qualname=class_symbol.qualname,
                            lines=lines,
                            qualname=f'{module_name}.{node.name}.{child.name}',
                        )
                        symbols.append(method_symbol)
                        relations.append(RelationRecord(
                            source_qualname=class_symbol.qualname,
                            relation_kind='contains',
                            target_ref=method_symbol.name,
                            target_qualname=method_symbol.qualname,
                            file_path=relative_path,
                        ))
                        method_map[child.name] = method_symbol.qualname
                        if child.name == '__init__':
                            init_attr_types = self._collect_self_attr_types(child, imports, module_name)
                        class_method_nodes.append((class_symbol.qualname, child, relative_path, init_attr_types))
                class_method_qualnames[class_symbol.qualname] = method_map
            elif isinstance(node, ast.FunctionDef):
                function_symbol = self._symbol_from_node(
                    relative_path, module_name, node, 'function', parent_qualname=module_name, lines=lines
                )
                symbols.append(function_symbol)
                relations.append(RelationRecord(
                    source_qualname=module_name,
                    relation_kind='contains',
                    target_ref=function_symbol.name,
                    target_qualname=function_symbol.qualname,
                    file_path=relative_path,
                ))
                top_level_functions[node.name] = function_symbol.qualname
                function_nodes.append((function_symbol.qualname, node))

        for source_qualname, node in function_nodes:
            relations.extend(
                self._extract_call_relations(
                    source_qualname=source_qualname,
                    node=node,
                    file_path=relative_path,
                    module_name=module_name,
                    imports=imports,
                    module_functions=top_level_functions,
                    class_method_map={},
                    self_attr_types={},
                    local_var_types=self._collect_local_var_types(node, imports, module_name),
                )
            )

        for class_qualname, node, rel_path, self_attr_types in class_method_nodes:
            relations.extend(
                self._extract_call_relations(
                    source_qualname=f'{class_qualname}.{node.name}',
                    node=node,
                    file_path=rel_path,
                    module_name=module_name,
                    imports=imports,
                    module_functions=top_level_functions,
                    class_method_map=class_method_qualnames.get(class_qualname, {}),
                    self_attr_types=self_attr_types,
                    local_var_types=self._collect_local_var_types(node, imports, module_name),
                )
            )

        return symbols, self._dedupe_relations(relations)

    def _collect_imports(self, tree: ast.Module, module_name: str) -> dict[str, str]:
        imports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    alias_name = alias.asname or alias.name.split('.')[-1]
                    imports[alias_name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                base_module = self._resolve_from_module(module_name, node.module, node.level)
                for alias in node.names:
                    if alias.name == '*':
                        continue
                    alias_name = alias.asname or alias.name
                    imports[alias_name] = f'{base_module}.{alias.name}' if base_module else alias.name
        return imports

    def _resolve_from_module(self, module_name: str, imported_module: str | None, level: int) -> str:
        if level <= 0:
            return imported_module or ''
        base_parts = module_name.split('.')
        keep = max(0, len(base_parts) - level)
        prefix = '.'.join(base_parts[:keep])
        if imported_module:
            return f'{prefix}.{imported_module}'.strip('.')
        return prefix

    def _collect_local_var_types(self, function_node: ast.FunctionDef, imports: dict[str, str], module_name: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in ast.walk(function_node):
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                resolved = self._resolve_annotation(child.annotation, imports, module_name)
                if resolved:
                    result[child.target.id] = resolved
            elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                target_name = child.targets[0].id
                value = child.value
                if isinstance(value, ast.Call):
                    if isinstance(value.func, ast.Name):
                        resolved = imports.get(value.func.id) or f'{module_name}.{value.func.id}'
                        result[target_name] = resolved
                    elif isinstance(value.func, ast.Attribute):
                        dotted = self._attribute_to_dotted(value.func)
                        if dotted and dotted.split('.')[0] in imports:
                            base = dotted.split('.')[0]
                            result[target_name] = dotted.replace(base, imports[base], 1)
                elif isinstance(value, ast.Name) and value.id in imports:
                    result[target_name] = imports[value.id]
        return result

    def _collect_self_attr_types(self, init_node: ast.FunctionDef, imports: dict[str, str], module_name: str) -> dict[str, str]:
        param_types: dict[str, str] = {}
        for arg in init_node.args.args:
            if arg.arg == 'self':
                continue
            if arg.annotation is not None:
                resolved = self._resolve_annotation(arg.annotation, imports, module_name)
                if resolved:
                    param_types[arg.arg] = resolved

        result: dict[str, str] = {}
        for child in ast.walk(init_node):
            if isinstance(child, ast.Assign) and len(child.targets) == 1:
                target = child.targets[0]
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == 'self'
                    and isinstance(child.value, ast.Name)
                ):
                    param_name = child.value.id
                    if param_name in param_types:
                        result[target.attr] = param_types[param_name]
        return result

    def _resolve_annotation(self, annotation: ast.AST, imports: dict[str, str], module_name: str) -> str | None:
        if isinstance(annotation, ast.Name):
            if annotation.id in imports:
                return imports[annotation.id]
            return f'{module_name}.{annotation.id}'
        if isinstance(annotation, ast.Attribute):
            dotted = self._attribute_to_dotted(annotation)
            if dotted:
                base_name = dotted.split('.')[0]
                if base_name in imports:
                    return dotted.replace(base_name, imports[base_name], 1)
                return dotted
        if isinstance(annotation, ast.Subscript):
            return self._resolve_annotation(annotation.value, imports, module_name)
        return None

    def _extract_call_relations(
        self,
        source_qualname: str,
        node: ast.FunctionDef,
        file_path: str,
        module_name: str,
        imports: dict[str, str],
        module_functions: dict[str, str],
        class_method_map: dict[str, str],
        self_attr_types: dict[str, str],
        local_var_types: dict[str, str],
    ) -> list[RelationRecord]:
        relations: list[RelationRecord] = []
        is_test_source = file_path.startswith('tests/') or '/tests/' in f'/{file_path}'
        is_controller_source = '.api.' in source_qualname

        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target_ref, target_qualname, relation_confidence = self._resolve_call_target(
                    child.func,
                    module_name=module_name,
                    imports=imports,
                    module_functions=module_functions,
                    class_method_map=class_method_map,
                    self_attr_types=self_attr_types,
                    local_var_types=local_var_types,
                )
                if not target_ref:
                    continue
                relations.append(RelationRecord(
                    source_qualname=source_qualname,
                    relation_kind='calls',
                    target_ref=target_ref,
                    target_qualname=target_qualname,
                    file_path=file_path,
                    relation_confidence=relation_confidence,
                ))
                if is_test_source and target_qualname:
                    relations.append(RelationRecord(
                        source_qualname=source_qualname,
                        relation_kind='covered_by_test',
                        target_ref=target_ref,
                        target_qualname=target_qualname,
                        file_path=file_path,
                        relation_confidence=relation_confidence,
                    ))
                if is_controller_source and target_qualname and '.services.' in target_qualname:
                    relations.append(RelationRecord(
                        source_qualname=source_qualname,
                        relation_kind='exposed_by_controller',
                        target_ref=target_ref,
                        target_qualname=target_qualname,
                        file_path=file_path,
                        relation_confidence=relation_confidence,
                    ))
        return relations

    def _resolve_call_target(
        self,
        node: ast.AST,
        module_name: str,
        imports: dict[str, str],
        module_functions: dict[str, str],
        class_method_map: dict[str, str],
        self_attr_types: dict[str, str],
        local_var_types: dict[str, str],
    ) -> tuple[str | None, str | None, str]:
        if isinstance(node, ast.Name):
            if node.id in imports:
                return node.id, imports[node.id], 'high'
            if node.id in module_functions:
                return node.id, module_functions[node.id], 'high'
            return node.id, None, 'low'

        if isinstance(node, ast.Attribute):
            dotted = self._attribute_to_dotted(node)
            if not dotted:
                return None, None, 'low'
            parts = dotted.split('.')
            target_ref = parts[-1]

            if parts[0] == 'self':
                if len(parts) == 2 and parts[1] in class_method_map:
                    return target_ref, class_method_map[parts[1]], 'high'
                if len(parts) >= 3:
                    owner_attr = parts[1]
                    owner_type = self_attr_types.get(owner_attr)
                    if owner_type:
                        return target_ref, f"{owner_type}.{parts[-1]}", 'high'
                return target_ref, None, 'low'

            if parts[0] in imports:
                imported = imports[parts[0]]
                return target_ref, '.'.join([imported] + parts[1:]), 'high'
            if parts[0] in local_var_types:
                owner = local_var_types[parts[0]]
                return target_ref, '.'.join([owner] + parts[1:]), 'medium'

            return target_ref, None, 'low'

        return None, None, 'low'

    def _attribute_to_dotted(self, node: ast.AST) -> str | None:
        parts: list[str] = []
        current: ast.AST | None = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return '.'.join(reversed(parts))
        return None

    def _symbol_from_node(
        self,
        relative_path: str,
        module_name: str,
        node: ast.AST,
        kind: str,
        parent_qualname: str | None,
        lines: list[str],
        qualname: str | None = None,
    ) -> SymbolRecord:
        assert hasattr(node, 'name')
        start = getattr(node, 'lineno', 1)
        end = getattr(node, 'end_lineno', start)
        source_code = '\n'.join(lines[start - 1:end])
        return SymbolRecord(
            file_path=relative_path,
            module_name=module_name,
            name=getattr(node, 'name'),
            qualname=qualname or f'{module_name}.{getattr(node, "name")}',
            kind=kind,
            parent_qualname=parent_qualname,
            start_line=start,
            end_line=end,
            docstring=ast.get_docstring(node) or '',
            source_code=source_code,
        )

    def _dedupe_relations(self, relations: list[RelationRecord]) -> list[RelationRecord]:
        seen: set[tuple[str, str, str, str | None, str, str]] = set()
        result: list[RelationRecord] = []
        for relation in relations:
            key = (
                relation.source_qualname,
                relation.relation_kind,
                relation.target_ref,
                relation.target_qualname,
                relation.relation_source,
                relation.relation_confidence,
            )
            if key not in seen:
                seen.add(key)
                result.append(relation)
        return result

    def _sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _module_name(self, relative_path: str) -> str:
        return relative_path[:-3].replace('/', '.')
