from __future__ import annotations

import ast
import hashlib
import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

from codecollector.config import AppConfig, load_config
from codecollector.domain.models import ApplyResult, ImpactSummary, PatchArtifact, SymbolRecord
from codecollector.indexing.builder import PythonIndexBuilder
from codecollector.indexing.base import IndexStore
from codecollector.indexing.storage_factory import create_index_store
from codecollector.logger import get_logger
from codecollector.overlays.service import OverlayService
from codecollector.validation.service import ValidationService
from codecollector.workspace.diff_service import DiffService
from codecollector.workspace.staging_manager import StagingManager

LOGGER = get_logger(__name__)

WORKSPACE_BASELINE_FILENAME = '.codecollector_workspace_baseline.json'


class ApplyService:
    def __init__(self, tool_root: Path, config: AppConfig | None = None, overlay_dirname: str = '.codecollector', workspace_root_dirname: str = '.workspaces') -> None:
        self.tool_root = tool_root.resolve()
        self.config = config or load_config()
        self.overlay_dirname = overlay_dirname
        self.staging = StagingManager(self.tool_root, workspace_root_dirname=workspace_root_dirname)
        self.diff_service = DiffService()

    def apply_artifact(self, source_project: Path, artifact: PatchArtifact, generated_tests: list[dict[str, str]] | None = None) -> ApplyResult:
        source_project = source_project.resolve()
        workspace = self.staging.create_workspace(source_project)
        try:
            project_key = str(workspace)
            store = create_index_store(workspace, self.config)
            overlays = OverlayService(workspace, overlay_dirname=self.overlay_dirname)
            builder = PythonIndexBuilder(workspace, store)
            builder.build(full_rebuild=True)
            store.replace_knowledge_relations(project_key, overlays.knowledge_relations())

            symbol = store.get_symbol(project_key, artifact.target_qualname)
            if symbol is None:
                raise ValueError(f'Unknown qualname in workspace: {artifact.target_qualname}')

            target_path = workspace / symbol.file_path
            before_path = source_project / symbol.file_path
            baseline_rel_paths = [symbol.file_path]
            for test_artifact in generated_tests or []:
                baseline_rel_paths.append(str(test_artifact['file_path']))
            self._write_workspace_baseline(workspace, source_project, baseline_rel_paths)

            self._apply_operation_to_file(target_path, symbol, artifact)
            changed_files = [target_path]
            for test_artifact in generated_tests or []:
                test_path = workspace / str(test_artifact['file_path'])
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_source = str(test_artifact['source_code']).rstrip('\n') + '\n'
                ast.parse(test_source)
                test_path.write_text(test_source, encoding='utf-8')
                changed_files.append(test_path)
            validation = ValidationService().validate_project(workspace, changed_files=changed_files)

            reindexed = False
            if validation.is_valid:
                LOGGER.info('Targeted reindex of changed workspace files after successful validation: %s', workspace)
                reindex_report = builder.build_changed_files(changed_files)
                store.replace_knowledge_relations(project_key, overlays.knowledge_relations())
                reindexed = reindex_report.indexed_files > 0

            diff = self.diff_service.summarize_file_change(before_path, target_path)
            impact = self._build_impact_summary(
                project_key,
                store,
                overlays,
                artifact.target_qualname,
                changed_files=[str(path.relative_to(workspace)) for path in changed_files],
                validation_ok=validation.is_valid,
                before_primary_file=before_path,
                after_primary_file=target_path,
                target_module_name=symbol.module_name,
                operation=artifact.operation,
            )
            return ApplyResult(workspace, artifact, diff, validation, reindexed, impact)
        except Exception:
            try:
                self.staging.delete_workspace(workspace)
            except Exception:
                LOGGER.exception('Failed to delete staging workspace after apply failure: %s', workspace)
            raise


    def _write_workspace_baseline(self, workspace: Path, source_project: Path, rel_paths: list[str]) -> Path:
        """Store base hashes for files that this workspace is expected to change.

        The baseline is written inside the workspace but is explicitly ignored by
        workspace apply. It is used later to prevent applying an old workspace
        over project files that were changed by another CR after the workspace was
        created.
        """
        files: dict[str, dict[str, object]] = {}
        for raw_rel in rel_paths:
            rel = str(raw_rel or '').strip().replace('\\', '/')
            while rel.startswith('./'):
                rel = rel[2:]
            if not rel or rel in files:
                continue
            files[rel] = self._baseline_entry(source_project / rel)

        payload = {
            'version': 1,
            'created_at': datetime.now(tz=UTC).isoformat(),
            'source_project': str(source_project),
            'files': files,
        }
        path = workspace / WORKSPACE_BASELINE_FILENAME
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        LOGGER.info('Wrote workspace baseline metadata: %s files=%s', path, len(files))
        return path

    def _baseline_entry(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {'exists': False, 'sha256': None, 'size': None, 'mtime_ns': None}
        if not path.is_file():
            return {'exists': False, 'sha256': None, 'size': None, 'mtime_ns': None}
        stat = path.stat()
        return {
            'exists': True,
            'sha256': self._sha256_file(path),
            'size': int(stat.st_size),
            'mtime_ns': int(stat.st_mtime_ns),
        }

    def _sha256_file(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                hasher.update(chunk)
        return hasher.hexdigest()

    def _apply_operation_to_file(self, target_path: Path, symbol: SymbolRecord, artifact: PatchArtifact) -> None:
        LOGGER.info('Applying %s for %s in %s', artifact.operation, symbol.qualname, target_path)
        source = target_path.read_text(encoding='utf-8')
        lines = source.splitlines()

        payload_lines = self._normalize_payload_lines(artifact.replacement_code)

        if artifact.operation == 'replace_symbol':
            start = symbol.start_line - 1
            end = symbol.end_line
            payload_lines = self._normalize_replace_symbol_lines(lines, symbol, payload_lines)
            lines[start:end] = payload_lines
        elif artifact.operation == 'insert_after_symbol':
            if artifact.insert_scope == 'class_body':
                payload_lines = self._normalize_class_body_method_lines(lines, symbol, payload_lines)
            insert_at = symbol.end_line
            lines[insert_at:insert_at] = self._with_spacing_before_insert(lines, insert_at, payload_lines)
        else:
            raise ValueError(f'Unsupported patch operation: {artifact.operation}')

        updated_source = '\n'.join(lines).rstrip('\n') + '\n'
        if artifact.import_changes:
            updated_source = self._apply_import_changes(updated_source, artifact.import_changes)
        ast.parse(updated_source)
        self._assert_target_structure_preserved(updated_source, symbol, artifact)
        target_path.write_text(updated_source, encoding='utf-8')

    def _apply_import_changes(self, source: str, import_changes: list[dict]) -> str:
        if not import_changes:
            return source

        updated_source = source
        for item in import_changes:
            if not isinstance(item, dict):
                continue
            action = str(item.get('action') or '').strip()
            if action in {'remove_import', 'remove_from_import'}:
                updated_source = self._apply_remove_import_change(updated_source, item)

        used_names = self._loaded_names(updated_source)
        lines = updated_source.splitlines()
        existing = {line.strip() for line in lines}
        new_imports: list[str] = []
        for item in import_changes:
            if not isinstance(item, dict):
                continue
            action = str(item.get('action') or '').strip()
            module = str(item.get('module') or '').strip()
            if not module:
                continue
            if action == 'add_import':
                alias = str(item.get('alias') or '').strip()
                imported_name = alias or module.split('.', 1)[0]
                if imported_name and imported_name not in used_names:
                    continue
                line = f'import {module}' + (f' as {alias}' if alias else '')
            elif action == 'add_from_import':
                names = [str(name).strip() for name in (item.get('names') or []) if str(name).strip()]
                if not names:
                    continue
                used_items = [name for name in names if self._from_import_binding_name(name) in used_names]
                if not used_items:
                    continue
                line = f'from {module} import {", ".join(used_items)}'
            else:
                continue
            if line not in existing and line not in new_imports:
                new_imports.append(line)
        if not new_imports:
            return updated_source.rstrip('\n') + '\n'

        insert_at = self._import_insert_index(updated_source, lines)
        lines[insert_at:insert_at] = new_imports
        return '\n'.join(lines).rstrip('\n') + '\n'

    def _loaded_names(self, source: str) -> set[str]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}

    def _from_import_binding_name(self, name: str) -> str:
        raw = str(name or '').strip()
        if ' as ' in raw:
            return raw.rsplit(' as ', 1)[1].strip()
        return raw.split('.', 1)[0].strip()

    def _apply_remove_import_change(self, source: str, change: dict) -> str:
        module = str(change.get('module') or '').strip()
        if not module:
            return source
        action = str(change.get('action') or '').strip()
        names = [str(name).split(' as ')[0].strip() for name in (change.get('names') or []) if str(name).strip()]
        alias_filter = str(change.get('alias') or change.get('asname') or '').strip()

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return source

        lines = source.splitlines()
        replacements: list[tuple[int, int, list[str]]] = []

        def alias_text(alias: ast.alias) -> str:
            return alias.name + (f' as {alias.asname}' if alias.asname else '')

        for node in getattr(tree, 'body', []):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            start = int(getattr(node, 'lineno', 0) or 0) - 1
            end = int(getattr(node, 'end_lineno', 0) or 0)
            if start < 0 or end <= start:
                continue

            if isinstance(node, ast.Import) and action == 'remove_import':
                remaining: list[ast.alias] = []
                removed = False
                for alias in node.names:
                    matches_module = alias.name == module
                    matches_alias = not alias_filter or alias.asname == alias_filter
                    if matches_module and matches_alias:
                        removed = True
                    else:
                        remaining.append(alias)
                if not removed:
                    continue
                replacement = [f"import {', '.join(alias_text(alias) for alias in remaining)}"] if remaining else []
                replacements.append((start, end, replacement))

            elif isinstance(node, ast.ImportFrom):
                node_module = ('.' * int(getattr(node, 'level', 0) or 0)) + (node.module or '')
                remaining = []
                removed = False
                for alias in node.names:
                    full_name = f'{node_module}.{alias.name}' if node_module else alias.name
                    matches = False
                    if action == 'remove_import':
                        matches = full_name == module
                    elif action == 'remove_from_import':
                        matches = node_module == module and (not names or alias.name in names)
                    if matches:
                        removed = True
                    else:
                        remaining.append(alias)
                if not removed:
                    continue
                replacement = [f"from {node_module} import {', '.join(alias_text(alias) for alias in remaining)}"] if remaining else []
                replacements.append((start, end, replacement))

        if not replacements:
            return source

        for start, end, replacement in sorted(replacements, reverse=True):
            lines[start:end] = replacement
        return '\n'.join(lines).rstrip('\n') + '\n'

    def _import_insert_index(self, source: str, lines: list[str]) -> int:
        def is_blank_or_comment(line: str) -> bool:
            stripped = line.strip()
            return not stripped or stripped.startswith('#')

        insert_at = 0
        try:
            tree = ast.parse(source)
            if (
                tree.body
                and isinstance(tree.body[0], ast.Expr)
                and isinstance(getattr(tree.body[0], 'value', None), ast.Constant)
                and isinstance(tree.body[0].value.value, str)
            ):
                insert_at = int(getattr(tree.body[0], 'end_lineno', 1) or 1)
        except SyntaxError:
            return 0

        # Future imports must stay immediately after the module docstring and
        # before all regular imports. Skip blank/comment lines only to discover
        # a future-import block, then insert regular imports after that block.
        probe = insert_at
        while probe < len(lines) and is_blank_or_comment(lines[probe]):
            probe += 1
        while probe < len(lines) and lines[probe].startswith('from __future__ import '):
            probe += 1
            while probe < len(lines) and is_blank_or_comment(lines[probe]):
                probe += 1
        # Insert new regular imports before the existing regular import block.
        # This preserves the required order: module docstring, future imports,
        # then normal imports. It also keeps newly added stdlib imports from
        # being placed before from __future__ imports.
        return probe

    def _normalize_method_snippet_indentation(self, raw_lines: list[str]) -> list[str]:
        if not raw_lines:
            return raw_lines

        def is_method_header(line: str) -> bool:
            stripped = line.lstrip()
            return stripped.startswith('def ') or stripped.startswith('async def ')

        header_index = next((idx for idx, line in enumerate(raw_lines) if is_method_header(line)), -1)
        if header_index < 0:
            return [line.lstrip() if line.strip() else '' for line in raw_lines]

        prefix_lines = raw_lines[:header_index]
        method_header = raw_lines[header_index].lstrip()
        body_lines = raw_lines[header_index + 1:]

        normalized: list[str] = []
        for line in prefix_lines:
            if not line.strip():
                normalized.append('')
            else:
                normalized.append(line.lstrip())
        normalized.append(method_header)

        if not body_lines:
            return normalized

        non_empty_body_indents = [
            len(line) - len(line.lstrip())
            for line in body_lines
            if line.strip()
        ]
        min_body_indent = min(non_empty_body_indents) if non_empty_body_indents else 0

        for line in body_lines:
            if not line.strip():
                normalized.append('')
                continue

            stripped_body = line[min_body_indent:] if min_body_indent > 0 else line.lstrip()
            normalized.append('    ' + stripped_body)

        return normalized

    def _class_body_payload_starts_with_method(self, source: str) -> bool:
        lines = [line for line in source.splitlines() if line.strip()]
        if not lines:
            return False

        def is_method_header(line: str) -> bool:
            stripped = line.lstrip()
            return stripped.startswith('def ') or stripped.startswith('async def ')

        first = lines[0].lstrip()
        if is_method_header(first):
            return True
        if not first.startswith('@'):
            return False

        for line in lines[1:]:
            stripped = line.lstrip()
            if stripped.startswith('@'):
                continue
            return is_method_header(stripped)
        return False

    def _normalize_class_body_method_lines(self, lines: list[str], symbol: SymbolRecord, payload_lines: list[str]) -> list[str]:
        if symbol.kind not in {'method', 'class'}:
            raise ValueError('insert_scope=class_body requires class or method target anchor')
        dedented = textwrap.dedent('\n'.join(payload_lines)).strip('\n')
        if not dedented.strip():
            raise ValueError('Generated method code is empty for class_body insert')
        if not self._class_body_payload_starts_with_method(dedented):
            raise ValueError('insert_scope=class_body expects generated code to start with def, async def, or method decorator')
        method_indent = self._method_indent(lines, symbol)
        normalized_lines = self._normalize_method_snippet_indentation(dedented.splitlines())
        return [method_indent + line if line.strip() else '' for line in normalized_lines]

    def _normalize_replace_symbol_lines(self, lines: list[str], symbol: SymbolRecord, payload_lines: list[str]) -> list[str]:
        if symbol.kind != 'method':
            return payload_lines

        dedented = textwrap.dedent('\n'.join(payload_lines)).strip('\n')
        if not dedented.strip():
            raise ValueError('Generated method code is empty for replace_symbol')
        stripped = dedented.lstrip()
        if not (stripped.startswith('def ') or stripped.startswith('async def ')):
            return payload_lines

        method_indent = self._method_indent(lines, symbol)
        normalized_lines = self._normalize_method_snippet_indentation(dedented.splitlines())
        return [method_indent + line if line.strip() else '' for line in normalized_lines]

    def _assert_target_structure_preserved(self, source: str, symbol: SymbolRecord, artifact: PatchArtifact) -> None:
        if artifact.operation != 'replace_symbol':
            return
        tree = ast.parse(source)
        qualnames = set(self._list_symbol_qualnames_from_tree(tree, symbol.module_name))
        if symbol.qualname in qualnames:
            return

        if symbol.kind == 'method' and f'{symbol.module_name}.{symbol.name}' in qualnames:
            raise ValueError(
                f'Replaced method {symbol.qualname} was rendered as module-level function {symbol.module_name}.{symbol.name}'
            )

        raise ValueError(f'Replaced target symbol is missing after apply: {symbol.qualname}')

    def _method_indent(self, lines: list[str], symbol: SymbolRecord) -> str:
        if symbol.kind == 'method' and 0 <= symbol.start_line - 1 < len(lines):
            line = lines[symbol.start_line - 1]
            return line[: len(line) - len(line.lstrip())]
        if symbol.kind == 'class':
            class_indent = ''
            if 0 <= symbol.start_line - 1 < len(lines):
                line = lines[symbol.start_line - 1]
                class_indent = line[: len(line) - len(line.lstrip())]
            for idx in range(symbol.start_line, min(symbol.end_line, len(lines))):
                line = lines[idx]
                stripped = line.lstrip()
                if stripped.startswith('def ') or stripped.startswith('async def '):
                    return line[: len(line) - len(stripped)]
            return class_indent + '    '
        return '    '

    def _normalize_payload_lines(self, payload: str) -> list[str]:
        stripped = payload.rstrip('\n')
        if not stripped:
            raise ValueError('Patch artifact is empty.')
        return stripped.splitlines()

    def _with_spacing_before_insert(self, lines: list[str], insert_at: int, payload_lines: list[str]) -> list[str]:
        result = list(payload_lines)
        if insert_at > 0 and lines[insert_at - 1].strip():
            result = [''] + result
        if insert_at < len(lines) and lines[insert_at].strip():
            result = result + ['']
        return result
    
    def _list_symbol_qualnames_from_source(self, file_path: Path, module_name: str) -> list[str]:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        return self._list_symbol_qualnames_from_tree(tree, module_name)

    def _list_symbol_qualnames_from_tree(self, tree: ast.AST, module_name: str) -> list[str]:
        qualnames: list[str] = []

        for node in getattr(tree, 'body', []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualnames.append(f'{module_name}.{node.name}')
            elif isinstance(node, ast.ClassDef):
                class_qualname = f'{module_name}.{node.name}'
                qualnames.append(class_qualname)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qualnames.append(f'{class_qualname}.{child.name}')
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        qualnames.append(f'{module_name}.{target.id}')
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                qualnames.append(f'{module_name}.{node.target.id}')

        return qualnames

    def _build_impact_summary(
        self,
        project_key: str,
        store: IndexStore,
        overlays: OverlayService,
        target_qualname: str,
        changed_files: list[str],
        validation_ok: bool,
        before_primary_file: Path,
        after_primary_file: Path,
        target_module_name: str,
        operation: str,
    ) -> ImpactSummary:
        changed_file_path = changed_files[0] if changed_files else ''
        direct_inbound = [
            relation for relation in store.list_inbound_relations_for_qualname(project_key, target_qualname)
            if relation.relation_kind in {'calls', 'exposed_by_controller', 'covered_by_test'}
        ]

        inbound_callers: list[str] = []
        related_tests: list[str] = []
        seen_callers: set[str] = set()
        seen_tests: set[str] = set()
        frontier: list[str] = []

        for relation in direct_inbound:
            if relation.source_qualname not in seen_callers:
                seen_callers.add(relation.source_qualname)
                inbound_callers.append(relation.source_qualname)
                frontier.append(relation.source_qualname)
            if relation.relation_kind == 'covered_by_test' or relation.source_qualname.startswith('tests.'):
                if relation.source_qualname not in seen_tests:
                    seen_tests.add(relation.source_qualname)
                    related_tests.append(relation.source_qualname)

        for caller_qualname in list(frontier):
            for relation in store.list_inbound_relations_for_qualname(project_key, caller_qualname):
                if relation.relation_kind not in {'calls', 'exposed_by_controller', 'covered_by_test'}:
                    continue
                if relation.source_qualname not in seen_callers:
                    seen_callers.add(relation.source_qualname)
                    inbound_callers.append(relation.source_qualname)
                if relation.source_qualname.startswith('tests.') and relation.source_qualname not in seen_tests:
                    seen_tests.add(relation.source_qualname)
                    related_tests.append(relation.source_qualname)

        linked_requirements = overlays.requirements_for_symbol(target_qualname)
        recommended_tests = sorted(related_tests)
        recommended_test_commands = self._recommended_test_commands(store, project_key, recommended_tests)
        notes = ['Impact summary is heuristic and based on the current graph index.']
        if validation_ok:
            notes.append('Структурная валидация прошла успешно; изменение можно анализировать дальше вручную.')
        else:
            notes.append('Структурная валидация завершилась с ошибкой; оценка последствий неполная.')
        if linked_requirements:
            notes.append('Для target есть связанные требования из knowledge-слоя.')
        if recommended_tests:
            notes.append('Для изменения подобраны рекомендуемые тесты к прогону.')
        if not inbound_callers:
            notes.append('Во входящем графе вызовов не найдено вызывающих символов.')

        symbols_in_changed_files: list[str] = []
        seen_symbols: set[str] = set()

        primary_symbols_before = set(
            self._list_symbol_qualnames_from_source(before_primary_file, target_module_name)
        )
        primary_symbols_after = self._list_symbol_qualnames_from_source(after_primary_file, target_module_name)

        primary_added_symbols = [
            qualname
            for qualname in primary_symbols_after
            if qualname not in primary_symbols_before
        ]

        if operation == 'insert_after_symbol':
            if primary_added_symbols:
                for qualname in primary_added_symbols:
                    if qualname not in seen_symbols:
                        seen_symbols.add(qualname)
                        symbols_in_changed_files.append(qualname)
            elif target_qualname:
                seen_symbols.add(target_qualname)
                symbols_in_changed_files.append(target_qualname)
        else:
            if target_qualname:
                seen_symbols.add(target_qualname)
                symbols_in_changed_files.append(target_qualname)

        for file_path in changed_files:
            if file_path == changed_file_path:
                continue
            for changed_symbol in store.list_symbols_in_file(project_key, file_path):
                if changed_symbol.kind == 'module' or changed_symbol.qualname in seen_symbols:
                    continue
                seen_symbols.add(changed_symbol.qualname)
                symbols_in_changed_files.append(changed_symbol.qualname)

        return ImpactSummary(
            target_qualname=target_qualname,
            changed_files=changed_files,
            symbols_in_changed_files=symbols_in_changed_files,
            inbound_callers=inbound_callers,
            related_tests=related_tests,
            linked_requirements=linked_requirements,
            notes=notes,
            recommended_tests=recommended_tests,
            recommended_test_commands=recommended_test_commands,
        )

    def _recommended_test_commands(self, store: IndexStore, project_key: str, test_qualnames: list[str]) -> list[str]:
        commands: list[str] = []
        seen_paths: set[str] = set()
        for qualname in test_qualnames:
            symbol = store.get_symbol(project_key, qualname)
            if symbol and symbol.file_path not in seen_paths:
                seen_paths.add(symbol.file_path)
                commands.append(f'pytest {symbol.file_path}')
        return commands
