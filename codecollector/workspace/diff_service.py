from __future__ import annotations

import difflib
from pathlib import Path

from codecollector.domain.models import DiffSummary
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class DiffService:
    def summarize_file_change(self, before_path: Path, after_path: Path) -> DiffSummary:
        LOGGER.info('Summarizing diff for %s -> %s', before_path, after_path)
        before = before_path.read_text(encoding='utf-8').splitlines(keepends=True)
        after = after_path.read_text(encoding='utf-8').splitlines(keepends=True)
        diff = ''.join(
            difflib.unified_diff(
                before,
                after,
                fromfile=str(before_path),
                tofile=str(after_path),
            )
        )
        return DiffSummary(changed_files=[str(after_path)], unified_diff=diff)
