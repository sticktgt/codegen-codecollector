from __future__ import annotations

from typing import Any

import requests

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

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, Any] = {'model': self.model, 'input': texts}
        response = self.session.post(f'{self.base_url}/api/embed', json=payload, timeout=self.timeout_sec)
        if response.ok:
            data = response.json()
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
            result.append([float(x) for x in data.get('embedding', [])])
        return result

    def embed_query(self, text: str) -> list[float]:
        values = self.embed_documents([text])
        return values[0] if values else []
