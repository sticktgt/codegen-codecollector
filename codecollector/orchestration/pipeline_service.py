from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any

from codecollector.domain.models import (
    ChangeRequest,
    ExternalGenerationCall,
    GenerationReplay,
    MergePlan,
    PatchArtifact,
    PipelineRunResult,
    PipelineStepRecord,
    SearchCandidate,
)
from codecollector.logger import get_logger
from codecollector.orchestration.run_artifacts import RunArtifactsManager
from codecollector.external_codegen.adapter import build_generation_request, invoke_generate, patch_artifact_from_result, build_repair_request, invoke_repair

if TYPE_CHECKING:
    from codecollector.domain.models import ApplyResult, ContextPack
    from codecollector.orchestration.services import ProjectServices

LOGGER = get_logger(__name__)


class PipelineService:
    def __init__(self, project_services: "ProjectServices", artifacts_manager: RunArtifactsManager) -> None:
        self.project_services = project_services
        self.artifacts_manager = artifacts_manager

    def run_replay(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        artifact_file: Path,
        operation: str,
        limit: int,
        use_vector_search: bool | None = None,
    ) -> PipelineRunResult:
        run_id, run_label, run_dir = self.artifacts_manager.create_run_dir('pipeline')
        steps: list[PipelineStepRecord] = []

        build_report = self._run_step(
            steps,
            'index_build',
            f'Обновить индекс проекта {self.project_services.project_root.name}',
            lambda: asdict(self.project_services.build_index(full_rebuild=False)),
        )

        search_text = change_request.search_text()
        candidates_dicts = self._run_step(
            steps,
            'search_primary_target',
            f'Найти shortlist по change request: {change_request.title}',
            lambda: [asdict(item) for item in self.project_services.search(search_text, limit=limit, use_vector_search=use_vector_search)],
        )
        candidates = [SearchCandidate(**item) for item in candidates_dicts]
        if not any(item.qualname == selected_target for item in candidates):
            raise ValueError(f'Selected qualname {selected_target} is not present in shortlist')

        context_pack = self._run_step(
            steps,
            'context_supporting_code',
            f'Собрать context pack для {selected_target}',
            lambda: self.project_services.context(selected_target),
        )

        reference_artifacts = self._run_step(
            steps,
            'reference_retrieval',
            'Подобрать reference artifacts для генерации',
            lambda: self.project_services.retrieve_reference_artifacts(change_request, selected_target),
        )
        context_pack.reference_artifacts = reference_artifacts
        context_pack.reference_summary = {
            'count': len(reference_artifacts),
            'titles': [item.title for item in reference_artifacts],
            'content_modes': [item.content_mode for item in reference_artifacts],
        }

        generation_replay = self._run_step(
            steps,
            'generation_replay',
            'Подготовить пакет контекста и воспроизвести ответ внешнего генератора',
            lambda: self._generation_replay(change_request, selected_target, context_pack, artifact_file, operation),
        )

        apply_result = self._run_step(
            steps,
            'apply_staging',
            'Применить подготовленный артефакт в staging workspace',
            lambda: self.project_services.apply(
                PatchArtifact(
                    target_qualname=selected_target,
                    replacement_code=artifact_file.read_text(encoding='utf-8'),
                    operation=operation,
                )
            ),
        )

        merge_plan = self._run_step(
            steps,
            'merge_dry_run',
            'Подготовить dry-run план merge в master',
            lambda: self._prepare_merge_plan(apply_result),
        )

        result = PipelineRunResult(
            run_id=run_id,
            run_label=run_label,
            run_dir=run_dir,
            change_request=change_request,
            selected_target=selected_target,
            build_report=build_report,
            search_candidates=candidates,
            context_pack=context_pack,
            generation_replay=generation_replay,
            apply_result=apply_result,
            merge_plan=merge_plan,
            steps=steps,
        )
        self.artifacts_manager.write_bundle(run_dir, f'pipeline_run_{run_label}.json', result)
        return result


    def run_generate(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        limit: int,
        use_vector_search: bool | None = None,
    ) -> PipelineRunResult:
        run_id, run_label, run_dir = self.artifacts_manager.create_run_dir('pipeline')
        steps: list[PipelineStepRecord] = []

        build_report = self._run_step(
            steps,
            'index_build',
            f'Обновить индекс проекта {self.project_services.project_root.name}',
            lambda: asdict(self.project_services.build_index(full_rebuild=False)),
        )

        search_text = change_request.search_text()
        candidates_dicts = self._run_step(
            steps,
            'search_primary_target',
            f'Найти shortlist по change request: {change_request.title}',
            lambda: [asdict(item) for item in self.project_services.search(search_text, limit=limit, use_vector_search=use_vector_search)],
        )
        candidates = [SearchCandidate(**item) for item in candidates_dicts]
        if not any(item.qualname == selected_target for item in candidates):
            raise ValueError(f'Selected qualname {selected_target} is not present in shortlist')

        context_pack = self._run_step(
            steps,
            'context_supporting_code',
            f'Собрать context pack для {selected_target}',
            lambda: self.project_services.context(selected_target),
        )

        reference_artifacts = self._run_step(
            steps,
            'reference_retrieval',
            'Подобрать reference artifacts для генерации',
            lambda: self.project_services.retrieve_reference_artifacts(change_request, selected_target),
        )
        context_pack.reference_artifacts = reference_artifacts
        context_pack.reference_summary = {
            'count': len(reference_artifacts),
            'titles': [item.title for item in reference_artifacts],
            'content_modes': [item.content_mode for item in reference_artifacts],
        }

        external_generation = self._run_step(
            steps,
            'external_generate',
            'Вызвать внешний codegenerator и получить code artifact',
            lambda: self._external_generate(run_dir, change_request, selected_target, context_pack),
        )

        generated_tests = self._extract_generated_tests(external_generation.result_payload)
        generated_test_apply = None
        if generated_tests:
            generated_test_apply = {'applied_tests': [item['file_path'] for item in generated_tests], 'count': len(generated_tests)}

        repair_generation = None
        try:
            apply_result = self._run_step(
                steps,
                'apply_staging',
                'Применить сгенерированный артефакт в staging workspace',
                lambda: self.project_services.apply(patch_artifact_from_result(external_generation.result_payload, selected_target), generated_tests=generated_tests),
            )
            verification_report = self._run_step(
                steps,
                'verification',
                'Запустить проверки проекта после apply',
                lambda: self._run_verification(apply_result),
            )
        except Exception as exc:
            apply_failure_report = self._build_apply_failure_report(exc)
            if not self.project_services.config.codegenerator_repair_enabled or not apply_failure_report['failure_summary'].get('repairable', False):
                raise
            repair_generation = self._run_step(
                steps,
                'external_repair',
                'Вызвать внешний codegenerator repair после ошибки apply',
                lambda: self._external_repair(run_dir, change_request, selected_target, context_pack, external_generation.result_payload, apply_failure_report),
            )
            repair_generated_tests = self._extract_generated_tests(repair_generation.result_payload)
            if repair_generated_tests:
                generated_tests = repair_generated_tests
                generated_test_apply = {'applied_tests': [item['file_path'] for item in generated_tests], 'count': len(generated_tests)}
            apply_result = self._run_step(
                steps,
                'apply_repair_staging',
                'Применить исправленный артефакт в staging workspace',
                lambda: self.project_services.apply(patch_artifact_from_result(repair_generation.result_payload, selected_target), generated_tests=generated_tests),
            )
            verification_report = self._run_step(
                steps,
                'verification_after_repair',
                'Повторно запустить проверки проекта после repair',
                lambda: self._run_verification(apply_result),
            )
            verification_report = self._mark_noop_repair_if_needed(verification_report, apply_result, repair_generation)

        if (not verification_report.get('passed', False)) and self.project_services.config.codegenerator_repair_enabled and repair_generation is None:
            repair_generation = self._run_step(
                steps,
                'external_repair',
                'Вызвать внешний codegenerator repair после неуспешной проверки',
                lambda: self._external_repair(run_dir, change_request, selected_target, context_pack, external_generation.result_payload, verification_report),
            )
            repair_generated_tests = self._extract_generated_tests(repair_generation.result_payload)
            if repair_generated_tests:
                generated_tests = repair_generated_tests
                generated_test_apply = {'applied_tests': [item['file_path'] for item in generated_tests], 'count': len(generated_tests)}
            apply_result = self._run_step(
                steps,
                'apply_repair_staging',
                'Применить исправленный артефакт в staging workspace',
                lambda: self.project_services.apply(patch_artifact_from_result(repair_generation.result_payload, selected_target), generated_tests=generated_tests),
            )
            verification_report = self._run_step(
                steps,
                'verification_after_repair',
                'Повторно запустить проверки проекта после repair',
                lambda: self._run_verification(apply_result),
            )
            verification_report = self._mark_noop_repair_if_needed(verification_report, apply_result, repair_generation)

        merge_plan = self._run_step(
            steps,
            'merge_dry_run',
            'Подготовить dry-run план merge в master',
            lambda: self._prepare_merge_plan(apply_result, verification_report),
        )

        result = PipelineRunResult(
            run_id=run_id,
            run_label=run_label,
            run_dir=run_dir,
            change_request=change_request,
            selected_target=selected_target,
            build_report=build_report,
            search_candidates=candidates,
            context_pack=context_pack,
            generation_replay=None,
            external_generation=external_generation,
            generated_test_apply=generated_test_apply,
            verification_report=verification_report,
            repair_generation=repair_generation,
            apply_result=apply_result,
            merge_plan=merge_plan,
            steps=steps,
        )
        self.artifacts_manager.write_bundle(run_dir, f'pipeline_run_{run_label}.json', result)
        return result

    def _external_generate(self, run_dir: Path, change_request: ChangeRequest, selected_target: str, context_pack: ContextPack) -> ExternalGenerationCall:
        request_payload = build_generation_request(self.project_services.project_root, change_request, selected_target, context_pack, self.project_services.config)
        call_result = invoke_generate(run_dir, self.project_services.config, request_payload)
        return ExternalGenerationCall(
            mode='cli_json',
            command=call_result.command,
            request_path=call_result.request_path,
            result_path=call_result.result_path,
            trace_path=call_result.trace_path,
            request_payload=call_result.request_payload,
            result_payload=call_result.result_payload,
        )

    def _external_repair(self, run_dir: Path, change_request: ChangeRequest, selected_target: str, context_pack: ContextPack, previous_result_payload: dict[str, Any], verification_report: dict[str, Any]) -> ExternalGenerationCall:
        request_payload = build_repair_request(change_request, selected_target, previous_result_payload, context_pack, verification_report.get('failure_summary', {}))
        call_result = invoke_repair(run_dir, self.project_services.config, request_payload)
        return ExternalGenerationCall(
            mode='cli_json',
            command=call_result.command,
            request_path=call_result.request_path,
            result_path=call_result.result_path,
            trace_path=call_result.trace_path,
            request_payload=call_result.request_payload,
            result_payload=call_result.result_payload,
        )

    def _extract_generated_tests(self, result_payload: dict[str, Any]) -> list[dict[str, str]]:
        test_artifact = result_payload.get('test_artifact') or {}
        if not test_artifact:
            return []
        file_path = str(test_artifact.get('file_path', '')).strip()
        source_code = str(test_artifact.get('source_code', '') or '').strip()
        if not file_path or not source_code:
            return []
        return [{'file_path': file_path, 'source_code': source_code}]

    def _run_verification(self, apply_result: ApplyResult) -> dict[str, Any]:
        config = self.project_services.config
        results = self.project_services.validation_service.run_post_apply_checks(
            apply_result.workspace_path,
            apply_result.impact.recommended_tests,
            run_ruff=config.verification_run_ruff,
            run_recommended_tests=config.verification_run_recommended_tests,
            run_full_project_tests=config.verification_run_full_project_tests,
        )
        passed = self.project_services.validation_service.verification_passed(results)
        failure_summary = self.project_services.validation_service.build_failure_summary(results)
        return {'passed': passed, 'results': results, 'failure_summary': failure_summary}
    def _build_apply_failure_report(self, exc: Exception) -> dict[str, Any]:
        return {
            'passed': False,
            'results': {
                'apply_generated_artifact': {
                    'ok': False,
                    'error_type': type(exc).__name__,
                    'message': str(exc),
                }
            },
            'failure_summary': {
                'failed_checks': ['apply_generated_artifact'],
                'repairable': True,
                'messages': [str(exc)],
                'stage': 'apply',
            },
        }

    def _mark_noop_repair_if_needed(self, verification_report: dict[str, Any], apply_result: ApplyResult, repair_generation: ExternalGenerationCall | None) -> dict[str, Any]:
        if repair_generation is None:
            return verification_report
        diff_text = apply_result.diff.unified_diff or ''
        if diff_text.strip():
            return verification_report
        verification_report = dict(verification_report)
        results = dict(verification_report.get('results', {}))
        results['repair_intent'] = {
            'ok': False,
            'message': 'Repair produced no effective change relative to the original project code.',
        }
        failure_summary = dict(verification_report.get('failure_summary', {}))
        failed_checks = list(failure_summary.get('failed_checks', []))
        if 'repair_intent' not in failed_checks:
            failed_checks.append('repair_intent')
        messages = list(failure_summary.get('messages', []))
        messages.append('Repair lost requested change intent: resulting diff is empty.')
        failure_summary.update({
            'failed_checks': failed_checks,
            'messages': messages,
            'repairable': False,
            'stage': 'repair',
        })
        verification_report['passed'] = False
        verification_report['results'] = results
        verification_report['failure_summary'] = failure_summary
        return verification_report


    def _generation_replay(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        context_pack: ContextPack,
        artifact_file: Path,
        operation: str,
    ) -> GenerationReplay:
        context_excerpt = {
            'target_qualname': context_pack.target.qualname,
            'target_file_path': context_pack.target.file_path,
            'knowledge_title': context_pack.knowledge_title,
            'knowledge_description': context_pack.knowledge_description,
            'requirement_ids': context_pack.requirement_ids,
            'recommended_tests': list(context_pack.recommended_tests),
            'related_tests': [item.qualname for item in context_pack.related_tests],
            'inbound_relations': [
                relation.source_qualname for relation in context_pack.inbound_relations[:8]
            ],
            'outbound_relations': [
                relation.target_qualname or relation.target_ref for relation in context_pack.outbound_relations[:8]
            ],
            'reference_summary': context_pack.reference_summary,
            'reference_artifacts': [
                {
                    'artifact_id': item.artifact_id,
                    'title': item.title,
                    'artifact_type': item.artifact_type,
                    'usage_mode': item.usage_mode,
                    'content_mode': item.content_mode,
                    'why_selected': item.why_selected,
                    'source_path': item.source_path,
                    'content': item.content,
                }
                for item in context_pack.reference_artifacts
            ],
        }
        return GenerationReplay(
            mode='replay_from_artifact_file',
            change_request=change_request,
            selected_target=selected_target,
            operation=operation,
            context_excerpt=context_excerpt,
            artifact_source=str(artifact_file),
            replacement_code=artifact_file.read_text(encoding='utf-8'),
        )

    def _prepare_merge_plan(self, apply_result: ApplyResult, verification_report: dict[str, Any] | None = None) -> MergePlan:
        verification_ok = True if verification_report is None else bool(verification_report.get('passed', False))
        ready_for_manual_merge_review = apply_result.validation.is_valid and verification_ok
        summary_lines = [
            'Режим merge: dry-run, без копирования изменений в master.',
            'Структурная валидация и тестовые проверки прошли, но итоговое решение о merge принимает человек.' if ready_for_manual_merge_review else 'Есть ошибки валидации или тестов, merge не рекомендуется без дополнительной проверки.',
            f'Измененные файлы: {", ".join(apply_result.impact.changed_files) or "—"}',
            f'Символы в измененных файлах: {", ".join(apply_result.impact.symbols_in_changed_files) or "—"}',
            f'Связанные требования: {", ".join(apply_result.impact.linked_requirements) or "—"}',
            f'Рекомендуемые тесты: {", ".join(apply_result.impact.recommended_tests) or "—"}',
            f'Проверки после apply: {"passed" if verification_ok else "failed"}',
        ]
        return MergePlan(
            mode='dry_run',
            ready_for_manual_merge_review=ready_for_manual_merge_review,
            workspace_path=str(apply_result.workspace_path),
            changed_files=apply_result.impact.changed_files,
            symbols_in_changed_files=apply_result.impact.symbols_in_changed_files,
            linked_requirements=apply_result.impact.linked_requirements,
            recommended_tests=apply_result.impact.recommended_tests,
            recommended_test_commands=apply_result.impact.recommended_test_commands,
            summary_lines=summary_lines,
        )

    def _run_step(self, steps, step_name, summary, func):
        started = datetime.now(tz=UTC)
        started_perf = perf_counter()
        status = 'ok'
        try:
            payload = func()
        except Exception:
            status = 'error'
            finished = datetime.now(tz=UTC)
            steps.append(PipelineStepRecord(
                step_name=step_name,
                status=status,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                duration_ms=int((perf_counter() - started_perf) * 1000),
                summary=summary,
            ))
            raise
        finished = datetime.now(tz=UTC)
        steps.append(PipelineStepRecord(
            step_name=step_name,
            status=status,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=int((perf_counter() - started_perf) * 1000),
            summary=summary,
        ))
        return payload
