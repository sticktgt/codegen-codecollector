from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, relative_path: str | Path, payload: dict[str, Any]) -> Path:
        path = (self.root / relative_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return path

    def read(self, relative_path: str | Path) -> dict[str, Any]:
        path = (self.root / relative_path).resolve()
        return json.loads(path.read_text(encoding='utf-8'))

    def exists(self, relative_path: str | Path) -> bool:
        return (self.root / relative_path).resolve().exists()

    def delete(self, relative_path: str | Path) -> None:
        path = (self.root / relative_path).resolve()
        if path.exists():
            path.unlink()

    def list(self, relative_dir: str | Path = '.') -> list[Path]:
        path = (self.root / relative_dir).resolve()
        if not path.exists():
            return []
        return sorted(item for item in path.iterdir() if item.is_file())
