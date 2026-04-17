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
    PipelineExecutionSummary,
    PipelineRunResult,
    PipelineStepRecord,
    SearchCandidate,
)
from codecollector.logger import get_logger
from codecollector.orchestration.run_artifacts import RunArtifactsManager
from codecollector.external_codegen.adapter import (
    build_generation_request,
    invoke_generate,
    invoke_generate_test,
    patch_artifact_from_result,
    build_repair_request,
    invoke_repair,
    ensure_expected_operation,
)

if TYPE_CHECKING:
    from codecollector.domain.models import ApplyResult, ContextPack
    from codecollector.orchestration.services import ProjectServices

LOGGER = get_logger(__name__)

class PipelineRunFailed(RuntimeError):
    def __init__(self, message: str, result: PipelineRunResult) -> None:
        super().__init__(message)
        self.result = result
        
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
        self.project_services.reset_embedding_usage()
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


    def run_repair(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        previous_result_payload: dict[str, Any],
        verification_report: dict[str, Any],
        requested_operation: str = 'replace_symbol',
    ) -> PipelineRunResult:
        self.project_services.reset_embedding_usage()
        run_id, run_label, run_dir = self.artifacts_manager.create_run_dir('pipeline')
        steps: list[PipelineStepRecord] = []

        build_report: dict[str, Any] | None = None
        context_pack: ContextPack | None = None
        repair_generation: ExternalGenerationCall | None = None
        apply_result: ApplyResult | None = None
        verification_result: dict[str, Any] | None = None
        merge_plan: MergePlan | None = None
        result: PipelineRunResult | None = None

        try:
            build_report = self._run_step(
                steps,
                'index_build',
                f'Обновить индекс проекта {self.project_services.project_root.name}',
                lambda: asdict(self.project_services.build_index(full_rebuild=False)),
            )

            self._record_skipped_step(
                steps,
                'search_primary_target',
                f'Повторный shortlist пропущен: repair использует выбранный target {selected_target}',
            )

            context_pack = self._run_step(
                steps,
                'context_supporting_code',
                f'Собрать context pack для {selected_target}',
                lambda: self.project_services.context(selected_target),
            )

            reference_artifacts = self._run_step(
                steps,
                'reference_retrieval',
                'Подобрать reference artifacts для repair',
                lambda: self.project_services.retrieve_reference_artifacts(change_request, selected_target),
            )
            context_pack.reference_artifacts = reference_artifacts
            context_pack.reference_summary = {
                'count': len(reference_artifacts),
                'titles': [item.title for item in reference_artifacts],
                'content_modes': [item.content_mode for item in reference_artifacts],
            }

            repair_generation, repair_result_payload = self._run_step(
                steps,
                'external_repair',
                'Вызвать внешний codegenerator repair по запросу пользователя',
                lambda: self._external_repair(
                    run_dir,
                    change_request,
                    selected_target,
                    context_pack,
                    previous_result_payload,
                    verification_report,
                    requested_operation=requested_operation,
                ),
            )
            if not self._has_code_artifact(repair_result_payload):
                raise RuntimeError(repair_result_payload.get('message') or 'codegenerator repair did not return code_artifact')

            apply_result = self._replace_active_apply_result(
                apply_result,
                self._run_step(
                    steps,
                    'apply_repair_staging',
                    'Применить исправленный артефакт в staging workspace',
                    lambda: self.project_services.apply(
                        patch_artifact_from_result(repair_result_payload, selected_target),
                        generated_tests=[],
                    ),
                ),
            )

            verification_result = self._run_step(
                steps,
                'verification_after_repair',
                'Запустить проверки проекта после ручного repair',
                lambda: self._run_verification(apply_result),
            )
            verification_result = self._mark_noop_repair_if_needed(verification_result, apply_result, repair_generation)

            merge_plan = self._run_step(
                steps,
                'merge_dry_run',
                'Подготовить dry-run план merge в master',
                lambda: self._prepare_merge_plan(apply_result, verification_result),
            )

            result = self._build_partial_run_result(
                run_id=run_id,
                run_label=run_label,
                run_dir=run_dir,
                change_request=change_request,
                selected_target=selected_target,
                build_report=build_report,
                candidates=[],
                context_pack=context_pack,
                generation_replay=None,
                external_code_generation=None,
                external_test_generation=None,
                generated_test_apply=None,
                verification_report=verification_result,
                repair_generation=repair_generation,
                apply_result=apply_result,
                merge_plan=merge_plan,
                steps=steps,
                warnings=[],
            )
            return result
        finally:
            if result is None:
                result = self._build_partial_run_result(
                    run_id=run_id,
                    run_label=run_label,
                    run_dir=run_dir,
                    change_request=change_request,
                    selected_target=selected_target,
                    build_report=build_report,
                    candidates=[],
                    context_pack=context_pack,
                    generation_replay=None,
                    external_code_generation=None,
                    external_test_generation=None,
                    generated_test_apply=None,
                    verification_report=verification_result,
                    repair_generation=repair_generation,
                    apply_result=apply_result,
                    merge_plan=merge_plan,
                    steps=steps,
                    warnings=[],
                )
            try:
                self.artifacts_manager.write_bundle(run_dir, f'pipeline_run_{run_label}.json', result)
            except Exception:
                LOGGER.exception('Failed to write repair pipeline run bundle in finally: %s', run_dir)

    def run_generate(
        self,
        change_request: ChangeRequest,
        selected_target: str,
        limit: int,
        use_vector_search: bool | None = None,
        skip_search: bool = False,
        requested_operation: str = 'replace_symbol',
    ) -> PipelineRunResult:
        self.project_services.reset_embedding_usage()
        run_id, run_label, run_dir = self.artifacts_manager.create_run_dir('pipeline')
        steps: list[PipelineStepRecord] = []
        warnings: list[str] = []

        build_report: dict[str, Any] | None = None
        candidates: list[SearchCandidate] = []
        context_pack: ContextPack | None = None
        external_code_generation: ExternalGenerationCall | None = None
        external_test_generation: ExternalGenerationCall | None = None
        generated_test_apply: dict[str, Any] | None = None
        verification_report: dict[str, Any] | None = None
        repair_generation: ExternalGenerationCall | None = None
        apply_result: ApplyResult | None = None
        merge_plan: MergePlan | None = None
        result: PipelineRunResult | None = None
        caught_exc: Exception | None = None

        try:
            build_report = self._run_step(
                steps,
                'index_build',
                f'Обновить индекс проекта {self.project_services.project_root.name}',
                lambda: asdict(self.project_services.build_index(full_rebuild=False)),
            )

            candidates: list[SearchCandidate] = []
            if skip_search:
                self._record_skipped_step(
                    steps,
                    'search_primary_target',
                    f'Повторный shortlist пропущен: используется заранее выбранный target {selected_target}',
                )
            else:
                search_text = change_request.search_text()
                candidates_dicts = self._run_step(
                    steps,
                    'search_primary_target',
                    f'Найти shortlist по change request: {change_request.title}',
                    lambda: [asdict(item) for item in self.project_services.search(search_text, limit=limit, use_vector_search=use_vector_search)],
                )
                candidates = [SearchCandidate(**item) for item in candidates_dicts]
                if not any(item.qualname == selected_target for item in candidates):
                    warning_message = (
                        f'Selected qualname {selected_target} отсутствует в shortlist; '
                        'продолжаем по явно указанному selected_target.'
                    )
                    warnings.append(warning_message)
                    LOGGER.warning(warning_message)

            context_pack = self._run_step(
                steps,
                'context_supporting_code',
                f'Собрать context pack для {selected_target}',
                lambda: self.project_services.context(selected_target),
            )
            if context_pack is None or context_pack.target is None:
                raise ValueError(f'Не удалось собрать context pack для {selected_target}')

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

            external_code_generation, external_code_result_payload = self._run_step(
                steps,
                'external_generate',
                'Вызвать внешний codegenerator и получить code artifact',
                lambda: self._external_generate(
                    run_dir,
                    change_request,
                    selected_target,
                    context_pack,
                    requested_operation,
                ),
            )
            if not self._has_code_artifact(external_code_result_payload):
                raise RuntimeError(external_code_result_payload.get('message') or 'codegenerator generate did not return code_artifact')
            
            ensure_expected_operation(external_code_result_payload, requested_operation)

            final_payload = external_code_result_payload
            generated_tests: list[dict[str, str]] = []

            try:
                apply_result = self._replace_active_apply_result(
                    apply_result,
                    self._run_step(
                        steps,
                        'apply_staging',
                        'Применить сгенерированный артефакт в staging workspace',
                        lambda: self.project_services.apply(
                            patch_artifact_from_result(final_payload, selected_target),
                            generated_tests=[],
                        ),
                    ),
                )
            except Exception as exc:
                apply_failure_report = self._build_apply_failure_report(exc)
                if not self.project_services.config.codegenerator_repair_enabled or not apply_failure_report['failure_summary'].get('repairable', False):
                    raise
                repair_generation, repair_result_payload = self._run_step(
                    steps,
                    'external_repair',
                    'Вызвать внешний codegenerator repair после ошибки apply',
                    lambda: self._external_repair(
                        run_dir,
                        change_request,
                        selected_target,
                        context_pack,
                        external_code_result_payload,
                        apply_failure_report,
                        requested_operation=requested_operation,
                    ),
                )
                if not self._has_code_artifact(repair_result_payload):
                    raise RuntimeError(repair_result_payload.get('message') or 'codegenerator repair did not return code_artifact')
                final_payload = repair_result_payload
                apply_result = self._replace_active_apply_result(
                    apply_result,
                    self._run_step(
                        steps,
                        'apply_repair_staging',
                        'Применить исправленный артефакт в staging workspace',
                        lambda: self.project_services.apply(
                            patch_artifact_from_result(final_payload, selected_target),
                            generated_tests=[],
                        ),
                    ),
                )

            if self._should_request_generated_test(context_pack, selected_target):
                external_test_generation, external_test_result_payload = self._run_step(
                    steps,
                    'external_generate_test',
                    'Вызвать внешний codegenerator для генерации теста',
                    lambda: self._external_generate_test(
                        run_dir,
                        change_request,
                        selected_target,
                        context_pack,
                        final_payload,
                        requested_operation=requested_operation,
                    ),
                )
                generated_tests = self._extract_generated_tests(external_test_result_payload)
                if generated_tests:
                    generated_test_apply = {
                        'applied_tests': [item['file_path'] for item in generated_tests],
                        'count': len(generated_tests),
                    }
                    apply_result = self._replace_active_apply_result(
                        apply_result,
                        self._run_step(
                            steps,
                            'apply_with_generated_tests',
                            'Повторно применить артефакт вместе с сгенерированными тестами в staging workspace',
                            lambda: self.project_services.apply(
                                patch_artifact_from_result(final_payload, selected_target),
                                generated_tests=generated_tests,
                            ),
                        ),
                    )
            else:
                warning_message = 'Генерация теста пропущена: для target уже есть related_tests или recommended_tests.'
                warnings.append(warning_message)
                LOGGER.info(warning_message)

            verification_step_name = 'verification_after_repair' if repair_generation is not None else 'verification'
            verification_step_summary = 'Повторно запустить проверки проекта после repair' if repair_generation is not None else 'Запустить проверки проекта после apply'
            verification_report = self._run_step(
                steps,
                verification_step_name,
                verification_step_summary,
                lambda: self._run_verification(apply_result),
            )
            if repair_generation is not None:
                verification_report = self._mark_noop_repair_if_needed(verification_report, apply_result, repair_generation)

            if (not verification_report.get('passed', False)) and self.project_services.config.codegenerator_repair_enabled and repair_generation is None:
                if self._generated_test_failure_only(verification_report):
                    warning_message = 'Repair пропущен: упал только сгенерированный тест, основной код не отправляется в repair.'
                    warnings.append(warning_message)
                    LOGGER.warning(warning_message)
                else:
                    repair_generation, repair_result_payload = self._run_step(
                        steps,
                        'external_repair',
                        'Вызвать внешний codegenerator repair после неуспешной проверки',
                        lambda: self._external_repair(
                            run_dir, 
                            change_request, 
                            selected_target, 
                            context_pack, 
                            final_payload, 
                            verification_report,
                            requested_operation=requested_operation,
                        ),
                    )
                    if not self._has_code_artifact(repair_result_payload):
                        raise RuntimeError(repair_result_payload.get('message') or 'codegenerator repair did not return code_artifact')
                    final_payload = repair_result_payload
                    apply_result = self._replace_active_apply_result(
                        apply_result,
                        self._run_step(
                            steps,
                            'apply_repair_staging',
                            'Применить исправленный артефакт в staging workspace',
                            lambda: self.project_services.apply(
                                patch_artifact_from_result(final_payload, selected_target),
                                generated_tests=generated_tests,
                            ),
                        ),
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

            result = self._build_partial_run_result(
                run_id=run_id,
                run_label=run_label,
                run_dir=run_dir,
                change_request=change_request,
                selected_target=selected_target,
                build_report=build_report,
                candidates=candidates,
                context_pack=context_pack,
                generation_replay=None,
                external_code_generation=external_code_generation,
                external_test_generation=external_test_generation,
                generated_test_apply=generated_test_apply,
                verification_report=verification_report,
                repair_generation=repair_generation,
                apply_result=apply_result,
                merge_plan=merge_plan,
                steps=steps,
                warnings=warnings,
            )
            return result

        except Exception as exc:
            caught_exc = exc
        finally:
            if result is None:
                result = self._build_partial_run_result(
                    run_id=run_id,
                    run_label=run_label,
                    run_dir=run_dir,
                    change_request=change_request,
                    selected_target=selected_target,
                    build_report=build_report,
                    candidates=candidates,
                    context_pack=context_pack,
                    generation_replay=None,
                    external_code_generation=external_code_generation,
                    external_test_generation=external_test_generation,
                    generated_test_apply=generated_test_apply,
                    verification_report=verification_report,
                    repair_generation=repair_generation,
                    apply_result=apply_result,
                    merge_plan=merge_plan,
                    steps=steps,
                    warnings=warnings,
                )
            try:
                self.artifacts_manager.write_bundle(run_dir, f'pipeline_run_{run_label}.json', result)
            except Exception:
                LOGGER.exception('Failed to write pipeline run bundle in finally: %s', run_dir)
        if caught_exc is not None:
            assert result is not None
            failed_step = next((step for step in reversed(steps) if step.status == 'error'), None)
            if failed_step is not None:
                message = f'{failed_step.step_name} failed: {caught_exc}'
            else:
                message = str(caught_exc)
            raise PipelineRunFailed(message, result) from caught_exc

        assert result is not None
        return result        

    def _delete_apply_workspace(self, apply_result: ApplyResult | None) -> None:
        if apply_result is None:
            return
        try:
            self.project_services.apply_service.staging.delete_workspace(apply_result.workspace_path)
        except Exception:
            LOGGER.exception('Failed to delete superseded staging workspace: %s', apply_result.workspace_path)

    def _replace_active_apply_result(self, current: ApplyResult | None, new_result: ApplyResult) -> ApplyResult:
        if current is not None and current.workspace_path != new_result.workspace_path:
            self._delete_apply_workspace(current)
        return new_result

    def _has_code_artifact(self, result_payload: dict[str, Any]) -> bool:
        code_artifact = result_payload.get('code_artifact') or {}
        return bool(str(code_artifact.get('code', '') or '').strip())

    def _should_request_generated_test(self, context_pack: ContextPack, selected_target: str) -> bool:
        mode = str(self.project_services.config.codegenerator_test_generation_mode).lower()
        if mode == 'never':
            return False
        if self._has_existing_tests_for_target(context_pack, selected_target):
            return False
        if mode == 'always':
            return True
        return True
    
    def _has_existing_tests_for_target(self, context_pack: ContextPack, selected_target: str) -> bool:
        target_name = selected_target.split('.')[-1].lower()
        target_qualname = selected_target.lower()

        def _entry_matches(entry: Any) -> bool:
            if not isinstance(entry, dict):
                return False
            qualname = str(entry.get('qualname', '') or '').lower()
            file_path = str(entry.get('file_path', '') or '').lower()
            reasons = [str(item).lower() for item in (entry.get('reasons') or [])]
            if target_qualname and target_qualname in qualname:
                return True
            if target_name and target_name in qualname:
                return True
            if target_name and any(target_name in reason for reason in reasons):
                return True
            if target_name and target_name in file_path:
                return True
            return False

        return any(_entry_matches(item) for item in (context_pack.related_tests or [])) or any(
            _entry_matches(item) for item in (context_pack.recommended_tests or [])
        )

    def _generated_test_failure_only(self, verification_report: dict[str, Any] | None) -> bool:
        if not verification_report or verification_report.get('passed', False):
            return False

        results = verification_report.get('results', {}) or {}
        failed_results = {
            name: payload for name, payload in results.items()
            if not bool((payload or {}).get('ok', False))
        }
        if not failed_results:
            return False

        generated_test_failure = False
        non_generated_test_failure = False

        for check_name, payload in failed_results.items():
            payload = payload or {}
            command = payload.get('command') or []
            command_text = " ".join(str(item) for item in command)

            if check_name.startswith('pytest') and 'test_generated_' in command_text:
                generated_test_failure = True
            else:
                non_generated_test_failure = True

        return generated_test_failure and not non_generated_test_failure
    
    def _resolve_verification_status(
        self,
        verification_report: dict[str, Any] | None,
    ) -> str | None:
        if verification_report is None:
            return None
        if bool(verification_report.get('passed')):
            return 'passed'
        if self._generated_test_failure_only(verification_report):
            return 'generated_test_verification_failed'
        return 'verification_failed'    

    def _build_partial_run_result(
        self,
        run_id: str,
        run_label: str,
        run_dir: Path,
        change_request: ChangeRequest,
        selected_target: str,
        steps: list[PipelineStepRecord],
        build_report: dict[str, Any] | None = None,
        candidates: list[SearchCandidate] | None = None,
        context_pack: ContextPack | None = None,
        generation_replay: GenerationReplay | None = None,
        external_code_generation: ExternalGenerationCall | None = None,
        external_test_generation: ExternalGenerationCall | None = None,
        generated_test_apply: dict[str, Any] | None = None,
        verification_report: dict[str, Any] | None = None,
        repair_generation: ExternalGenerationCall | None = None,
        apply_result: ApplyResult | None = None,
        merge_plan: MergePlan | None = None,
        warnings: list[str] | None = None,
    ) -> PipelineRunResult:
        usage_summary = self._build_usage_summary(
            external_code_generation,
            external_test_generation,
            repair_generation,
        )
        execution_summary = self._build_execution_summary(
            selected_target=selected_target,
            requested_operation=(
                external_code_generation.request_summary.get('target', {}).get('operation')
                if external_code_generation is not None else None
            ),
            apply_result=apply_result,
            verification_report=verification_report,
            merge_plan=merge_plan,
            generated_test_apply=generated_test_apply,
            external_code_generation=external_code_generation,
            external_test_generation=external_test_generation,
            repair_generation=repair_generation,
            usage_summary=usage_summary,
        )     
        return PipelineRunResult(
            run_id=run_id,
            run_label=run_label,
            run_dir=run_dir,
            change_request=change_request,
            selected_target=selected_target,
            build_report=build_report,
            search_candidates=candidates or [],
            context_pack=context_pack,
            generation_replay=generation_replay,
            external_code_generation=external_code_generation,
            external_test_generation=external_test_generation,
            generated_test_apply=generated_test_apply,
            verification_report=verification_report,
            repair_generation=repair_generation,
            apply_result=apply_result,
            merge_plan=merge_plan,
            steps=steps,
            warnings=warnings or [],
            execution_summary=execution_summary,
        )

    def _external_generate_test(
        self,
        run_dir: Path,
        change_request: ChangeRequest,
        selected_target: str,
        context_pack: ContextPack,
        final_payload: dict[str, Any],
        requested_operation: str = 'replace_symbol',
    ) -> tuple[ExternalGenerationCall, dict[str, Any]]:
        request_payload = build_generation_request(
            self.project_services.project_root,
            change_request,
            selected_target,
            context_pack,
            self.project_services.config,
            generated_code_artifact=final_payload.get("code_artifact") or {},
            mode='generate_test',
            operation=requested_operation,
        )
        request_payload['request_id'] = f'generate-test-{selected_target.split(".")[-1]}'
        request_payload['mode'] = 'generate_test'
        request_payload.setdefault('options', {})['generate_test_mode'] = 'always'
        call_result = invoke_generate_test(run_dir, self.project_services.config, request_payload)
        external_call = ExternalGenerationCall(
            mode='cli_json',
            command=call_result.command,
            request_path=call_result.request_path,
            result_path=call_result.result_path,
            trace_path=call_result.trace_path,
            request_summary=self._summarize_external_request(call_result.request_payload),
            result_summary=self._summarize_external_result(call_result.result_payload),
        )
        return external_call, call_result.result_payload

    def _external_generate(
        self,
        run_dir: Path,
        change_request: ChangeRequest,
        selected_target: str,
        context_pack: ContextPack,
        operation: str = 'replace_symbol',
    ) -> tuple[ExternalGenerationCall, dict[str, Any]]:
        request_payload = build_generation_request(
            self.project_services.project_root,
            change_request,
            selected_target,
            context_pack,
            self.project_services.config,
            operation=operation,
        )
        request_payload.setdefault('options', {})['generate_test_mode'] = 'never'
        call_result = invoke_generate(run_dir, self.project_services.config, request_payload)
        external_call = ExternalGenerationCall(
            mode='cli_json',
            command=call_result.command,
            request_path=call_result.request_path,
            result_path=call_result.result_path,
            trace_path=call_result.trace_path,
            request_summary=self._summarize_external_request(call_result.request_payload),
            result_summary=self._summarize_external_result(call_result.result_payload),
        )
        return external_call, call_result.result_payload

    def _external_repair(
        self,
        run_dir: Path,
        change_request: ChangeRequest,
        selected_target: str,
        context_pack: ContextPack,
        previous_result_payload: dict[str, Any],
        verification_report: dict[str, Any],
        requested_operation: str = 'replace_symbol',
    ) -> tuple[ExternalGenerationCall, dict[str, Any]]:
        request_payload = build_repair_request(
            change_request,
            selected_target,
            previous_result_payload,
            context_pack,
            verification_report.get('failure_summary', {}),
            self.project_services.config,
            requested_operation=requested_operation,
        )
        call_result = invoke_repair(run_dir, self.project_services.config, request_payload)
        external_call = ExternalGenerationCall(
            mode='cli_json',
            command=call_result.command,
            request_path=call_result.request_path,
            result_path=call_result.result_path,
            trace_path=call_result.trace_path,
            request_summary=self._summarize_external_request(call_result.request_payload),
            result_summary=self._summarize_external_result(call_result.result_payload),
        )
        return external_call, call_result.result_payload

    def _extract_generated_tests(self, result_payload: dict[str, Any]) -> list[dict[str, str]]:
        test_artifact = result_payload.get('test_artifact') or {}
        if not test_artifact:
            return []
        file_path = str(test_artifact.get('file_path', '')).strip()
        source_code = str(test_artifact.get('source_code', '') or '').strip()
        if not file_path or not source_code:
            return []
        return [{'file_path': file_path, 'source_code': source_code}]
    
    def _summarize_external_request(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        target = dict(request_payload.get('target') or {})
        project_context = dict(request_payload.get('project_context') or {})
        reference_context = dict(request_payload.get('reference_context') or {})
        generated_code_artifact = dict(request_payload.get('generated_code_artifact') or {})

        return {
            'request_id': request_payload.get('request_id'),
            'mode': request_payload.get('mode'),
            'target': {
                'qualname': target.get('qualname'),
                'file_path': target.get('file_path'),
                'operation': target.get('operation'),
            },
            'project_context_summary': {
                'module_outline_count': len(project_context.get('module_outline') or []),
                'full_file_included': bool(project_context.get('full_file_source')),
                'target_symbol_qualname': (project_context.get('target_symbol') or {}).get('qualname'),
                'related_tests_count': len(project_context.get('related_tests') or []),
                'recommended_tests_count': len(project_context.get('recommended_tests') or []),
            },
            'reference_context_summary': {
                'reference_artifacts_count': len(reference_context.get('reference_artifacts') or []),
            },
            'has_generated_code_artifact': bool(generated_code_artifact),
            'options': dict(request_payload.get('options') or {}),
        }    

    def _summarize_external_result(self, result_payload: dict[str, Any]) -> dict[str, Any]:
        code_artifact = dict(result_payload.get('code_artifact') or {})
        test_artifact = dict(result_payload.get('test_artifact') or {})
        llm_usage = dict(result_payload.get('llm_usage') or {})

        return {
            'request_id': result_payload.get('request_id'),
            'status': result_payload.get('status'),
            'has_code_artifact': bool(code_artifact.get('code')),
            'code_artifact_summary': {
                'operation': code_artifact.get('operation'),
                'target_qualname': code_artifact.get('target_qualname'),
                'target_file': code_artifact.get('target_file'),
                'insert_after': code_artifact.get('insert_after'),
                'code_chars': len(str(code_artifact.get('code') or '')),
            } if code_artifact else {},
            'has_test_artifact': bool(test_artifact.get('source_code')),
            'test_artifact_summary': {
                'file_path': test_artifact.get('file_path'),
                'source_code_chars': len(str(test_artifact.get('source_code') or '')),
            } if test_artifact else {},
            'warnings_count': len(result_payload.get('warnings') or []),
            'trace_path': result_payload.get('trace_path'),
            'llm_usage': llm_usage,
            'error_type': result_payload.get('error_type'),
            'message': result_payload.get('message'),
        }

    def _extract_external_llm_usage(self, call: ExternalGenerationCall | None) -> dict[str, Any] | None:
        if call is None:
            return None
        result_summary = getattr(call, 'result_summary', None) or {}
        llm_usage = result_summary.get('llm_usage') or {}
        if not llm_usage:
            return None
        return dict(llm_usage)
    
    def _sum_usage_dicts(self, *usage_items: dict[str, Any] | None) -> dict[str, Any]:
        totals = {
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
        for usage in usage_items:
            if not usage:
                continue
            totals['calls'] += int(usage.get('calls', 0) or 0)
            totals['prompt_tokens'] += float(usage.get('prompt_tokens', 0.0) or 0.0)
            totals['output_tokens'] += float(usage.get('output_tokens', 0.0) or 0.0)
            totals['total_tokens'] += float(usage.get('total_tokens', 0.0) or 0.0)
            totals['duration_sec'] += float(usage.get('duration_sec', 0.0) or 0.0)
            totals['total_duration_sec'] += float(usage.get('total_duration_sec', 0.0) or 0.0)
            totals['load_duration_sec'] += float(usage.get('load_duration_sec', 0.0) or 0.0)
            totals['prompt_eval_duration_sec'] += float(usage.get('prompt_eval_duration_sec', 0.0) or 0.0)
            totals['eval_duration_sec'] += float(usage.get('eval_duration_sec', 0.0) or 0.0)
        return totals

    def _build_usage_summary(
        self,
        external_code_generation: ExternalGenerationCall | None,
        external_test_generation: ExternalGenerationCall | None,
        repair_generation: ExternalGenerationCall | None,
    ) -> dict[str, Any]:
        embedding_usage = None
        if hasattr(self.project_services, 'get_embedding_usage_summary'):
            try:
                embedding_usage = self.project_services.get_embedding_usage_summary()
            except Exception:
                LOGGER.exception('Failed to collect embedding usage summary')
                embedding_usage = None

        code_generation_usage = self._extract_external_llm_usage(external_code_generation)
        test_generation_usage = self._extract_external_llm_usage(external_test_generation)
        repair_generation_usage = self._extract_external_llm_usage(repair_generation)

        llm_total = self._sum_usage_dicts(
            code_generation_usage,
            test_generation_usage,
            repair_generation_usage,
        )

        overall_total_tokens = (
            float((embedding_usage or {}).get('prompt_tokens', 0.0) or 0.0)
            + float(llm_total.get('total_tokens', 0.0) or 0.0)
        )

        return {
            'embedding': embedding_usage,
            'code_generation': code_generation_usage,
            'test_generation': test_generation_usage,
            'repair_generation': repair_generation_usage,
            'llm_total': llm_total,
            'overall_total_tokens_including_embeddings': overall_total_tokens,
          }

    def _build_execution_summary(
        self,
        *,
        selected_target: str,
        requested_operation: str | None,
        apply_result: ApplyResult | None,
        verification_report: dict[str, Any] | None,
        merge_plan: MergePlan | None,
        generated_test_apply: dict[str, Any] | None,
        external_code_generation: ExternalGenerationCall | None,
        external_test_generation: ExternalGenerationCall | None,
        repair_generation: ExternalGenerationCall | None,
        usage_summary: dict[str, Any] | None,
    ) -> PipelineExecutionSummary:
        code_result_summary = (external_code_generation.result_summary if external_code_generation else {}) or {}
        code_artifact_summary = (code_result_summary.get('code_artifact_summary') or {}) if isinstance(code_result_summary, dict) else {}
        final_operation = code_artifact_summary.get('operation') or requested_operation

        changed_files = []
        symbols_in_changed_files = []
        workspace_path = None
        linked_requirements: list[str] = []
        recommended_tests: list[str] = []
        recommended_test_commands: list[str] = []

        if apply_result is not None:
            changed_files = list(apply_result.impact.changed_files)
            symbols_in_changed_files = list(apply_result.impact.symbols_in_changed_files)
            workspace_path = str(apply_result.workspace_path)
            linked_requirements = list(apply_result.impact.linked_requirements)
            recommended_tests = list(apply_result.impact.recommended_tests)
            recommended_test_commands = list(apply_result.impact.recommended_test_commands)

        verification_passed = None if verification_report is None else bool(verification_report.get('passed', False))
        verification_status = self._resolve_verification_status(verification_report)
        has_generated_test = bool((generated_test_apply or {}).get('count', 0))
        generated_test_files = list((generated_test_apply or {}).get('applied_tests') or [])
        repair_used = repair_generation is not None
        merge_mode = None if merge_plan is None else merge_plan.mode
        merge_ready = None if merge_plan is None else bool(merge_plan.ready_for_manual_merge_review)

        if merge_ready:
            status = 'ready_for_merge_review'
        elif verification_status == 'generated_test_verification_failed':
            status = 'generated_test_verification_failed'
        elif verification_status == 'verification_failed':
            status = 'verification_failed'
        elif apply_result is not None:
            status = 'applied'
        elif external_code_generation is not None:
            status = 'generated'
        else:
            status = 'incomplete'

        return PipelineExecutionSummary(
            status=status,
            selected_target=selected_target,
            requested_operation=requested_operation,
            final_operation=final_operation,
            changed_files=changed_files,
            symbols_in_changed_files=symbols_in_changed_files,
            workspace_path=workspace_path,
            verification_passed=verification_passed,
            has_generated_test=has_generated_test,
            generated_test_files=generated_test_files,
            repair_used=repair_used,
            merge_mode=merge_mode,
            merge_ready=merge_ready,
            linked_requirements=linked_requirements,
            recommended_tests=recommended_tests,
            recommended_test_commands=recommended_test_commands,
            code_generation_usage=(usage_summary or {}).get('code_generation'),
            test_generation_usage=(usage_summary or {}).get('test_generation'),
            repair_generation_usage=(usage_summary or {}).get('repair_generation'),
            embedding_usage=(usage_summary or {}).get('embedding'),
        )

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


    def _record_skipped_step(self, steps, step_name: str, summary: str) -> None:
        now = datetime.now(tz=UTC).isoformat()
        steps.append(PipelineStepRecord(
            step_name=step_name,
            status='ok',
            started_at=now,
            finished_at=now,
            duration_ms=0,
            summary=summary,
        ))

    def _run_step(self, steps, step_name, summary, func):
        started = datetime.now(tz=UTC)
        started_perf = perf_counter()
        status = 'ok'
        try:
            payload = func()
        except Exception as exc:
            status = 'error'
            finished = datetime.now(tz=UTC)
            steps.append(PipelineStepRecord(
                step_name=step_name,
                status=status,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                duration_ms=int((perf_counter() - started_perf) * 1000),
                summary=summary,
                error_type='step_execution_error',
                error_message=str(exc),
                exception_class=exc.__class__.__name__,
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
