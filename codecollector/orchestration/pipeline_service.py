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
    PipelineExecutionSummary,
    VerificationBlock,
    VerificationIssue,
    VerificationReport,
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

from codecollector.validation.semantic_checks import (
    build_verification_report,
    validate_code_artifact_static_semantics,
    validate_generated_test_relevance,
    validate_generated_test_static_semantics,
    validate_patch_static_semantics,
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
        verification_report: VerificationReport,
        requested_operation: str = 'replace_symbol',
    ) -> PipelineRunResult:
        self.project_services.reset_embedding_usage()
        run_id, run_label, run_dir = self.artifacts_manager.create_run_dir('pipeline')
        steps: list[PipelineStepRecord] = []

        build_report: dict[str, Any] | None = None
        context_pack: ContextPack | None = None
        repair_generation: ExternalGenerationCall | None = None
        apply_result: ApplyResult | None = None
        verification_result: VerificationReport | None = None
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
                raise RuntimeError(
                    repair_result_payload.get('message')
                    or 'codegenerator repair did not return code_artifact'
                )

            self._run_step(
                steps,
                'external_repair_static_semantics',
                'Проверить code artifact после external_repair статическими правилами',
                lambda: self._ensure_code_artifact_static_semantics(
                    result_payload=repair_result_payload,
                    step_name='external_repair',
                    expected_operation=requested_operation,
                    expected_target_qualname=selected_target,
                ),
            )

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

            if caught_exc is not None:
                execution_summary = getattr(result, 'execution_summary', None)
                if execution_summary is not None:
                    execution_summary.status = 'failed'
                    execution_summary.merge_ready = False
                    execution_summary.verification_passed = False
                    execution_summary.merge_mode = None

                if result.merge_plan is not None:
                    result.merge_plan = None

            try:
                self.artifacts_manager.write_bundle(run_dir, f'pipeline_run_{run_label}.json', result)
            except Exception:
                LOGGER.exception('Failed to write repair pipeline run bundle in finally: %s', run_dir)

            if caught_exc is not None:
                failed_step = next((step for step in reversed(steps) if step.status == 'error'), None)
                if failed_step is not None:
                    message = f'{failed_step.step_name} failed: {caught_exc}'
                else:
                    message = str(caught_exc)

                LOGGER.exception(
                    'pipeline repair failed run_id=%s selected_target=%s failed_step=%s error_class=%s error=%s',
                    run_id,
                    selected_target,
                    failed_step.step_name if failed_step is not None else None,
                    type(caught_exc).__name__,
                    str(caught_exc),
                )

                raise PipelineRunFailed(message, result) from caught_exc

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
        verification_report: VerificationReport | None = None
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
                raise RuntimeError(
                    external_code_result_payload.get('message')
                    or 'codegenerator generate did not return code_artifact'
                )

            ensure_expected_operation(external_code_result_payload, requested_operation)

            final_payload = external_code_result_payload
            generated_tests: list[dict[str, str]] = []

            try:
                self._run_step(
                    steps,
                    'external_generate_static_semantics',
                    'Проверить code artifact после external_generate статическими правилами',
                    lambda: self._ensure_code_artifact_static_semantics(
                        result_payload=external_code_result_payload,
                        step_name='external_generate',
                        expected_operation=requested_operation,
                        expected_target_qualname=selected_target,
                    ),
                )

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
                repair_input_report = self._build_apply_failure_report(exc)
                repair_input_context = self._normalize_repair_context(repair_input_report)
                if (
                    not self.project_services.config.codegenerator_repair_enabled
                    or not repair_input_context.get('failure_summary', {}).get('repairable', False)
                ):
                    raise

                repair_step_name = (
                    'external_repair_after_external_generate_static_semantics'
                    if any(step.step_name == 'external_generate_static_semantics' and step.status == 'error' for step in steps)
                    else 'external_repair'
                )
                repair_step_summary = (
                    'Вызвать внешний codegenerator repair после ошибки external_generate_static_semantics'
                    if repair_step_name == 'external_repair_after_external_generate_static_semantics'
                    else 'Вызвать внешний codegenerator repair после ошибки apply'
                )

                repair_generation, repair_result_payload = self._run_step(
                    steps,
                    repair_step_name,
                    repair_step_summary,
                    lambda: self._external_repair(
                        run_dir,
                        change_request,
                        selected_target,
                        context_pack,
                        final_payload,
                        repair_input_report,
                        requested_operation=requested_operation,
                    ),
                )
                if not self._has_code_artifact(repair_result_payload):
                    raise RuntimeError(
                        repair_result_payload.get('message')
                        or 'codegenerator repair did not return code_artifact'
                    )

                self._run_step(
                    steps,
                    'external_repair_static_semantics',
                    'Проверить code artifact после external_repair статическими правилами',
                    lambda: self._ensure_code_artifact_static_semantics(
                        result_payload=repair_result_payload,
                        step_name='external_repair',
                        expected_operation=requested_operation,
                        expected_target_qualname=selected_target,
                    ),
                )

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

            target_file = str(context_pack.target.file_path or "").strip()
            original_target_file_path = self.project_services.project_root / target_file
            patched_target_file_path = apply_result.workspace_path / target_file

            patch_static_block = self._run_step(
                steps,
                'patch_static_semantics',
                'Проверить patch статическими семантическими правилами',
                lambda: validate_patch_static_semantics(
                    requested_operation=requested_operation,
                    change_request=change_request,
                    target_qualname=selected_target,
                    original_file_text=original_target_file_path.read_text(encoding='utf-8'),
                    patched_file_text=patched_target_file_path.read_text(encoding='utf-8'),
                    changed_files=list(apply_result.impact.changed_files),
                    target_file=target_file,
                ),
            )

            generated_test_blocks: list[VerificationBlock] = []

            if not patch_static_block.ok:
                if self.project_services.config.codegenerator_repair_enabled:
                    patch_failure_report = self._build_patch_static_failure_report(
                        patch_static_block,
                    )
                    LOGGER.warning(
                        "patch static semantics failed; trying repair issue_codes=%s",
                        [issue.code for issue in patch_static_block.issues],
                    )
                    repair_generation, repair_result_payload = self._run_step(
                        steps,
                        'external_repair_after_patch_static_semantics',
                        'Вызвать внешний codegenerator repair после ошибки patch_static_semantics',
                        lambda: self._external_repair(
                            run_dir,
                            change_request,
                            selected_target,
                            context_pack,
                            final_payload,
                            patch_failure_report,
                            requested_operation=requested_operation,
                        ),
                    )
                    if not self._has_code_artifact(repair_result_payload):
                        raise RuntimeError(
                            repair_result_payload.get('message')
                            or 'codegenerator repair did not return code_artifact'
                        )

                    self._run_step(
                        steps,
                        'external_repair_after_patch_static_semantics_static_semantics',
                        'Проверить code artifact после repair статическими правилами',
                        lambda: self._ensure_code_artifact_static_semantics(
                            result_payload=repair_result_payload,
                            step_name='external_repair',
                            expected_operation=requested_operation,
                            expected_target_qualname=selected_target,
                        ),
                    )

                    final_payload = repair_result_payload
                    apply_result = self._replace_active_apply_result(
                        apply_result,
                        self._run_step(
                            steps,
                            'apply_repair_staging_after_patch_static_semantics',
                            'Применить исправленный артефакт в staging workspace после patch_static_semantics',
                            lambda: self.project_services.apply(
                                patch_artifact_from_result(final_payload, selected_target),
                                generated_tests=[],
                            ),
                        ),
                    )

                    patched_target_file_path = apply_result.workspace_path / target_file
                    patch_static_block = self._run_step(
                        steps,
                        'patch_static_semantics_after_repair',
                        'Повторно проверить patch статическими семантическими правилами после repair',
                        lambda: validate_patch_static_semantics(
                            requested_operation=requested_operation,
                            change_request=change_request,
                            target_qualname=selected_target,
                            original_file_text=original_target_file_path.read_text(encoding='utf-8'),
                            patched_file_text=patched_target_file_path.read_text(encoding='utf-8'),
                            changed_files=list(apply_result.impact.changed_files),
                            target_file=target_file,
                        ),
                    )

                if not patch_static_block.ok:
                    runtime_blocks = self._run_step(
                        steps,
                        'verification',
                        'Запустить runtime-проверки проекта после apply',
                        lambda: self._run_runtime_verification_blocks(
                            apply_result,
                            generated_test_apply,
                        ),
                    )
                    verification_report = build_verification_report(
                        blocks=[patch_static_block, *runtime_blocks],
                    )
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
                    generated_test_file_path = apply_result.workspace_path / generated_tests[0]['file_path']
                    generated_test_file_path.parent.mkdir(parents=True, exist_ok=True)
                    generated_test_file_path.write_text(generated_tests[0]['source_code'], encoding='utf-8')

                    generated_test_static_block = self._run_step(
                        steps,
                        'generated_test_static_semantics',
                        'Проверить generated test статическими правилами',
                        lambda: validate_generated_test_static_semantics(
                            project_root=apply_result.workspace_path,
                            test_file_path=generated_test_file_path,
                            target_qualname=selected_target,
                            requested_operation=requested_operation,
                            generated_symbol_names=list(apply_result.impact.symbols_in_changed_files),
                        ),
                    )

                    generated_test_relevance_block = self._run_step(
                        steps,
                        'generated_test_relevance',
                        'Проверить релевантность generated test изменению',
                        lambda: validate_generated_test_relevance(
                            test_source=generated_tests[0]['source_code'],
                            requested_operation=requested_operation,
                            target_qualname=selected_target,
                            generated_symbol_names=list(apply_result.impact.symbols_in_changed_files),
                        ),
                    )                    

                    generated_test_blocks = [
                        generated_test_static_block,
                        generated_test_relevance_block,
                    ]
                    LOGGER.info(
                        "generated test checks result static_ok=%s relevance_ok=%s test_files=%s",
                        generated_test_static_block.ok,
                        generated_test_relevance_block.ok,
                        [item['file_path'] for item in generated_tests],
                    )
                    if generated_test_static_block.ok and generated_test_relevance_block.ok:
                        generated_test_apply = {
                            'applied_tests': [item['file_path'] for item in generated_tests],
                            'count': len(generated_tests),
                            'skipped': False,
                        }
                        LOGGER.info(
                            "generated tests accepted for apply count=%s files=%s",
                            generated_test_apply['count'],
                            generated_test_apply['applied_tests'],
                        )
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
                        generated_test_apply = {
                            'applied_tests': [],
                            'count': 0,
                            'skipped': True,
                            'reason': 'generated_test_semantic_checks_failed',
                            'candidate_test_files': [item['file_path'] for item in generated_tests],
                        }
                        LOGGER.warning(
                            "generated tests rejected by semantic checks candidate_files=%s static_issues=%s relevance_issues=%s",
                            generated_test_apply['candidate_test_files'],
                            [issue.code for issue in generated_test_static_block.issues],
                            [issue.code for issue in generated_test_relevance_block.issues],
                        )
                else:
                    generated_test_apply = {
                        'applied_tests': [],
                        'count': 0,
                        'skipped': True,
                        'reason': 'no_generated_tests',
                    }
            else:
                warning_message = 'Генерация теста пропущена: для target уже есть related_tests или recommended_tests.'
                warnings.append(warning_message)
                LOGGER.info(warning_message)

            runtime_blocks = self._run_step(
                steps,
                'verification',
                'Запустить runtime-проверки проекта после apply',
                lambda: self._run_runtime_verification_blocks(
                    apply_result,
                   generated_test_apply,
                ),
            )

            verification_report = build_verification_report(
                blocks=[patch_static_block, *generated_test_blocks, *runtime_blocks],
            )

            if (not verification_report.passed) and self.project_services.config.codegenerator_repair_enabled and repair_generation is None:
                if verification_report.verdict == 'generated_test_verification_failed':
                    warning_message = 'Repair пропущен: упал только сгенерированный тест, основной код не отправляется в repair.'
                    warnings.append(warning_message)
                    LOGGER.warning(warning_message)
                elif verification_report.verdict == 'verification_failed':
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
                        raise RuntimeError(
                            repair_result_payload.get('message')
                            or 'codegenerator repair did not return code_artifact'
                        )

                    self._run_step(
                        steps,
                        'external_repair_static_semantics_after_verification',
                        'Проверить code artifact после repair статическими правилами',
                        lambda: self._ensure_code_artifact_static_semantics(
                            result_payload=repair_result_payload,
                            step_name='external_repair',
                            expected_operation=requested_operation,
                            expected_target_qualname=selected_target,
                        ),
                    )

                    final_payload = repair_result_payload
                    apply_result = self._replace_active_apply_result(
                        apply_result,
                        self._run_step(
                            steps,
                            'apply_repair_staging',
                            'Применить исправленный артефакт в staging workspace',
                            lambda: self.project_services.apply(
                                patch_artifact_from_result(final_payload, selected_target),
                                generated_tests=generated_tests if generated_test_apply and generated_test_apply.get('applied_tests') else [],
                            ),
                        ),
                    )
                    runtime_blocks = self._run_step(
                        steps,
                        'verification_after_repair',
                        'Повторно запустить runtime-проверки проекта после repair',
                        lambda: self._run_runtime_verification_blocks(
                            apply_result,
                            generated_test_apply,
                        ),
                    )

                    verification_report = build_verification_report(
                        blocks=[patch_static_block, *generated_test_blocks, *runtime_blocks],
                    )

            verification_report = self._reclassify_generated_test_failure_only(
                verification_report,
                generated_test_apply,
            )

            if (
                apply_result is not None
                and self._is_generated_test_only_verdict(verification_report)
            ):
                notes = list(apply_result.impact.notes or [])
                special_note = (
                    'Основное изменение прошло проверки, но упал только сгенерированный тест; требуется ручная оценка качества generated test.'
                )
                if special_note not in notes:
                    notes.append(special_note)
                    apply_result.impact.notes = notes            

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

            if caught_exc is not None:
                execution_summary = getattr(result, 'execution_summary', None)
                if execution_summary is not None:
                    execution_summary.status = 'failed'
                    execution_summary.merge_ready = False
                    execution_summary.verification_passed = False
                    execution_summary.merge_mode = None

                if result.merge_plan is not None:
                    result.merge_plan = None

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

            LOGGER.exception(
                'pipeline generate failed run_id=%s selected_target=%s failed_step=%s error_class=%s error=%s',
                run_id,
                selected_target,
                failed_step.step_name if failed_step is not None else None,
                type(caught_exc).__name__,
                str(caught_exc),
            )

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
    
    def _ensure_code_artifact_static_semantics(
        self,
        *,
        result_payload: dict[str, Any],
        step_name: str,
        expected_operation: str | None = None,
        expected_target_qualname: str | None = None,
    ) -> VerificationBlock:
        block = validate_code_artifact_static_semantics(
            result_payload=result_payload,
            step_name=step_name,
            expected_operation=expected_operation,
            expected_target_qualname=expected_target_qualname,
        )
        if not block.ok:
            first_issue = block.issues[0] if block.issues else None
            details = dict(block.details or {})
            syntax_error = dict(details.get("syntax_error") or {})

            error_message = (
                (first_issue.message if first_issue and first_issue.message else None)
                or syntax_error.get("message")
                or f"{step_name} returned invalid code_artifact"
            )
            raise SyntaxError(error_message)
        return block 

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

    def _generated_test_failure_only(
        self,
        verification_report: VerificationReport | None,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> bool:
        if verification_report is None:
            return False
        return (
            verification_report.verdict == 'generated_test_verification_failed'
            or self._is_generated_test_only_runtime_failure(
                verification_report,
                generated_test_apply,
            )
        )
    
    def _is_generated_test_only_verdict(
        self,
        verification_report: VerificationReport | None,
    ) -> bool:
        if verification_report is None:
            return False
        return (
            verification_report.verdict == 'generated_test_verification_failed'
            or bool((verification_report.summary or {}).get('generated_test_runtime_only'))
        )    

    def _is_generated_test_only_runtime_failure(
        self,
        verification_report: VerificationReport | None,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> bool:
        if verification_report is None:
            return False

        generated_paths = {
            str(item or "").strip().replace("\\", "/").lower()
            for item in list((generated_test_apply or {}).get('applied_tests') or [])
            if str(item or "").strip()
        }
        if not generated_paths:
            return False

        failed_blocks = [block for block in verification_report.blocks if not block.ok]
        if len(failed_blocks) != 1:
            return False

        failed_block = failed_blocks[0]
        if failed_block.name != 'runtime_pytest_recommended':
            return False

        details = dict(failed_block.details or {})
        output_parts = [
            str(details.get('stdout') or '').strip(),
            str(details.get('stderr') or '').strip(),
        ]
        output = '\n'.join(part for part in output_parts if part).replace("\\", "/").lower()
        if not output:
            return False

        command_paths = {
            str(part).strip().replace("\\", "/").lower()
            for part in list(details.get('command') or [])
            if str(part).strip().endswith('.py')
        }
        non_generated_paths = {
            path for path in command_paths
            if path not in generated_paths
        }

        generated_mentioned = any(
            path in output or Path(path).name.lower() in output
            for path in generated_paths
        )
        if not generated_mentioned:
            return False

        non_generated_mentioned = any(
            path in output or Path(path).name.lower() in output
            for path in non_generated_paths
        )
        if non_generated_mentioned:
            return False

        return True

    def _reclassify_generated_test_failure_only(
        self,
        verification_report: VerificationReport | None,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> VerificationReport | None:
        if verification_report is None:
            return None

        if not self._is_generated_test_only_runtime_failure(
            verification_report,
            generated_test_apply,
        ):
            return verification_report

        summary = dict(verification_report.summary or {})
        summary['production_failed'] = False
        summary['generated_test_failed'] = True
        summary['generated_test_runtime_only'] = True

        return VerificationReport(
            verdict='generated_test_verification_failed',
            passed=False,
            blocks=list(verification_report.blocks),
            summary=summary,
        )

    def _resolve_verification_status(
        self,
        verification_report: VerificationReport | None,
    ) -> str | None:
        if verification_report is None:
            return None
        return verification_report.verdict

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
        verification_report: VerificationReport | None = None,
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
                (
                    external_code_generation.request_summary.get('target', {}).get('operation')
                    if isinstance(external_code_generation.request_summary, dict)
                    else None
                )
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
        verification_report: VerificationReport | dict[str, Any],
        requested_operation: str = 'replace_symbol',
    ) -> tuple[ExternalGenerationCall, dict[str, Any]]:
        repair_context = self._normalize_repair_context(verification_report)
        LOGGER.info(
            'prepare repair request target=%s requested_operation=%s failed_blocks=%s',
            selected_target,
            requested_operation,
            [
                item.get('name')
                for item in (repair_context.get('failure_summary', {}).get('failed_blocks') or [])
            ],
        )
        request_payload = build_repair_request(
            change_request,
            selected_target,
            previous_result_payload,
            context_pack,
            repair_context.get('failure_summary', {}),
            self.project_services.config,
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
        verification_report: VerificationReport | None,
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

        verification_passed = None if verification_report is None else bool(verification_report.passed)
        verification_status = self._resolve_verification_status(verification_report)
        has_generated_test = bool((generated_test_apply or {}).get('count', 0))
        generated_test_files = list((generated_test_apply or {}).get('applied_tests') or [])
        repair_used = repair_generation is not None
        merge_mode = None if merge_plan is None else merge_plan.mode
        merge_ready = None if merge_plan is None else bool(merge_plan.ready_for_manual_merge_review)

        if verification_status is not None:
            status = verification_status
        elif merge_plan is not None and merge_plan.ready_for_manual_merge_review:
            status = 'ready_for_merge_review'
        elif apply_result is not None:
            status = 'applied'
        elif external_code_generation is not None:
            code_result_status = None
            if isinstance(code_result_summary, dict):
                code_result_status = code_result_summary.get('status')
            status = 'generated' if code_result_status == 'ok' else 'incomplete'
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
            merge_ready=(status == 'ready_for_merge_review'),
            linked_requirements=linked_requirements,
            recommended_tests=recommended_tests,
            recommended_test_commands=recommended_test_commands,
            code_generation_usage=(usage_summary or {}).get('code_generation'),
            test_generation_usage=(usage_summary or {}).get('test_generation'),
            repair_generation_usage=(usage_summary or {}).get('repair_generation'),
            embedding_usage=(usage_summary or {}).get('embedding'),
        )

    def _collect_verification_test_targets(
        self,
        apply_result: ApplyResult,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        for item in list(apply_result.impact.recommended_tests or []):
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                ordered.append(value)

        for item in list((generated_test_apply or {}).get('applied_tests') or []):
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                ordered.append(value)

        LOGGER.info(
            "verification targets resolved recommended=%s generated_applied=%s final=%s",
            list(apply_result.impact.recommended_tests or []),
            list((generated_test_apply or {}).get('applied_tests') or []),
            ordered,
        )
        return ordered
    
    def _run_runtime_verification_blocks(
        self,
        apply_result: ApplyResult,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> list[VerificationBlock]:
        verification_targets = self._collect_verification_test_targets(
            apply_result,
            generated_test_apply,
        )
        return self.project_services.validation_service.run_runtime_verification_blocks(
            apply_result.workspace_path,
            verification_targets,
            run_ruff=self.project_services.config.verification_run_ruff,
            run_recommended_tests=self.project_services.config.verification_run_recommended_tests,
            run_full_project_tests=self.project_services.config.verification_run_full_project_tests,
        )    

    def _run_verification(
        self,
        apply_result: ApplyResult,
        generated_test_apply: dict[str, Any] | None = None,
    ) -> VerificationReport:
        runtime_blocks = self._run_runtime_verification_blocks(
            apply_result,
            generated_test_apply,
        )
        return build_verification_report(blocks=runtime_blocks)
    
    def _build_apply_failure_report(self, exc: Exception) -> VerificationReport:
        return build_verification_report(
            blocks=[
                VerificationBlock(
                    name='apply_generated_artifact',
                    ok=False,
                    severity='error',
                    issues=[
                        VerificationIssue(
                            code=type(exc).__name__,
                            message=str(exc),
                            severity='error',
                        )
                    ],
                    details={
                        'error_type': type(exc).__name__,
                        'message': str(exc),
                    },
                )
            ]
        )
    
    def _build_patch_static_failure_report(
        self,
        patch_static_block: VerificationBlock,
    ) -> VerificationReport:
        return build_verification_report(
            blocks=[patch_static_block],
        )

    def _normalize_repair_context(
        self,
        verification_report: VerificationReport | dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(verification_report, dict):
            failure_summary = dict(verification_report.get('failure_summary') or {})
            if 'repairable' not in failure_summary:
                failure_summary['repairable'] = bool(
                    failure_summary.get('failed_blocks')
                    or failure_summary.get('failed_checks')
                )
            normalized = dict(verification_report)
            normalized['failure_summary'] = failure_summary
            return normalized

        failed_blocks: list[dict[str, Any]] = []
        for block in verification_report.blocks:
            if block.ok:
                continue
            failed_blocks.append(
                {
                    'name': block.name,
                    'severity': block.severity,
                    'issues': [
                        {
                            'code': issue.code,
                            'message': issue.message,
                            'severity': issue.severity,
                            'file_path': issue.file_path,
                            'symbol': issue.symbol,
                        }
                        for issue in block.issues
                    ],
                    'details': asdict(block).get('details', {}),
                }
            )

        return {
            'verdict': verification_report.verdict,
            'passed': verification_report.passed,
            'summary': dict(verification_report.summary or {}),
            'failure_summary': {
                'repairable': bool(failed_blocks),
                'failed_blocks': failed_blocks,
            },
        }    

    def _mark_noop_repair_if_needed(
        self,
        verification_report: VerificationReport,
        apply_result: ApplyResult,
        repair_generation: ExternalGenerationCall | None,
    ) -> VerificationReport:
        if repair_generation is None:
            return verification_report

        diff_text = apply_result.diff.unified_diff or ''
        if diff_text.strip():
            return verification_report

        extra_block = VerificationBlock(
            name='repair_intent',
            ok=False,
            severity='error',
            issues=[
                VerificationIssue(
                    code='repair_intent',
                    message='Repair produced no effective change relative to the original project code.',
                    severity='error',
                )
            ],
            details={},
        )
        return build_verification_report(
            blocks=[*verification_report.blocks, extra_block]
        )


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

    def _prepare_merge_plan(
        self,
        apply_result: ApplyResult,
        verification_report: VerificationReport | None = None,
    ) -> MergePlan:
        verification_ok = True if verification_report is None else bool(verification_report.passed)
        generated_test_only_failed = self._is_generated_test_only_verdict(verification_report)

        ready_for_manual_merge_review = (
            apply_result.validation.is_valid
            and (verification_ok or generated_test_only_failed)
        )

        status_line = (
            'Структурная валидация и тестовые проверки прошли, но итоговое решение о merge принимает человек.'
            if verification_ok
            else (
                'Основное изменение прошло проверки, но упал только сгенерированный тест; merge возможен после ручной оценки.'
                if generated_test_only_failed
                else 'Есть ошибки валидации или тестов, merge не рекомендуется без дополнительной проверки.'
            )
        )

        post_apply_line = (
            'Проверки после apply: only generated test failed'
            if generated_test_only_failed
            else f'Проверки после apply: {"passed" if verification_ok else "failed"}'
        )

        summary_lines = [
            'Режим merge: dry-run, без копирования изменений в master.',
            status_line,
            f'Измененные файлы: {", ".join(apply_result.impact.changed_files) or "—"}',
            f'Символы в измененных файлах: {", ".join(apply_result.impact.symbols_in_changed_files) or "—"}',
            f'Связанные требования: {", ".join(apply_result.impact.linked_requirements) or "—"}',
            f'Рекомендуемые тесты: {", ".join(apply_result.impact.recommended_tests) or "—"}',
            post_apply_line,
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
