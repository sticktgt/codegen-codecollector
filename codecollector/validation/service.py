from __future__ import annotations

import ast
import compileall
import subprocess
import sys
from pathlib import Path
from typing import Any

from codecollector.domain.models import ValidationIssue, ValidationReport
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class ValidationService:
    def validate_project(self, project_root: Path, changed_files: list[Path]) -> ValidationReport:
        issues: list[ValidationIssue] = []
        for path in changed_files:
            try:
                ast.parse(path.read_text(encoding='utf-8'))
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

    def run_post_apply_checks(
        self,
        project_root: Path,
        recommended_tests: list[str] | None = None,
        *,
        run_ruff: bool = False,
        run_recommended_tests: bool = True,
        run_full_project_tests: bool = False,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        results['ast_parse'] = self._verify_python_syntax(project_root)
        results['py_compile'] = self._run_command([sys.executable, '-m', 'compileall', '.'], project_root)
        if run_ruff:
            results['ruff'] = self._run_command([sys.executable, '-m', 'ruff', 'check', '.'], project_root)
        if run_recommended_tests and recommended_tests:
            paths = self._qualnames_to_test_paths(project_root, recommended_tests)
            if paths:
                results['pytest_recommended'] = self._run_command([sys.executable, '-m', 'pytest', *paths], project_root)
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
        completed = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        return {
            'command': cmd,
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
            'ok': completed.returncode == 0,
        }

    def _qualnames_to_test_paths(self, project_root: Path, recommended_tests: list[str]) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        for item in recommended_tests:
            parts = item.split('.')
            if parts and parts[0] == 'tests' and len(parts) >= 2:
                file_path = '/'.join(parts[:-1]) + '.py'
                if (project_root / file_path).exists() and file_path not in seen:
                    seen.add(file_path)
                    paths.append(file_path)
        return paths
