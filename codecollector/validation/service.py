from __future__ import annotations

import ast
import compileall
import subprocess
import sys
from pathlib import Path
from typing import Any

from codecollector.domain.models import (
    ValidationIssue,
    ValidationReport,
    VerificationBlock,
    VerificationIssue,
)
from codecollector.logger import get_logger
from codecollector.validation.semantic_checks import find_duplicate_symbol_definitions

LOGGER = get_logger(__name__)


class ValidationService:
    def validate_project(self, project_root: Path, changed_files: list[Path]) -> ValidationReport:
        issues: list[ValidationIssue] = []
        project_root = project_root.resolve()
        for path in changed_files:
            try:
                source = path.read_text(encoding='utf-8')
                ast.parse(source)
            except SyntaxError as exc:
                LOGGER.exception('AST validation failed for %s: %s', path, exc)
                issues.append(
                    ValidationIssue(
                        severity='error',
                        check_name='ast_parse',
                        message=f'{exc.msg} at line {exc.lineno}',
                        file_path=str(path),
                    )
                )
                continue

            try:
                relative_path = str(path.resolve().relative_to(project_root))
            except ValueError:
                relative_path = str(path)
            module_name = self._module_name_from_path(relative_path)
            for duplicate in find_duplicate_symbol_definitions(source, module_name, relative_path):
                lines = duplicate.get('lines') or []
                line_text = ', '.join(str(item) for item in lines) if lines else 'unknown'
                issues.append(
                    ValidationIssue(
                        severity='error',
                        check_name='duplicate_symbol_definition',
                        message=(
                            f"Duplicate symbol definition {duplicate['qualname']} "
                            f"({duplicate['kind']}) at lines {line_text}. "
                            "Repair should generate a new unique symbol instead of copying an existing one."
                        ),
                        file_path=relative_path,
                    )
                )
        if not issues:
            ok = compileall.compile_dir(str(project_root), quiet=1, force=True)
            if not ok:
                LOGGER.error('compileall reported errors for %s', project_root)
                issues.append(
                    ValidationIssue(
                        severity='error',
                        check_name='compileall',
                        message='compileall reported errors in project',
                        file_path=str(project_root),
                    )
                )
        if not issues:
            issues.append(
                ValidationIssue(
                    severity='info',
                    check_name='structural_review',
                    message='Structural validation passed. Manual architectural review is still recommended.',
                    file_path=str(project_root),
                )
            )
        return ValidationReport(is_valid=not any(item.severity == 'error' for item in issues), issues=issues)


    def _module_name_from_path(self, relative_path: str) -> str:
        normalized = str(relative_path or '').replace('\\', '/').strip('/')
        if normalized.endswith('/__init__.py'):
            normalized = normalized[: -len('/__init__.py')]
        elif normalized.endswith('.py'):
            normalized = normalized[:-3]
        return normalized.replace('/', '.')

    def run_post_apply_checks(
        self,
        project_root: Path,
        recommended_tests: list[str] | None = None,
       *,
        run_ruff: bool = False,
        run_recommended_tests: bool = True,
        run_full_project_tests: bool = False,
    ) -> dict[str, Any]:
        LOGGER.info(
            "run_post_apply_checks project_root=%s run_ruff=%s run_recommended_tests=%s "
            "run_full_project_tests=%s recommended_tests=%s",
            project_root,
            run_ruff,
            run_recommended_tests,
            run_full_project_tests,
            recommended_tests or [],
        )
    
        results: dict[str, Any] = {}
        results['ast_parse'] = self._verify_python_syntax(project_root)
        results['py_compile'] = self._run_command([sys.executable, '-m', 'compileall', '.'], project_root)

        if run_ruff:
            results['ruff'] = self._run_command([sys.executable, '-m', 'ruff', 'check', '.'], project_root)

        if run_recommended_tests and recommended_tests:
            paths, resolution_details = self._resolve_test_targets(project_root, recommended_tests)
            LOGGER.info(
                "resolved verification test targets from %s to pytest paths %s",
                recommended_tests,
                paths,
            )
            unresolved_targets = [
                item.get('target')
                for item in resolution_details
                if item.get('status') == 'unresolved'
            ]
            fallback_targets = [
                item
                for item in resolution_details
                if item.get('status') == 'fallback_file'
            ]
            if unresolved_targets:
                LOGGER.warning(
                    "some verification targets were not resolved to pytest paths: unresolved=%s",
                    unresolved_targets,
                )
            if fallback_targets:
                LOGGER.warning(
                    "some verification targets were resolved only to fallback files: fallback=%s",
                    fallback_targets,
                )
            if paths:
                pytest_result = self._run_command([sys.executable, '-m', 'pytest', *paths], project_root)
                pytest_result['target_resolution'] = resolution_details
                results['pytest_recommended'] = pytest_result

        if run_full_project_tests:
            results['pytest_full'] = self._run_command([sys.executable, '-m', 'pytest'], project_root)

        return results

    def verification_passed(self, results: dict[str, Any]) -> bool:
        return all(result.get('ok', False) for result in results.values())

    def build_failure_summary(self, results: dict[str, Any], max_lines: int = 20, max_chars: int = 1600) -> dict[str, Any]:
        failed_checks: list[dict[str, Any]] = []
        for name, result in results.items():
            if result.get('ok', False):
                continue
            summary: dict[str, Any] = {'check': name, 'ok': False}
            if 'command' in result:
                summary['command'] = ' '.join(str(part) for part in result.get('command', []))
            if 'returncode' in result:
                summary['returncode'] = result.get('returncode')
            if result.get('error'):
                summary['error'] = str(result.get('error'))
            output = '\n'.join(
                str(result.get(key, '') or '').strip()
                for key in ('stdout', 'stderr')
                if str(result.get(key, '') or '').strip()
            )
            lines = [line.rstrip() for line in output.splitlines() if line.strip()][:max_lines]
            excerpt = '\n'.join(lines)
            if len(excerpt) > max_chars:
                excerpt = excerpt[:max_chars].rstrip() + '\n...'
            if excerpt:
                summary['output_excerpt'] = excerpt
            failed_checks.append(summary)
        return {'failed_checks': failed_checks, 'repairable': bool(failed_checks)}

    def run_runtime_verification_blocks(
        self,
        project_root: Path,
        recommended_tests: list[str] | None = None,
        *,
        run_ruff: bool = False,
        run_recommended_tests: bool = True,
        run_full_project_tests: bool = False,
    ) -> list[VerificationBlock]:
        raw_results = self.run_post_apply_checks(
            project_root,
            recommended_tests,
            run_ruff=run_ruff,
            run_recommended_tests=run_recommended_tests,
            run_full_project_tests=run_full_project_tests,
        )
        return self._raw_results_to_blocks(raw_results)

    def _raw_results_to_blocks(self, results: dict[str, Any]) -> list[VerificationBlock]:
        blocks: list[VerificationBlock] = []
        name_mapping = {
            'ast_parse': 'runtime_ast_parse',
            'py_compile': 'runtime_py_compile',
            'ruff': 'runtime_ruff',
            'pytest_recommended': 'runtime_pytest_recommended',
            'pytest_full': 'runtime_pytest_full',
        }
        for raw_name, payload in results.items():
            ok = bool((payload or {}).get('ok', False))
            issues: list[VerificationIssue] = []
            if not ok:
                message = str((payload or {}).get('error') or '').strip()
                if not message:
                    stderr = str((payload or {}).get('stderr') or '').strip()
                    stdout = str((payload or {}).get('stdout') or '').strip()
                    message = stderr or stdout or f'Проверка {raw_name} завершилась ошибкой.'
                issues.append(
                    VerificationIssue(
                        code=raw_name,
                        message=message,
                        severity='error',
                    )
                )
            blocks.append(
                VerificationBlock(
                    name=name_mapping.get(raw_name, raw_name),
                    ok=ok,
                    severity='info' if ok else 'error',
                    issues=issues,
                    details=dict(payload or {}),
                )
            )
        return blocks

    def _verify_python_syntax(self, workspace_dir: Path) -> dict[str, Any]:
        checked_files: list[str] = []
        try:
            for py_file in workspace_dir.rglob('*.py'):
                source = py_file.read_text(encoding='utf-8')
                ast.parse(source)
                checked_files.append(str(py_file.relative_to(workspace_dir)))
            return {'ok': True, 'checked_files': checked_files}
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'checked_files': checked_files}

    def _run_command(self, cmd: list[str], cwd: Path) -> dict[str, Any]:
        LOGGER.info("run command cwd=%s cmd=%s", cwd, cmd)
        completed = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        LOGGER.info(
            "command finished returncode=%s cmd=%s stdout_chars=%s stderr_chars=%s",
            completed.returncode,
            cmd,
            len(completed.stdout or ""),
            len(completed.stderr or ""),
        )
        return {
            'command': cmd,
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'ok': completed.returncode == 0,
        }

    def _qualnames_to_test_paths(self, project_root: Path, recommended_tests: list[str]) -> list[str]:
        paths, _details = self._resolve_test_targets(project_root, recommended_tests)
        return paths

    def _resolve_test_targets(
        self,
        project_root: Path,
        recommended_tests: list[str],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        paths: list[str] = []
        details: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in recommended_tests:
            normalized = str(item or "").strip()
            if not normalized:
                continue

            resolved_path, detail = self._resolve_single_test_target(project_root, normalized)
            details.append(detail)
            if resolved_path and resolved_path not in seen:
                seen.add(resolved_path)
                paths.append(resolved_path)

        deduped_paths = self._dedupe_resolved_test_targets(paths)
        if len(deduped_paths) != len(paths):
            LOGGER.info(
                "deduplicated pytest targets from %s to %s",
                paths,
                deduped_paths,
            )
        return deduped_paths, details

    def _dedupe_resolved_test_targets(self, paths: list[str]) -> list[str]:
        """Remove broad file targets when a more precise node id for the same file exists."""
        node_files = {path.split('::', 1)[0] for path in paths if '::' in path}
        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if '::' not in path and path in node_files:
                continue
            if path in seen:
                continue
            seen.add(path)
            deduped.append(path)
        return deduped

    def _resolve_single_test_target(self, project_root: Path, target: str) -> tuple[str | None, dict[str, Any]]:
        normalized = str(target or "").strip().replace("\\", "/")
        detail: dict[str, Any] = {
            'target': target,
            'status': 'unresolved',
            'resolved': None,
            'candidates': [],
        }
        if not normalized:
            return None, detail

        if '::' in normalized:
            file_part = normalized.split('::', 1)[0]
            if file_part.endswith('.py') and (project_root / file_part).exists():
                detail.update({'status': 'pytest_node', 'resolved': normalized})
                return normalized, detail
            detail['candidates'] = [file_part]
            LOGGER.warning(
                "verification target looks like pytest node id but file is missing: target=%s file=%s",
                target,
                file_part,
            )
            return None, detail

        if normalized.endswith('.py'):
            if (project_root / normalized).exists():
                detail.update({'status': 'file', 'resolved': normalized})
                return normalized, detail
            detail['candidates'] = [normalized]
            LOGGER.warning(
                "verification target looks like direct test path but file is missing: target=%s resolved=%s",
                target,
                normalized,
            )
            return None, detail

        parts = [part for part in normalized.split('.') if part]
        if not parts or parts[0] != 'tests' or len(parts) < 2:
            LOGGER.warning("unsupported verification target format: target=%s", target)
            return None, detail

        candidate_files: list[tuple[str, list[str]]] = []
        for split_at in range(len(parts), 1, -1):
            file_path = '/'.join(parts[:split_at]) + '.py'
            suffix_parts = parts[split_at:]
            candidate_files.append((file_path, suffix_parts))

        detail['candidates'] = [file_path for file_path, _suffix in candidate_files]

        first_existing_file: str | None = None
        first_existing_suffix: list[str] = []
        for file_path, suffix_parts in candidate_files:
            if not (project_root / file_path).exists():
                continue
            if first_existing_file is None:
                first_existing_file = file_path
                first_existing_suffix = suffix_parts
            if not suffix_parts:
                detail.update({'status': 'file', 'resolved': file_path})
                return file_path, detail
            node_id = file_path + '::' + '::'.join(suffix_parts)
            if self._pytest_node_exists(project_root / file_path, suffix_parts):
                detail.update({'status': 'pytest_node', 'resolved': node_id})
                return node_id, detail

        if first_existing_file:
            detail.update(
                {
                    'status': 'fallback_file',
                    'resolved': first_existing_file,
                    'unresolved_node_suffix': '::'.join(first_existing_suffix),
                }
            )
            LOGGER.warning(
                "failed to resolve exact pytest node; using fallback file path: target=%s fallback=%s suffix=%s",
                target,
                first_existing_file,
                '::'.join(first_existing_suffix),
            )
            return first_existing_file, detail

        LOGGER.warning(
            "failed to resolve verification target to pytest path: target=%s candidates=%s",
            target,
            detail['candidates'],
        )
        return None, detail

    def _pytest_node_exists(self, file_path: Path, suffix_parts: list[str]) -> bool:
        if not suffix_parts:
            return True
        try:
            tree = ast.parse(file_path.read_text(encoding='utf-8'))
        except (OSError, SyntaxError) as exc:
            LOGGER.warning("failed to inspect pytest target file: file=%s error=%s", file_path, exc)
            return False

        first = suffix_parts[0]
        if len(suffix_parts) == 1:
            return any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == first
                for node in tree.body
            )

        class_node = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == first),
            None,
        )
        if class_node is None:
            return False

        second = suffix_parts[1]
        if len(suffix_parts) == 2:
            return any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == second
                for node in class_node.body
            )

        return False
