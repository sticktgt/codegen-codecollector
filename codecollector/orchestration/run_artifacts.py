from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from codecollector.logger import get_logger
from codecollector.vector_search.ollama_embeddings import snapshot_embedding_usage

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
            serialized = {key: self._serialize(item) for key, item in asdict(value).items()}
            if value.__class__.__name__ == 'PipelineRunResult':
                serialized['usage_summary'] = self._build_usage_summary(serialized)
            return serialized
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._serialize(item) for item in value]
        return value


    def _build_usage_summary(self, serialized_run: dict[str, Any]) -> dict[str, Any]:
        def usage_from(payload: dict[str, Any] | None) -> dict[str, Any] | None:
            if not payload:
                return None
            usage = payload.get('result_payload', {}).get('llm_usage')
            return usage if isinstance(usage, dict) else None

        def merge_usage(items: list[dict[str, Any] | None]) -> dict[str, Any]:
            merged: dict[str, Any] = {
                'calls': 0,
                'prompt_tokens': 0.0,
                'output_tokens': 0.0,
                'total_tokens': 0.0,
                'duration_sec': 0.0,
                'total_duration_sec': 0.0,
                'load_duration_sec': 0.0,
                'prompt_eval_duration_sec': 0.0,
                'eval_duration_sec': 0.0,
            }
            for item in items:
                if not item:
                    continue
                merged['calls'] += int(item.get('calls', 0) or 0)
                for key in (
                    'prompt_tokens',
                    'output_tokens',
                    'total_tokens',
                    'duration_sec',
                    'total_duration_sec',
                    'load_duration_sec',
                    'prompt_eval_duration_sec',
                    'eval_duration_sec',
                ):
                    merged[key] = round(float(merged.get(key, 0.0)) + float(item.get(key, 0.0) or 0.0), 6)
            return merged

        code_generation = usage_from(serialized_run.get('external_code_generation'))
        test_generation = usage_from(serialized_run.get('external_test_generation'))
        repair_generation = usage_from(serialized_run.get('repair_generation'))
        embedding = snapshot_embedding_usage()
        llm_total = merge_usage([code_generation, test_generation, repair_generation])
        overall_total_tokens = round(float(llm_total.get('total_tokens', 0.0)) + float(embedding.get('prompt_tokens', 0) or 0.0), 6)
        return {
            'embedding': embedding,
            'code_generation': code_generation,
            'test_generation': test_generation,
            'repair_generation': repair_generation,
            'llm_total': llm_total,
            'overall_total_tokens_including_embeddings': overall_total_tokens,
        }
