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
            paths = self._qualnames_to_test_paths(project_root, recommended_tests)
            LOGGER.info(
                "resolved verification test targets from %s to pytest paths %s",
                recommended_tests,
                paths,
            )
            unresolved_targets = [
                item for item in (recommended_tests or [])
                if str(item or "").strip()
                and not (
                    (str(item).strip().endswith(".py") and (project_root / str(item).strip().replace("\\", "/")).exists())
                    or (
                        str(item).strip().startswith("tests.")
                        and (
                            (project_root / ("/".join(str(item).strip().split(".")) + ".py")).exists()
                            or (
                                len(str(item).strip().split(".")) >= 3
                                and (project_root / ("/".join(str(item).strip().split(".")[:-1]) + ".py")).exists()
                            )
                        )
                    )
                )
            ]
            if unresolved_targets:
                LOGGER.warning(
                    "some verification targets were not resolved to pytest paths: unresolved=%s",
                    unresolved_targets,
                )            
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
        paths: list[str] = []
        seen: set[str] = set()

        for item in recommended_tests:
            normalized = str(item or "").strip()
            if not normalized:
                continue

            # 1. Уже готовый pytest path, например:
            #    tests/test_generated_generate_test_AgentSummary.py
            if normalized.endswith(".py"):
                file_path = normalized.replace("\\", "/")
                if (project_root / file_path).exists():
                    if file_path not in seen:
                        seen.add(file_path)
                        paths.append(file_path)
                    continue

                LOGGER.warning(
                    "verification target looks like direct test path but file is missing: target=%s resolved=%s",
                    item,
                    file_path,
                )
                continue

            # 2. Квалифицированное имя тестового модуля/символа:
            #    tests.test_report_service
            #    tests.test_report_service.test_build_agent_summary_counts_only_open_assigned
            parts = normalized.split(".")
            if parts and parts[0] == "tests" and len(parts) >= 2:
                candidate_paths: list[str] = []

                # Полный модуль как файл: tests/test_report_service.py
                module_file_path = "/".join(parts) + ".py"
                candidate_paths.append(module_file_path)

                # Символ внутри модуля: tests/test_report_service.py
                parent_module_file_path = "/".join(parts[:-1]) + ".py"
                if len(parts) >= 3:
                    candidate_paths.append(parent_module_file_path)

                resolved = False
                for file_path in candidate_paths:
                    if (project_root / file_path).exists():
                        if file_path not in seen:
                            seen.add(file_path)
                            paths.append(file_path)
                        resolved = True
                        break

                if not resolved:
                    LOGGER.warning(
                        "failed to resolve verification target to pytest path: target=%s candidates=%s",
                        item,
                        candidate_paths,
                    )
                continue

            LOGGER.warning(
                "unsupported verification target format: target=%s",
                item,
            )

        return paths