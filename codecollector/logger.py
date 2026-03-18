from __future__ import annotations

import logging

_CONFIGURED = False


def configure_logging(level: str = 'INFO', fmt: str = '%(asctime)s | %(levelname)s | %(name)s | %(message)s') -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
