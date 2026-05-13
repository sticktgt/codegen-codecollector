from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import time
from typing import Any
from urllib import error, request

from codecollector.config import AppConfig
from codecollector.logger import get_logger

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class LlmCallResult:
    content: str
    raw: dict[str, Any]
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_sec: float = 0.0
    total_duration_sec: float = 0.0
    prompt_chars: int = 0
    system_chars: int = 0
    user_chars: int = 0
    num_predict: int = 0
    done_reason: str = ''

    def usage_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop('content', None)
        payload.pop('raw', None)
        payload['total_tokens'] = self.prompt_tokens + self.output_tokens
        return payload


class AnalysisLlmClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.base_url = config.analysis_llm_base_url.rstrip('/')
        self.api_key = config.analysis_llm_api_key.strip() or None
        if self.base_url.endswith('/api'):
            self.api_base_url = self.base_url
        else:
            self.api_base_url = f'{self.base_url}/api'
        self.chat_url = f'{self.api_base_url}/chat'

    def chat_json(
        self,
        *,
        step: str,
        system_prompt: str,
        user_prompt: str,
        max_prompt_chars: int | None = None,
        num_predict: int | None = None,
    ) -> LlmCallResult:
        if not self.base_url:
            raise RuntimeError('analysis.llm_assist.base_url is empty')
        prompt_chars = len(system_prompt) + len(user_prompt)
        effective_max_prompt_chars = max_prompt_chars or self.config.analysis_llm_max_prompt_chars
        if prompt_chars > effective_max_prompt_chars:
            raise ValueError(
                f'analysis LLM prompt is too large for {step}: '
                f'{prompt_chars} chars > {effective_max_prompt_chars} chars'
            )

        effective_num_predict = int(num_predict if num_predict is not None else self.config.analysis_llm_num_predict)

        payload: dict[str, Any] = {
            'model': self.config.analysis_llm_model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': False,
            'format': 'json',
            'keep_alive': self.config.analysis_llm_keep_alive,
            'options': {
                'temperature': self.config.analysis_llm_temperature,
                'num_predict': effective_num_predict,
                'num_ctx': self.config.analysis_llm_num_ctx,
            },
        }
        if self.config.analysis_llm_think is not None:
            payload['think'] = self.config.analysis_llm_think

        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'

        LOGGER.info(
            'analysis llm %s call: model=%s prompt_chars=%s max_prompt_chars=%s system_chars=%s user_chars=%s num_predict=%s',
            step,
            self.config.analysis_llm_model,
            prompt_chars,
            effective_max_prompt_chars,
            len(system_prompt),
            len(user_prompt),
            effective_num_predict,
        )
        started_at = time.perf_counter()
        req = request.Request(
            self.chat_url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=self.config.analysis_llm_timeout_sec) as response:
                raw_body = response.read().decode('utf-8')
        except error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')[:2000]
            LOGGER.error('analysis llm %s HTTP %s body=%s', step, exc.code, body)
            raise RuntimeError(f'analysis LLM HTTP {exc.code}: {body}') from exc
        except error.URLError as exc:
            raise RuntimeError(f'analysis LLM request failed: {exc}') from exc

        duration_sec = time.perf_counter() - started_at
        try:
            raw = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'analysis LLM returned invalid JSON response: {exc}') from exc

        message = raw.get('message') or {}
        content = str(message.get('content') or '')
        result = LlmCallResult(
            content=content,
            raw=raw,
            prompt_tokens=int(raw.get('prompt_eval_count') or 0),
            output_tokens=int(raw.get('eval_count') or 0),
            duration_sec=duration_sec,
            total_duration_sec=float(raw.get('total_duration') or 0) / 1_000_000_000,
            prompt_chars=prompt_chars,
            system_chars=len(system_prompt),
            user_chars=len(user_prompt),
            num_predict=effective_num_predict,
            done_reason=str(raw.get('done_reason') or ''),
        )
        LOGGER.info(
            'analysis llm %s result: prompt_tokens=%s output_tokens=%s total_tokens=%s duration=%.2fs done_reason=%s',
            step,
            result.prompt_tokens,
            result.output_tokens,
            result.prompt_tokens + result.output_tokens,
            duration_sec,
            result.done_reason,
        )
        return result
