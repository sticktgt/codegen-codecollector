from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


class RunArtifactsManager:
    def __init__(self, tool_root: Path, runs_root_dirname: str = '.runs') -> None:
        self.tool_root = tool_root.resolve()
        self.runs_root = self.tool_root / runs_root_dirname
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create_run_dir(self, prefix: str = 'pipeline') -> tuple[str, str, Path]:
        now = datetime.now(tz=UTC)
        run_label = now.strftime('%Y%m%dT%H%M%S.%fZ')
        run_id = f'{prefix}-{run_label}-{uuid4().hex[:6]}'
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        LOGGER.info('Created run artifacts directory %s', run_dir)
        return run_id, run_label, run_dir

    def write_bundle(self, run_dir: Path, filename: str, payload: Any) -> Path:
        path = run_dir / filename
        serializable = self._serialize(payload)
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding='utf-8')
        LOGGER.info('Wrote run artifact bundle %s', path)
        return path

    def _serialize(self, value: Any) -> Any:
        if is_dataclass(value):
            return {key: self._serialize(item) for key, item in asdict(value).items()}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._serialize(item) for item in value]
        return value
