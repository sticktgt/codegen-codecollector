from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING

from codecollector.domain.models import (
    ChangeRequest,
    GenerationReplay,
    MergePlan,
    PatchArtifact,
    PipelineRunResult,
    PipelineStepRecord,
    SearchCandidate,
)
from codecollector.logger import get_logger
from codecollector.orchestration.run_artifacts import RunArtifactsManager

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

    def _prepare_merge_plan(self, apply_result: ApplyResult) -> MergePlan:
        ready_for_manual_merge_review = apply_result.validation.is_valid
        summary_lines = [
            'Режим merge: dry-run, без копирования изменений в master.',
            'Структурная валидация прошла, но итоговое решение о merge принимает человек.' if ready_for_manual_merge_review else 'Структурная валидация завершилась с ошибками, merge не рекомендуется без дополнительной проверки.',
            f'Измененные файлы: {", ".join(apply_result.impact.changed_files) or "—"}',
            f'Символы в измененных файлах: {", ".join(apply_result.impact.symbols_in_changed_files) or "—"}',
            f'Связанные требования: {", ".join(apply_result.impact.linked_requirements) or "—"}',
            f'Рекомендуемые тесты: {", ".join(apply_result.impact.recommended_tests) or "—"}',
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
