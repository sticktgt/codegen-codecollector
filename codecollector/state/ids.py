from __future__ import annotations

from datetime import UTC, datetime
from secrets import token_hex


def _make_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    return f"{prefix}-{stamp}-{token_hex(3)}"


def new_project_id() -> str:
    return _make_id('proj')


def new_library_id() -> str:
    return _make_id('lib')


def new_session_id() -> str:
    return _make_id('sess')
