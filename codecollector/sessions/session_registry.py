
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from codecollector.config import AppConfig
from codecollector.state.paths import StatePathService


class SessionRegistry:
    def __init__(self, tool_root: Path, config: AppConfig) -> None:
        self.tool_root = tool_root.resolve()
        self.config = config
        self.paths = StatePathService(tool_root, config).ensure()

    @property
    def sessions_dir(self) -> Path:
        return self.paths.sessions

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f'{session_id}.json'

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload['session_id'])
        path = self._session_path(session_id)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return payload

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f'Session not found: {session_id}')
        return json.loads(path.read_text(encoding='utf-8'))

    def list(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob('*.json')):
            try:
                items.append(json.loads(path.read_text(encoding='utf-8')))
            except Exception:
                continue
        items.sort(key=lambda item: str(item.get('created_at', '')), reverse=True)
        return items

    def delete(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True
