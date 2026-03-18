from pathlib import Path

from codecollector.domain.models import ChangeRequest, PatchArtifact
from codecollector.orchestration.services import ProjectServices


REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT / 'demo_projects' / 'sample_python_app'
ARTIFACTS_ROOT = REPO_ROOT / 'demo_artifacts'
CHANGE_REQUESTS_ROOT = REPO_ROOT / 'demo_change_requests'


def test_index_and_search_returns_assignment_candidate_for_change_request_text() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    candidates = services.search('Изменить формирование текста уведомления о назначении тикета', limit=5)

    qualnames = [candidate.qualname for candidate in candidates]
    assert 'support_app.services.notification_service.build_assignment_message' in qualnames


def test_context_pack_contains_target_neighbors_and_recommended_tests() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    context_pack = services.context('support_app.services.ticket_service.TicketService.assign_ticket')

    assert context_pack.target.name == 'assign_ticket'
    assert any(neighbor.name == 'list_open_tickets' for neighbor in context_pack.neighbors)
    assert any(test.qualname == 'tests.test_ticket_service.test_assign_ticket_returns_message' for test in context_pack.related_tests)
    assert 'tests.test_ticket_service.test_assign_ticket_returns_message' in context_pack.recommended_tests


def test_context_pack_resolves_indirect_related_tests_for_notification_message() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    context_pack = services.context('support_app.services.notification_service.build_assignment_message')

    assert 'tests.test_ticket_service.test_assign_ticket_returns_message' in context_pack.recommended_tests


def test_apply_replacement_creates_valid_workspace() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    replacement_code = (ARTIFACTS_ROOT / 'build_assignment_message_v2.py').read_text(encoding='utf-8')
    result = services.apply(
        PatchArtifact(
            target_qualname='support_app.services.notification_service.build_assignment_message',
            replacement_code=replacement_code,
        )
    )

    assert result.validation.is_valid is True
    assert result.reindexed is True
    assert 'теперь назначен на' in result.diff.unified_diff
    assert 'support_app.api.controllers.TicketController.assign_ticket_endpoint' in result.impact.inbound_callers
    assert 'tests.test_ticket_service.test_assign_ticket_returns_message' in result.impact.related_tests
    assert 'pytest tests/test_ticket_service.py' in result.impact.recommended_test_commands


def test_apply_insert_after_symbol_adds_helper_function() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    artifact_code = (ARTIFACTS_ROOT / 'add_assignment_audit_line.py').read_text(encoding='utf-8')
    result = services.apply(
        PatchArtifact(
            target_qualname='support_app.services.notification_service.build_assignment_message',
            replacement_code=artifact_code,
            operation='insert_after_symbol',
        )
    )

    assert result.validation.is_valid is True
    assert 'build_assignment_audit_line' in result.diff.unified_diff
    changed_file = result.workspace_path / 'support_app/services/notification_service.py'
    content = changed_file.read_text(encoding='utf-8')
    assert 'def build_assignment_audit_line' in content


def test_apply_add_symbol_appends_method_to_class() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    artifact_code = (ARTIFACTS_ROOT / 'reopen_ticket_method.py').read_text(encoding='utf-8')
    result = services.apply(
        PatchArtifact(
            target_qualname='support_app.services.ticket_service.TicketService',
            replacement_code=artifact_code,
            operation='add_symbol',
        )
    )

    assert result.validation.is_valid is True
    assert result.reindexed is True
    assert 'reopen_ticket' in result.diff.unified_diff
    changed_file = result.workspace_path / 'support_app/services/ticket_service.py'
    content = changed_file.read_text(encoding='utf-8')
    assert 'def reopen_ticket' in content


def test_search_returns_russian_reasons_and_relevance_category() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    candidates = services.search('Изменить формирование текста уведомления о назначении тикета', limit=3)

    top = candidates[0]
    assert top.relevance_category in {'высокая', 'средняя'}
    assert 0.0 <= top.confidence <= 1.0
    assert any('совпадение' in reason or 'описание' in reason for reason in top.reasons)


def test_pipeline_replay_writes_run_bundle_and_merge_plan() -> None:
    services = ProjectServices(PROJECT_ROOT)
    change_request = ChangeRequest(
        title='Изменить формирование текста уведомления о назначении тикета',
        description='Сделать текст уведомления русскоязычным и использовать формулировку «теперь назначен на».',
        constraints=['Не менять внешний контракт API', 'Изменить только текст уведомления'],
        project=PROJECT_ROOT.name,
    )
    result = services.pipeline_replay(
        change_request=change_request,
        selected_target='support_app.services.notification_service.build_assignment_message',
        artifact_file=ARTIFACTS_ROOT / 'build_assignment_message_v2.py',
        operation='replace_symbol',
        limit=5,
    )

    assert result.merge_plan.mode == 'dry_run'
    assert result.merge_plan.ready_for_manual_merge_review is True
    assert result.run_dir.exists()
    bundle_files = list(result.run_dir.glob('pipeline_run_*.json'))
    assert bundle_files, 'Pipeline run bundle was not written'
    assert result.change_request.title == 'Изменить формирование текста уведомления о назначении тикета'


def test_demo_overlay_directory_contains_only_current_schema_files() -> None:
    overlay_dir = PROJECT_ROOT / '.codecollector'
    entries = sorted(path.name for path in overlay_dir.iterdir() if path.is_file())
    assert entries == ['index.db', 'knowledge.yaml']
