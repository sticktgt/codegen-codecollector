from __future__ import annotations

from typing import Any

import requests

from codecollector.logger import get_logger

LOGGER = get_logger(__name__)

_EMBEDDING_USAGE: dict[str, Any] = {
    'calls': 0,
    'texts_count': 0,
    'chars_total': 0,
    'prompt_tokens': 0,
    'total_duration_sec': 0.0,
    'load_duration_sec': 0.0,
    'models': [],
    'endpoints': [],
}


def reset_embedding_usage() -> None:
    _EMBEDDING_USAGE.update({
        'calls': 0,
        'texts_count': 0,
        'chars_total': 0,
        'prompt_tokens': 0,
        'total_duration_sec': 0.0,
        'load_duration_sec': 0.0,
        'models': [],
        'endpoints': [],
    })


def snapshot_embedding_usage() -> dict[str, Any]:
    return {
        'calls': int(_EMBEDDING_USAGE.get('calls', 0)),
        'texts_count': int(_EMBEDDING_USAGE.get('texts_count', 0)),
        'chars_total': int(_EMBEDDING_USAGE.get('chars_total', 0)),
        'prompt_tokens': int(_EMBEDDING_USAGE.get('prompt_tokens', 0)),
        'total_duration_sec': round(float(_EMBEDDING_USAGE.get('total_duration_sec', 0.0)), 6),
        'load_duration_sec': round(float(_EMBEDDING_USAGE.get('load_duration_sec', 0.0)), 6),
        'models': list(_EMBEDDING_USAGE.get('models', [])),
        'endpoints': list(_EMBEDDING_USAGE.get('endpoints', [])),
    }

try:
    from langchain_core.embeddings import Embeddings
except Exception:  # pragma: no cover
    class Embeddings:  # type: ignore[override]
        pass


class OllamaEmbeddings(Embeddings):
    def __init__(self, base_url: str, model: str, timeout_sec: int = 120) -> None:
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout_sec = timeout_sec
        self.session = requests.Session()

    def _log_usage(self, data: dict[str, Any], *, texts_count: int, chars_total: int, endpoint: str) -> None:
        prompt_tokens = int(data.get('prompt_eval_count', 0) or 0)
        total_duration_sec = float(data.get('total_duration', 0) or 0) / 1e9
        load_duration_sec = float(data.get('load_duration', 0) or 0) / 1e9
        _EMBEDDING_USAGE['calls'] = int(_EMBEDDING_USAGE.get('calls', 0)) + 1
        _EMBEDDING_USAGE['texts_count'] = int(_EMBEDDING_USAGE.get('texts_count', 0)) + texts_count
        _EMBEDDING_USAGE['chars_total'] = int(_EMBEDDING_USAGE.get('chars_total', 0)) + chars_total
        _EMBEDDING_USAGE['prompt_tokens'] = int(_EMBEDDING_USAGE.get('prompt_tokens', 0)) + prompt_tokens
        _EMBEDDING_USAGE['total_duration_sec'] = float(_EMBEDDING_USAGE.get('total_duration_sec', 0.0)) + total_duration_sec
        _EMBEDDING_USAGE['load_duration_sec'] = float(_EMBEDDING_USAGE.get('load_duration_sec', 0.0)) + load_duration_sec
        if self.model not in _EMBEDDING_USAGE['models']:
            _EMBEDDING_USAGE['models'].append(self.model)
        if endpoint not in _EMBEDDING_USAGE['endpoints']:
            _EMBEDDING_USAGE['endpoints'].append(endpoint)
        LOGGER.info(
            'embedding usage endpoint=%s model=%s texts_count=%s chars_total=%s prompt_tokens=%s total_duration=%.2fs load_duration=%.2fs',
            endpoint,
            self.model,
            texts_count,
            chars_total,
            prompt_tokens,
            total_duration_sec,
            load_duration_sec,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {'model': self.model, 'input': texts}
        chars_total = sum(len(text) for text in texts)
        response = self.session.post(f'{self.base_url}/api/embed', json=payload, timeout=self.timeout_sec)
        if response.ok:
            data = response.json()
            self._log_usage(data, texts_count=len(texts), chars_total=chars_total, endpoint='/api/embed')
            embeddings = data.get('embeddings') or []
            if embeddings:
                return [list(map(float, item)) for item in embeddings]
        result: list[list[float]] = []
        for text in texts:
            response = self.session.post(
                f'{self.base_url}/api/embeddings',
                json={'model': self.model, 'prompt': text},
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            data = response.json()
            self._log_usage(data, texts_count=1, chars_total=len(text), endpoint='/api/embeddings')
            result.append([float(x) for x in data.get('embedding', [])])
        return result

    def embed_query(self, text: str) -> list[float]:
        values = self.embed_documents([text])
        return values[0] if values else []
