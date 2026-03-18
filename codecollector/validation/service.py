from __future__ import annotations

import ast
import compileall
from pathlib import Path

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
