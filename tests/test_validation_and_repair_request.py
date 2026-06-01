from pathlib import Path

from codecollector.config import load_config
from codecollector.domain.models import ChangeRequest, ContextPack, RelatedSymbolContext, SymbolRecord
from codecollector.external_codegen.adapter import build_repair_request
from codecollector.validation.service import ValidationService


def test_dotted_test_qualname_resolves_to_pytest_node_id(tmp_path: Path) -> None:
    test_file = tmp_path / 'tests' / 'test_generated_generate_test_TicketController.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'class TestTicketController:\n'
        '    def test_agent_summary_v2_endpoint_returns_expected_dict(self):\n'
        '        assert True\n',
        encoding='utf-8',
    )

    service = ValidationService()
    paths = service._qualnames_to_test_paths(
        tmp_path,
        [
            'tests.test_generated_generate_test_TicketController.'
            'TestTicketController.test_agent_summary_v2_endpoint_returns_expected_dict'
        ],
    )

    assert paths == [
        'tests/test_generated_generate_test_TicketController.py::'
        'TestTicketController::test_agent_summary_v2_endpoint_returns_expected_dict'
    ]


def test_dotted_test_qualname_falls_back_to_file_when_node_missing(tmp_path: Path) -> None:
    test_file = tmp_path / 'tests' / 'test_sample.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text('def test_existing():\n    assert True\n', encoding='utf-8')

    service = ValidationService()
    paths, details = service._resolve_test_targets(
        tmp_path,
        ['tests.test_sample.TestMissing.test_missing'],
    )

    assert paths == ['tests/test_sample.py']
    assert details[0]['status'] == 'fallback_file'
    assert details[0]['unresolved_node_suffix'] == 'TestMissing::test_missing'



def test_resolved_pytest_node_suppresses_duplicate_file_target(tmp_path: Path) -> None:
    test_file = tmp_path / 'tests' / 'test_generated_generate_test_TicketController.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'class TestTicketController:\n'
        '    def test_ticket_stats_endpoint_returns_correct_stats(self):\n'
        '        assert True\n',
        encoding='utf-8',
    )

    service = ValidationService()
    paths, details = service._resolve_test_targets(
        tmp_path,
        [
            'tests.test_generated_generate_test_TicketController.'
            'TestTicketController.test_ticket_stats_endpoint_returns_correct_stats',
            'tests/test_generated_generate_test_TicketController.py',
        ],
    )

    assert paths == [
        'tests/test_generated_generate_test_TicketController.py::'
        'TestTicketController::test_ticket_stats_endpoint_returns_correct_stats'
    ]
    assert [item['status'] for item in details] == ['pytest_node', 'file']


def _symbol(
    *,
    qualname: str,
    name: str,
    kind: str,
    source: str,
    file_path: str = 'support_app/api/controllers.py',
    parent: str | None = None,
) -> SymbolRecord:
    return SymbolRecord(
        file_path=file_path,
        module_name='.'.join(file_path[:-3].split('/')),
        name=name,
        qualname=qualname,
        kind=kind,  # type: ignore[arg-type]
        parent_qualname=parent,
        start_line=1,
        end_line=max(1, len(source.splitlines())),
        source_code=source,
    )


def test_repair_request_reuses_allowed_surface_and_includes_full_file_source() -> None:
    config = load_config()
    module_source = (
        'from support_app.services.report_service import build_agent_summary\n'
        'from support_app.services.ticket_service import TicketService\n\n'
        'class TicketController:\n'
        '    def __init__(self, service: TicketService) -> None:\n'
        '        self.service = service\n\n'
        '    def agent_summary_endpoint(self, agent_name: str) -> dict:\n'
        '        tickets = self.service.repository.list_by_agent(agent_name)\n'
        '        summary = build_agent_summary(agent_name, tickets)\n'
        '        return summary.__dict__\n'
    )
    target_source = module_source.split('\n\n', 2)[-1]
    target = _symbol(
        qualname='support_app.api.controllers.TicketController',
        name='TicketController',
        kind='class',
        source=target_source,
        parent='support_app.api.controllers',
    )
    module = _symbol(
        qualname='support_app.api.controllers',
        name='controllers',
        kind='module',
        source=module_source,
        parent=None,
    )
    related = [
        RelatedSymbolContext(
            qualname='support_app.storage.ticket_repository.TicketRepository.list_by_agent',
            file_path='support_app/storage/ticket_repository.py',
            module_name='support_app.storage.ticket_repository',
            name='list_by_agent',
            kind='method',
            parent_qualname='support_app.storage.ticket_repository.TicketRepository',
            relation_kind='calls',
            relation_direction='outbound',
            relation_source='index',
            relation_confidence='high',
            role='called_by_class_member',
            signature='def list_by_agent(self, agent_name: str) -> list[Ticket]:',
        ),
    ]
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=related,
    )
    previous_allowed_surface = {
        'dependencies': [
            {
                'access_path': 'self.service.repository',
                'type_name': 'TicketRepository',
                'allowed_methods': [{'name': 'list_by_agent'}],
            }
        ],
        'free_functions': [{'name': 'build_agent_summary'}],
    }

    request = build_repair_request(
        ChangeRequest(
            title='Добавь API-функцию',
            description='Используй существующую сервисную функцию.',
            project='demo',
        ),
        'support_app.api.controllers.TicketController',
        {
            'request_id': 'generate-TicketController',
            'code_artifact': {
                'code': 'def agent_summary_endpoint(self, agent_name: str) -> dict:\n    return {}',
            },
        },
        context_pack,
        {'failure_summary': {'stage': 'verification', 'failed_blocks': []}},
        config,
        requested_operation='insert_after_symbol',
        insert_scope='class_body',
        previous_generation_request={'project_context': {'allowed_api_surface': previous_allowed_surface}},
    )

    assert request['project_context']['allowed_api_surface'] == previous_allowed_surface
    assert request['options']['allowed_api_surface_source'] == 'previous_generation_request'
    assert request['options']['allowed_api_surface_dependencies_count'] == 1
    assert 'class TicketController' in request['project_context']['full_file_source']
    assert request['project_context']['full_file_truncated'] is False
    assert request['target']['insert_scope'] == 'class_body'
    assert request['target']['parent_qualname'] == 'support_app.api.controllers.TicketController'

from codecollector.external_codegen.adapter import build_generation_request
from codecollector.validation.semantic_checks import validate_generated_test_relevance, validate_generated_test_static_semantics, validate_patch_static_semantics


def test_generation_request_infers_contract_attribute_requirements(tmp_path: Path) -> None:
    project_root = tmp_path
    target_file = project_root / 'support_app' / 'api' / 'controllers.py'
    target_file.parent.mkdir(parents=True)
    target_source = (
        'from support_app.services.report_service import build_agent_summary\n'
        'from support_app.services.ticket_service import TicketService\n\n'
        'class TicketController:\n'
        '    def __init__(self, service: TicketService) -> None:\n'
        '        self.service = service\n'
    )
    target_file.write_text(target_source, encoding='utf-8')

    target = _symbol(
        qualname='support_app.api.controllers.TicketController',
        name='TicketController',
        kind='class',
        source=target_source,
        parent='support_app.api.controllers',
    )
    module = _symbol(
        qualname='support_app.api.controllers',
        name='controllers',
        kind='module',
        source=target_source,
        parent=None,
    )
    ticket_class_source = (
        'class Ticket:\n'
        '    ticket_id: str\n'
        '    title: str\n'
        '    description: str\n'
        "    priority: str = 'medium'\n"
        "    status: str = 'open'\n"
        '    assigned_to: str | None = None\n'
    )
    report_source = (
        'def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:\n'
        "    assigned = [ticket for ticket in tickets if ticket.assigned_to == agent_name and ticket.status == 'open']\n"
        "    urgent = [ticket for ticket in assigned if ticket.priority in {'high', 'urgent'}]\n"
        '    return AgentSummary(agent_name=agent_name, open_tickets=len(assigned), urgent_tickets=len(urgent))\n'
    )
    related = [
        RelatedSymbolContext(
            qualname='support_app.domain.models.Ticket',
            file_path='support_app/domain/models.py',
            module_name='support_app.domain.models',
            name='Ticket',
            kind='class',
            parent_qualname='support_app.domain.models',
            relation_kind='calls',
            relation_direction='outbound',
            relation_source='index',
            relation_confidence='high',
            role='called_by_class_member',
            signature='class Ticket:',
            source_code=ticket_class_source,
        ),
        RelatedSymbolContext(
            qualname='support_app.services.report_service.build_agent_summary',
            file_path='support_app/services/report_service.py',
            module_name='support_app.services.report_service',
            name='build_agent_summary',
            kind='function',
            parent_qualname='support_app.services.report_service',
            relation_kind='calls',
            relation_direction='outbound',
            relation_source='index',
            relation_confidence='high',
            role='called_by_class_member',
            signature='def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:',
            source_code=report_source,
        ),
    ]
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=related,
    )

    request = build_generation_request(
        project_root,
        ChangeRequest(title='Добавь endpoint', description='Используй существующую сервисную функцию build_agent_summary.', project='demo'),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate_test',
        operation='insert_after_symbol',
        insert_scope='class_body',
    )

    required_contracts = request['project_context']['required_contracts']
    assert required_contracts == [
        {
            'qualname': 'support_app.services.report_service.build_agent_summary',
            'name': 'build_agent_summary',
            'signature': 'def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:',
            'reason': 'user_requested_existing_contract_reuse',
            'source': 'allowed_api_surface.free_functions',
            'origin_qualname': '',
        }
    ]

    requirements = request['project_context']['contract_attribute_requirements']
    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement['contract_qualname'] == 'support_app.services.report_service.build_agent_summary'
    assert requirement['parameter'] == 'tickets'
    assert requirement['item_type'] == 'Ticket'
    assert requirement['required_fields'] == ['assigned_to', 'priority', 'status']
    assert 'assigned_to' in requirement['constructor_fields']


def test_generated_test_static_semantics_rejects_unknown_project_constructor_keyword(tmp_path: Path) -> None:
    model_file = tmp_path / 'support_app' / 'domain' / 'models.py'
    model_file.parent.mkdir(parents=True)
    model_file.write_text(
        'class Ticket:\n'
        '    ticket_id: str\n'
        '    title: str\n'
        '    description: str\n'
        "    priority: str = 'medium'\n"
        "    status: str = 'open'\n"
        '    assigned_to: str | None = None\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from support_app.domain.models import Ticket\n\n'
        'def test_ticket():\n'
        '    ticket = Ticket(ticket_id="T-1", title="T", description="D", agent_name="Alice")\n'
        '    assert ticket\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='support_app.api.controllers.TicketController.ticket_stats_endpoint',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['support_app.api.controllers.TicketController.ticket_stats_endpoint'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_uses_unknown_constructor_keyword' for issue in block.issues)
    details = block.details['constructor_keyword_check']
    assert details['checked_calls'][0]['unknown_keywords'] == ['agent_name']


def test_generated_test_static_semantics_rejects_missing_project_import_module_and_name(tmp_path: Path) -> None:
    note_search = tmp_path / 'note' / 'note_search.py'
    note_search.parent.mkdir(parents=True)
    note_search.write_text(
        'class SearchResult:\n'
        '    def __init__(self, note, preview, match_positions):\n'
        '        self.note = note\n'
        '        self.preview = preview\n'
        '        self.match_positions = match_positions\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.search_result import SearchResult\n'
        'from note import search_utils\n'
        'from note.note_search import MissingResult\n\n'
        'def test_imports():\n'
        '    assert SearchResult\n'
        '    assert search_utils\n'
        '    assert MissingResult\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.search_results_by_content',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.search_results_by_content'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_import_points_to_missing_module' for issue in block.issues)
    assert any(issue.code == 'generated_test_imports_missing_project_name' for issue in block.issues)
    checked = block.details['project_import_check']['checked_imports']
    assert any(item['module'] == 'note.search_result' and not item['module_exists'] for item in checked)
    assert any(item['module'] == 'note.note_search' and item['missing_names'] == ['MissingResult'] for item in checked)


def test_generated_test_static_semantics_rejects_unknown_project_method_and_new_usage(tmp_path: Path) -> None:
    storage_file = tmp_path / 'note' / 'note_storage.py'
    storage_file.parent.mkdir(parents=True)
    storage_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def search_results_by_content(self, query):\n'
        '        return []\n'
        '    def search_by_content(self, query):\n'
        '        return []\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage(tmp_path):\n'
        '    storage = NoteStorage.__new__(NoteStorage)\n'
        '    storage.save_note(object())\n'
        '    assert storage.search_results_by_content("x") == []\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.search_results_by_content',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.search_results_by_content'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_uses_project_class_new' for issue in block.issues)
    assert any(issue.code == 'generated_test_calls_unknown_project_method' for issue in block.issues)
    assert block.details['project_method_call_check']['variable_types']['storage'] == 'NoteStorage'


def test_patch_static_semantics_rejects_missing_required_contract_call() -> None:
    original = (
        'from support_app.services.report_service import build_agent_summary\n\n'
        'class TicketController:\n'
        '    def agent_summary_endpoint(self, agent_name: str) -> dict:\n'
        '        tickets = self.service.repository.list_by_agent(agent_name)\n'
        '        summary = build_agent_summary(agent_name, tickets)\n'
        '        return summary.__dict__\n'
    )
    patched = original + (
        '\n'
        '    def ticket_stats_endpoint(self, agent_name: str) -> dict:\n'
        '        tickets = self.service.repository.list_by_agent(agent_name)\n'
        "        total = len(tickets)\n"
        "        open_count = sum(1 for ticket in tickets if ticket.status == 'open')\n"
        "        return {'total': total, 'open': open_count}\n"
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Добавь API-функцию',
            description='Используй существующую сервисную функцию и не дублируй бизнес-логику.',
            project='demo',
        ),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[],
        required_contracts=[
            {
                'qualname': 'support_app.services.report_service.build_agent_summary',
                'name': 'build_agent_summary',
                'reason': 'user_requested_existing_contract_reuse',
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == 'required_contract_not_used' for issue in block.issues)
    details = block.details['required_contract_usage_check']
    assert details['missing_contracts'][0]['name'] == 'build_agent_summary'


def test_patch_static_semantics_accepts_required_contract_call() -> None:
    original = (
        'from support_app.services.report_service import build_agent_summary\n\n'
        'class TicketController:\n'
        '    def agent_summary_endpoint(self, agent_name: str) -> dict:\n'
        '        tickets = self.service.repository.list_by_agent(agent_name)\n'
        '        summary = build_agent_summary(agent_name, tickets)\n'
        '        return summary.__dict__\n'
    )
    patched = original + (
        '\n'
        '    def ticket_stats_endpoint(self, agent_name: str) -> dict:\n'
        '        tickets = self.service.repository.list_by_agent(agent_name)\n'
        '        summary = build_agent_summary(agent_name, tickets)\n'
        "        return {'open_tickets': summary.open_tickets}\n"
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавь API-функцию', description='Используй существующую сервисную функцию.', project='demo'),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[],
        required_contracts=[
            {
                'qualname': 'support_app.services.report_service.build_agent_summary',
                'name': 'build_agent_summary',
                'reason': 'user_requested_existing_contract_reuse',
            }
        ],
    )

    assert block.ok
    assert block.details['required_contract_usage_check']['missing_contracts'] == []


def _models_context_pack(tmp_path: Path) -> tuple[Path, ContextPack, SymbolRecord]:
    project_root = tmp_path
    models_file = project_root / 'support_app' / 'domain' / 'models.py'
    models_file.parent.mkdir(parents=True)
    module_source = (
        'from dataclasses import dataclass\n\n'
        '@dataclass(slots=True)\n'
        'class Ticket:\n'
        '    ticket_id: str\n\n'
        '@dataclass(slots=True)\n'
        'class AgentSummary:\n'
        '    agent_name: str\n'
        '    open_tickets: int\n'
        '    urgent_tickets: int\n'
    )
    models_file.write_text(module_source, encoding='utf-8')
    module = _symbol(
        qualname='support_app.domain.models',
        name='models',
        kind='module',
        source=module_source,
        file_path='support_app/domain/models.py',
        parent=None,
    )
    target = _symbol(
        qualname='support_app.domain.models.AgentSummary',
        name='AgentSummary',
        kind='class',
        source=(
            'class AgentSummary:\n'
            '    agent_name: str\n'
            '    open_tickets: int\n'
            '    urgent_tickets: int\n'
        ),
        file_path='support_app/domain/models.py',
        parent='support_app.domain.models',
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    return project_root, context_pack, target


def test_generation_request_normalizes_dataclass_class_insert_to_module_body(tmp_path: Path) -> None:
    project_root, context_pack, target = _models_context_pack(tmp_path)

    request = build_generation_request(
        project_root,
        ChangeRequest(
            title='Добавить Dataclass модели адреса',
            description='Добавить Dataclass модели адреса с минимальным количеством полей',
            constraints=['Добавляем только Dataclass описания модели'],
            project='demo',
        ),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='insert_after_symbol',
        insert_scope='class_body',
    )

    assert request['target']['insert_scope'] == 'module_body'
    assert request['target']['expected_new_symbol_kind'] == 'class'
    assert request['target']['parent_qualname'] == 'support_app.domain.models'


def test_repair_request_overrides_impossible_class_body_metadata_for_dataclass(tmp_path: Path) -> None:
    _project_root, context_pack, target = _models_context_pack(tmp_path)

    request = build_repair_request(
        ChangeRequest(
            title='Добавить Dataclass модели адреса',
            description='Добавить Dataclass модели адреса с минимальным количеством полей',
            constraints=['Добавляем только Dataclass описания модели'],
            project='demo',
        ),
        target.qualname,
        {
            'request_id': 'generate-AgentSummary',
            'code_artifact': {
                'code': '@dataclass(slots=True)\nclass Address:\n    city: str',
                'insert_scope': 'class_body',
                'expected_new_symbol_kind': 'method',
                'parent_qualname': target.qualname,
            },
        },
        context_pack,
        {'failure_summary': {'stage': 'apply', 'failed_blocks': []}},
        load_config(),
        requested_operation='insert_after_symbol',
        insert_scope='class_body',
        previous_generation_request=None,
    )

    assert request['target']['insert_scope'] == 'module_body'
    assert request['target']['expected_new_symbol_kind'] == 'class'
    assert request['target']['parent_qualname'] == 'support_app.domain.models'
    assert request['previous_artifact']['insert_scope'] == 'module_body'
    assert request['previous_artifact']['expected_new_symbol_kind'] == 'class'
    assert request['previous_artifact']['parent_qualname'] == 'support_app.domain.models'


def test_generation_request_includes_required_class_members_for_replace_class(tmp_path: Path) -> None:
    project_root = tmp_path
    source = (
        'class NoteStorage:\n'
        '    def __init__(self, notes_dir: str = "./notes"):\n'
        '        raise NotImplementedError\n\n'
        '    def save_note(self, note):\n'
        '        raise NotImplementedError\n\n'
        '    def load_note(self, path):\n'
        '        raise NotImplementedError\n\n'
        '    def _internal_helper(self):\n'
        '        raise NotImplementedError\n'
    )
    target = _symbol(
        qualname='note.note_storage.NoteStorage',
        name='NoteStorage',
        kind='class',
        source=source,
        file_path='note/note_storage.py',
        parent='note.note_storage',
    )
    storage_file = project_root / 'note' / 'note_storage.py'
    storage_file.parent.mkdir(parents=True)
    storage_file.write_text(source, encoding='utf-8')
    module = _symbol(
        qualname='note.note_storage',
        name='note_storage',
        kind='module',
        source=source,
        file_path='note/note_storage.py',
        parent=None,
    )
    neighbors = [module]
    for name in ['__init__', 'save_note', 'load_note', '_internal_helper']:
        neighbors.append(
            _symbol(
                qualname=f'note.note_storage.NoteStorage.{name}',
                name=name,
                kind='method',
                source=f'    def {name}(self):\n        raise NotImplementedError',
                file_path='note/note_storage.py',
                parent='note.note_storage.NoteStorage',
            )
        )
    context_pack = ContextPack(
        target=target,
        neighbors=neighbors,
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )

    request = build_generation_request(
        project_root,
        ChangeRequest(
            title='Реализовать хранение заметок',
            description='Хранилище должно сохранять и загружать заметки.',
            project='demo',
        ),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
    )

    members = request['project_context']['required_class_members']
    assert [item['name'] for item in members] == ['__init__', 'save_note', 'load_note']
    assert request['options']['required_class_members_count'] == 3


def test_patch_static_semantics_rejects_class_replacement_missing_public_member() -> None:
    original = (
        'class NoteStorage:\n'
        '    def __init__(self, notes_dir="./notes"):\n'
        '        raise NotImplementedError\n\n'
        '    def save_note(self, note):\n'
        '        raise NotImplementedError\n\n'
        '    def load_note(self, path):\n'
        '        raise NotImplementedError\n'
    )
    patched = (
        'class NoteStorage:\n'
        '    def __init__(self, notes_dir="./notes"):\n'
        '        self.notes_dir = notes_dir\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Реализовать хранение заметок',
            description='Хранилище должно сохранять и загружать заметки.',
            project='demo',
        ),
        target_qualname='note.note_storage.NoteStorage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
        required_class_members=[
            {'name': '__init__', 'kind': 'method', 'required': True},
            {'name': 'save_note', 'kind': 'method', 'required': True},
            {'name': 'load_note', 'kind': 'method', 'required': True},
        ],
    )

    assert not block.ok
    missing = block.details['required_class_members_check']['missing_members']
    assert [item['name'] for item in missing] == ['save_note', 'load_note']
    assert any(issue.code == 'class_replacement_missing_existing_member' for issue in block.issues)


def test_patch_static_semantics_allows_explicitly_optional_class_member() -> None:
    original = (
        'class NoteStorage:\n'
        '    def __init__(self):\n'
        '        pass\n\n'
        '    def list_notes(self):\n'
        '        pass\n'
    )
    patched = (
        'class NoteStorage:\n'
        '    def __init__(self):\n'
        '        pass\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Упростить хранилище',
            description='Метод list_notes больше не нужен.',
            project='demo',
        ),
        target_qualname='note.note_storage.NoteStorage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
        required_class_members=[
            {'name': '__init__', 'kind': 'method', 'required': True},
            {'name': 'list_notes', 'kind': 'method', 'required': False, 'exclusion_reason': 'user_requested_removal'},
        ],
    )

    assert block.ok
    assert block.details['required_class_members_check']['missing_members'] == []


def test_required_class_members_keeps_methods_when_removing_notimplementederror(tmp_path: Path) -> None:
    class_source = (
        'class NoteStorage:\n'
        '    def __init__(self, notes_dir: str = "./notes"):\n'
        '        raise NotImplementedError\n'
        '    def save_note(self, note):\n'
        '        raise NotImplementedError\n'
        '    def load_note(self, path):\n'
        '        raise NotImplementedError\n'
        '    def get_note_path(self, note):\n'
        '        raise NotImplementedError\n'
        '    def list_notes(self):\n'
        '        raise NotImplementedError\n'
    )
    target = _symbol(
        qualname='note.note_storage.NoteStorage',
        name='NoteStorage',
        kind='class',
        source=class_source,
        file_path='note/note_storage.py',
        parent='note.note_storage',
    )
    module = _symbol(
        qualname='note.note_storage',
        name='note_storage',
        kind='module',
        source=class_source,
        file_path='note/note_storage.py',
        parent=None,
    )
    neighbors = [module]
    for name in ['__init__', 'save_note', 'load_note', 'get_note_path', 'list_notes']:
        neighbors.append(
            _symbol(
                qualname=f'note.note_storage.NoteStorage.{name}',
                name=name,
                kind='method',
                source=f'def {name}(self):\n    raise NotImplementedError\n',
                file_path='note/note_storage.py',
                parent='note.note_storage.NoteStorage',
            )
        )
    target_path = tmp_path / 'note' / 'note_storage.py'
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(class_source, encoding='utf-8')

    context_pack = ContextPack(
        target=target,
        neighbors=neighbors,
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )

    request = build_generation_request(
        tmp_path,
        ChangeRequest(
            title='Реализовать NoteStorage',
            description='Убрать NotImplementedError из конструктора, save_note, load_note, get_note_path и list_notes.',
            project='demo',
        ),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
    )

    members = request['project_context']['required_class_members']
    required = {item['name'] for item in members if item.get('required') is True}
    assert required == {'__init__', 'save_note', 'load_note', 'get_note_path', 'list_notes'}


def test_generation_request_includes_related_model_surface_for_storage(tmp_path: Path) -> None:
    target = _symbol(
        qualname='note.note_storage.NoteStorage',
        name='NoteStorage',
        kind='class',
        source='class NoteStorage:\n    def save_note(self, note: NoteModel):\n        pass\n',
        file_path='note/note_storage.py',
        parent='note.note_storage',
    )
    module = _symbol(
        qualname='note.note_storage',
        name='note_storage',
        kind='module',
        source='from note.note_model import NoteModel\n',
        file_path='note/note_storage.py',
        parent=None,
    )
    note_model_source = (
        'class NoteModel:\n'
        '    def __init__(self, topic: str = "", content: str = "", created_at=None, updated_at=None):\n'
        '        self.topic = topic\n'
        '        self.content = content\n'
    )
    related = [
        RelatedSymbolContext(
            qualname='note.note_model.NoteModel',
            file_path='note/note_model.py',
            module_name='note.note_model',
            name='NoteModel',
            kind='class',
            parent_qualname='note.note_model',
            relation_kind='imports',
            relation_direction='outbound',
            relation_source='index',
            relation_confidence='medium',
            role='imported_contract',
            signature='class NoteModel:',
            source_code=note_model_source,
        )
    ]
    target_path = tmp_path / 'note' / 'note_storage.py'
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(module.source_code + '\n' + target.source_code, encoding='utf-8')

    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=related,
    )

    request = build_generation_request(
        tmp_path,
        ChangeRequest(title='Реализовать хранилище', description='Сохранять и загружать заметки.', project='demo'),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
    )

    surfaces = request['project_context']['model_surfaces']
    assert surfaces[0]['name'] == 'NoteModel'
    assert set(surfaces[0]['constructor_fields']) == {'topic', 'content', 'created_at', 'updated_at'}


def test_patch_static_semantics_rejects_unknown_model_fields_in_production_code() -> None:
    original = (
        'from note.note_model import NoteModel\n\n'
        'class NoteStorage:\n'
        '    def save_note(self, note: NoteModel) -> None:\n'
        '        raise NotImplementedError\n'
    )
    patched = (
        'from note.note_model import NoteModel\n\n'
        'class NoteStorage:\n'
        '    def save_note(self, note: NoteModel) -> None:\n'
        '        title = note.title\n'
        '    def load_note(self, path: str) -> NoteModel:\n'
        '        return NoteModel(title="A", content="B")\n'
    )
    model_surfaces = [
        {
            'name': 'NoteModel',
            'qualname': 'note.note_model.NoteModel',
            'fields': ['topic', 'content', 'created_at', 'updated_at'],
            'constructor_fields': ['topic', 'content', 'created_at', 'updated_at'],
        }
    ]

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(title='Реализовать хранилище', description='Сохранять заметки.', project='demo'),
        target_qualname='note.note_storage.NoteStorage',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
        related_symbols=[],
        required_class_members=[{'name': 'save_note', 'kind': 'method', 'required': True}],
        model_surfaces=model_surfaces,
    )

    assert not block.ok
    codes = {issue.code for issue in block.issues}
    assert 'unknown_model_attribute' in codes
    assert 'unknown_model_constructor_keyword' in codes



def test_patch_static_semantics_does_not_infer_result_model_from_free_text() -> None:
    original = (
        'class SearchResult:\n'
        '    def __init__(self, note, preview, match_positions):\n'
        '        self.note = note\n'
        '        self.preview = preview\n'
        '        self.match_positions = match_positions\n\n'
        'class NoteStorage:\n'
        '    def search_by_content(self, query):\n'
        '        return []\n'
    )
    patched = original + (
        '    def search_results_by_content(self, query: str) -> list[dict]:\n'
        '        results = []\n'
        '        for note in self.search_by_content(query):\n'
        '            results.append({"note_id": note.id, "preview": note.content})\n'
        '        return results\n'
    )
    model_surfaces = [
        {
            'name': 'SearchResult',
            'qualname': 'note.note_search.SearchResult',
            'fields': ['note', 'preview', 'match_positions'],
            'constructor_fields': ['note', 'preview', 'match_positions'],
        }
    ]

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Поиск с результатами для отображения',
            description='Метод должен возвращать список SearchResult.',
            project='demo',
            constraints=[
                'Создавать SearchResult только с видимыми аргументами note, preview и match_positions.',
                'Не использовать словари вместо SearchResult.',
            ],
        ),
        target_qualname='note.note_storage.NoteStorage.search_by_content',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        related_symbols=[],
        model_surfaces=model_surfaces,
    )

    codes = {issue.code for issue in block.issues}
    assert 'requested_result_model_not_constructed' not in codes
    assert 'request_forbids_dict_result' not in codes
    details = block.details['requested_result_model_usage_check']
    assert details['skipped'] is True
    assert details['skip_reason'] == 'structured_verification_contract_not_available'


def test_patch_static_semantics_accepts_requested_result_model_constructor() -> None:
    original = (
        'class SearchResult:\n'
        '    def __init__(self, note, preview, match_positions):\n'
        '        self.note = note\n'
        '        self.preview = preview\n'
        '        self.match_positions = match_positions\n\n'
        'class NoteStorage:\n'
        '    def search_by_content(self, query):\n'
        '        return []\n'
    )
    patched = original + (
        '    def search_results_by_content(self, query: str) -> list[SearchResult]:\n'
        '        results = []\n'
        '        for note in self.search_by_content(query):\n'
        '            results.append(SearchResult(note=note, preview=note.content, match_positions=[]))\n'
        '        return results\n'
    )
    model_surfaces = [
        {
            'name': 'SearchResult',
            'qualname': 'note.note_search.SearchResult',
            'fields': ['note', 'preview', 'match_positions'],
            'constructor_fields': ['note', 'preview', 'match_positions'],
        }
    ]

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Поиск с результатами для отображения',
            description='Метод должен возвращать список SearchResult.',
            project='demo',
            constraints=[
                'Создавать SearchResult только с видимыми аргументами note, preview и match_positions.',
                'Не использовать словари вместо SearchResult.',
            ],
        ),
        target_qualname='note.note_storage.NoteStorage.search_by_content',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        related_symbols=[],
        model_surfaces=model_surfaces,
    )

    codes = {issue.code for issue in block.issues}
    assert 'requested_result_model_not_constructed' not in codes
    assert 'request_forbids_dict_result' not in codes


def test_patch_static_semantics_rejects_unknown_annotation_names() -> None:
    original = (
        'from dataclasses import dataclass, field\n'
        'from datetime import datetime\n'
        'from typing import Optional\n\n'
        '@dataclass\n'
        'class Note:\n'
        '    subject: str\n'
        '    content: str\n'
        '    created_at: datetime = field(default_factory=datetime.now)\n'
        '    updated_at: datetime = field(default_factory=datetime.now)\n'
        '    id: Optional[str] = None\n'
    )
    patched = original + (
        '    def to_dict(self) -> Dict[str, Any]:\n'
        '        return {"subject": self.subject}\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(title='Реализовать модель', description='Сериализовать в словарь.', project='demo'),
        target_qualname='note.note_model.Note',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_model.py'],
        target_file='note/note_model.py',
        related_symbols=[],
        required_class_members=[],
    )

    assert not block.ok
    codes = {issue.code for issue in block.issues}
    assert 'unknown_annotation_name' in codes
    details = block.details.get('annotation_name_check') or {}
    assert set(details.get('unknown_names') or []) >= {'Dict', 'Any'}


def test_patch_static_semantics_reports_possible_existing_method_contract_lost_as_warning() -> None:
    original = (
        'class Note:\n'
        '    def is_modified(self, other: "Note") -> bool:\n'
        '        """Check if note changed.\n\n'
        '        Raises:\n'
        '            TypeError: If other is not Note.\n'
        '        """\n'
        '        if not isinstance(other, Note):\n'
        '            raise TypeError("bad")\n'
        '        return self.subject != other.subject\n'
    )
    patched = (
        'class Note:\n'
        '    def is_modified(self, other: "Note") -> bool:\n'
        '        """Check if note changed."""\n'
        '        return self.subject != other.subject\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(title='Реализовать модель', description='Добавить сериализацию.', project='demo'),
        target_qualname='note.note_model.Note',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_model.py'],
        target_file='note/note_model.py',
        related_symbols=[],
        required_class_members=[{'name': 'is_modified', 'kind': 'method', 'required': True}],
    )

    assert block.ok
    warning_details = block.details.get('possible_existing_method_contract_lost') or {}
    warnings = warning_details.get('warnings') or []
    assert warnings
    assert warnings[0]['code'] == 'possible_existing_method_contract_lost'
    assert warnings[0]['missing_exception_contracts'] == ['TypeError']


def test_patch_static_semantics_rejects_method_replace_moved_to_module_level() -> None:
    original = (
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        raise NotImplementedError\n'
    )
    patched = (
        'class NoteStorage:\n'
        '    pass\n'
        '\n'
        'def __init__(self, base_dir):\n'
        '    self.base_dir = base_dir\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Инициализация файлового хранилища заметок',
            description='Конструктор NoteStorage должен создавать корневую директорию.',
            project='demo',
        ),
        target_qualname='note.note_storage.NoteStorage.__init__',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['note/note_storage.py'],
        target_file='note/note_storage.py',
    )

    assert not block.ok
    assert any(issue.code == 'target_method_moved_to_module_level' for issue in block.issues)



def test_generated_test_static_semantics_accepts_constructor_call_for_init_target(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_note_storage.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n'
        '\n'
        'def test_storage_initializes_base_dir(tmp_path):\n'
        '    base_dir = tmp_path / "notes"\n'
        '    storage = NoteStorage(base_dir)\n'
        '    assert storage.base_dir == base_dir\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.__init__',
        requested_operation='replace_symbol',
        generated_symbol_names=[],
    )

    assert block.ok
    assert block.issues == []
    assert block.details['target_reference_names'] == ['NoteStorage', '__init__']

def test_generated_test_relevance_accepts_constructor_call_for_init_target() -> None:
    test_source = (
        'from pathlib import Path\n'
        'from note.note_storage import NoteStorage\n'
        '\n'
        'def test_storage_initializes_base_dir(tmp_path):\n'
        '    base_dir = tmp_path / "notes"\n'
        '    storage = NoteStorage(base_dir)\n'
        '    assert storage.base_dir == base_dir\n'
    )

    block = validate_generated_test_relevance(
        test_source=test_source,
        requested_operation='replace_symbol',
        target_qualname='note.note_storage.NoteStorage.__init__',
        generated_symbol_names=[],
    )

    assert block.ok
    assert block.issues == []
    assert block.details['target_reference_names'] == ['NoteStorage', '__init__']


def test_generated_test_static_semantics_rejects_missing_required_constructor_arg(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage():\n'
        '    storage = NoteStorage()\n'
        '    assert storage\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.generate_filename',
        requested_operation='replace_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.generate_filename'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_missing_required_constructor_argument' for issue in block.issues)
    details = block.details['constructor_keyword_check']
    assert details['required_constructor_fields']['NoteStorage'] == ['base_dir']
    assert details['checked_calls'][0]['missing_required_arguments'] == ['base_dir']


def test_generated_test_static_semantics_rejects_string_for_path_constructor_arg(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'from pathlib import Path\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path):\n'
        '        self.base_dir = base_dir\n'
        '    def generate_filename(self, note):\n'
        '        return "name.note"\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage():\n'
        '    storage = NoteStorage(base_dir=".")\n'
        '    assert storage.generate_filename(object()) == "name.note"\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.generate_filename',
        requested_operation='replace_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.generate_filename'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_constructor_argument_type_mismatch' for issue in block.issues)
    details = block.details['constructor_keyword_check']
    assert details['checked_calls'][0]['field_annotations']['base_dir'] == 'Path'
    assert details['checked_calls'][0]['type_issue_count'] == 1


def test_generated_test_static_semantics_accepts_required_constructor_keyword(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def generate_filename(self, note):\n'
        '        return "name.note"\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage(tmp_path):\n'
        '    storage = NoteStorage(base_dir=tmp_path)\n'
        '    assert storage.generate_filename(object()) == "name.note"\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.generate_filename',
        requested_operation='replace_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.generate_filename'],
    )

    assert block.ok
    assert block.details['constructor_keyword_check']['checked_calls'][0]['missing_required_arguments'] == []


def test_patch_static_semantics_rejects_unknown_self_attributes_in_method_replace(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'from pathlib import Path\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        relative_path = self._path_builder.build_path(note.created_at, "x.note")\n'
        '        return self._base_dir / relative_path\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.get_save_path',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Путь', description='Реализовать путь', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'NotePathBuilder',
                'qualname': 'common.utils.NotePathBuilder',
                'source_code': 'class NotePathBuilder:\n    def build_path(self, date, filename):\n        pass\n',
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == 'unknown_self_attribute' for issue in block.issues)
    assert any(issue.code == 'unknown_injected_dependency_attribute' for issue in block.issues)
    unknown = block.details['self_attribute_usage_check']['unknown_attributes']
    assert any(item['attribute'] == '_base_dir' and item['line'] == 9 and item['suggested_replacements'] == ['base_dir'] for item in unknown)
    assert any(item['attribute'] == '_path_builder' and item['line'] == 8 and item['suggested_replacements'] == ['path_builder'] for item in unknown)



def test_patch_static_semantics_rejects_unknown_self_method_calls_in_method_replace(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def find_note_file_by_id(self, note_id: str) -> Path:\n'
        '        return self.base_dir / f"{note_id}.note"\n'
        '    def load(self, note_id: str):\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'from pathlib import Path\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def find_note_file_by_id(self, note_id: str) -> Path:\n'
        '        return self.base_dir / f"{note_id}.note"\n'
        '    def load(self, note_id: str):\n'
        '        file_path = self._find_file_by_id(note_id)\n'
        '        return file_path\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Реализовать загрузку', project='demo'),
    )

    assert not block.ok
    assert any(issue.code == 'unknown_self_method' for issue in block.issues)
    unknown_methods = block.details['self_attribute_usage_check']['unknown_methods']
    assert unknown_methods == [
        {
            'method': '_find_file_by_id',
            'line': 9,
            'suggested_replacements': ['find_note_file_by_id'],
        }
    ]

def test_patch_static_semantics_accepts_visible_self_attributes_in_method_replace(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n\n'
        'class NotePathBuilder:\n'
        '    def build_path(self, date, filename):\n'
        '        pass\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'from pathlib import Path\n\n'
        'class NotePathBuilder:\n'
        '    def build_path(self, date, filename):\n'
        '        pass\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        relative_path = self.path_builder.build_path(note.created_at, "x.note")\n'
        '        return self.base_dir / relative_path\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.get_save_path',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Путь', description='Реализовать путь', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'NotePathBuilder',
                'qualname': 'common.utils.NotePathBuilder',
                'source_code': 'class NotePathBuilder:\n    def build_path(self, date, filename):\n        pass\n',
            }
        ],
    )

    assert block.ok
    assert block.details['self_attribute_usage_check']['unknown_attributes'] == []
    assert block.details['injected_dependency_method_check']['checked_calls'][0]['access_path'] == 'self.path_builder'


def test_patch_static_semantics_rejects_operator_incompatible_with_visible_dependency_field_type(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n\n'
        'class NotePathBuilder:\n'
        '    def __init__(self, base_dir: str = "notes"):\n'
        '        self.base_dir = base_dir\n'
        '    def build_path(self, date, filename):\n'
        '        pass\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'from pathlib import Path\n\n'
        'class NotePathBuilder:\n'
        '    def __init__(self, base_dir: str = "notes"):\n'
        '        self.base_dir = base_dir\n'
        '    def build_path(self, date, filename):\n'
        '        pass\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def get_save_path(self, note):\n'
        '        directory_name = note.created_at.strftime("%Y-%m-%d")\n'
        '        filename = "name.note"\n'
        '        return self.path_builder.base_dir / directory_name / filename\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.get_save_path',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Путь', description='Реализовать путь', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'NotePathBuilder',
                'qualname': 'common.utils.NotePathBuilder',
                'source_code': (
                    'class NotePathBuilder:\n'
                    '    def __init__(self, base_dir: str = "notes"):\n'
                    '        self.base_dir = base_dir\n'
                    '    def build_path(self, date, filename):\n'
                    '        pass\n'
                ),
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == 'incompatible_operator_for_visible_type' for issue in block.issues)
    details = block.details['visible_type_operator_check']
    assert details['checked_operands'][0]['expression'] == 'self.path_builder.base_dir'
    assert details['checked_operands'][0]['field_type'] == 'str'


def test_patch_static_semantics_rejects_visible_contract_positional_type_mismatch(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n'
        'from datetime import datetime\n\n'
        'class NotePathBuilder:\n'
        '    def __init__(self, base_dir: str = "notes"):\n'
        '        self.base_dir = base_dir\n'
        '    def build_path(self, date: datetime, filename: str) -> Path:\n'
        '        return Path(self.base_dir) / date.strftime("%Y-%m-%d") / filename\n\n'
        'class Note:\n'
        '    subject: str\n'
        '    created_at: datetime\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '        self.path_builder = NotePathBuilder(base_dir=str(base_dir))\n'
        '    def generate_filename(self, note: Note) -> str:\n'
        '        return "name.note"\n'
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        raise NotImplementedError()\n',
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        folder_name = note.created_at.strftime("%Y-%m-%d")\n'
        '        filename = self.generate_filename(note)\n'
        '        return self.path_builder.build_path(folder_name, filename)\n',
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.get_save_path',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Путь', description='Реализовать путь', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'NotePathBuilder',
                'qualname': 'common.utils.NotePathBuilder',
                'source_code': (
                    'from pathlib import Path\n'
                    'from datetime import datetime\n\n'
                    'class NotePathBuilder:\n'
                    '    def __init__(self, base_dir: str = "notes"):\n'
                    '        self.base_dir = base_dir\n'
                    '    def build_path(self, date: datetime, filename: str) -> Path:\n'
                    '        return Path(self.base_dir) / date.strftime("%Y-%m-%d") / filename\n'
                ),
            },
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from datetime import datetime\n\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    created_at: datetime\n'
                ),
            },
        ],
    )

    assert not block.ok
    assert any(issue.code == 'contract_call_argument_type_mismatch' for issue in block.issues)
    checked = block.details['contract_call_signature_check']['checked_calls']
    mismatch = checked[0]['argument_type_mismatches'][0]
    assert mismatch['argument'] == 'date'
    assert mismatch['expected_type'] == 'datetime'
    assert mismatch['actual_type'] == 'str'



def test_patch_static_semantics_rejects_unknown_runtime_name_in_production_code(tmp_path: Path) -> None:
    original = (
        'from pathlib import Path\n'
        'import json\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str):\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str):\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str):\n'
        '        for root, _dirs, files in os.walk(self.base_dir):\n'
        '            return json.loads("{}")\n'
        '        raise FileNotFoundError(note_id)\n',
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Реализовать загрузку', project='demo'),
        related_symbols=[],
    )

    assert not block.ok
    assert any(issue.code == 'unknown_runtime_name' for issue in block.issues)
    assert block.details['runtime_name_check']['unknown_names'] == ['os']


def test_patch_static_semantics_rejects_serialized_value_for_typed_model_field(tmp_path: Path) -> None:
    original = (
        'import json\n'
        'from datetime import datetime\n'
        'from pathlib import Path\n'
        'from note.note_model import Note\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str) -> Note:\n'
        '        file_path = self.base_dir / f"{note_id}.note"\n'
        '        with file_path.open("r", encoding="utf-8") as handle:\n'
        '            data = json.load(handle)\n'
        '        return Note(\n'
        '            subject=data["subject"],\n'
        '            content=data["content"],\n'
        '            created_at=data["created_at"],\n'
        '            updated_at=data.get("updated_at"),\n'
        '            id=data.get("id") or note_id,\n'
        '        )\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Восстановить заметку из JSON', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n'
                    'from typing import Optional\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                    '    updated_at: datetime = field(default_factory=datetime.now)\n'
                    '    id: Optional[str] = None\n'
                ),
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == 'model_constructor_field_type_mismatch' for issue in block.issues)
    mismatches = block.details['model_surface_usage_check']['checked_constructor_calls'][0]['field_type_mismatches']
    mismatch_fields = {item['field'] for item in mismatches}
    assert {'created_at', 'updated_at'} <= mismatch_fields


def test_patch_static_semantics_accepts_explicit_conversion_for_typed_model_field(tmp_path: Path) -> None:
    original = (
        'import json\n'
        'from datetime import datetime\n'
        'from pathlib import Path\n'
        'from note.note_model import Note\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str) -> Note:\n'
        '        file_path = self.base_dir / f"{note_id}.note"\n'
        '        with file_path.open("r", encoding="utf-8") as handle:\n'
        '            data = json.load(handle)\n'
        '        return Note(\n'
        '            subject=data["subject"],\n'
        '            content=data["content"],\n'
        '            created_at=datetime.fromisoformat(data["created_at"]),\n'
        '            updated_at=datetime.fromisoformat(data["updated_at"]),\n'
        '            id=data.get("id") or note_id,\n'
        '        )\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Восстановить заметку из JSON', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n'
                    'from typing import Optional\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                    '    updated_at: datetime = field(default_factory=datetime.now)\n'
                    '    id: Optional[str] = None\n'
                ),
            }
        ],
    )

    assert block.ok
    assert not any(issue.code == 'model_constructor_field_type_mismatch' for issue in block.issues)


def test_patch_static_semantics_accepts_none_for_optional_model_field(tmp_path: Path) -> None:
    original = (
        'from datetime import datetime\n'
        'from note.note_model import Note\n\n'
        'class EditorWindow:\n'
        '    def save_note(self) -> None:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def save_note(self) -> None:\n'
        '        raise NotImplementedError()\n',
        '    def save_note(self) -> None:\n'
        '        self.note = Note(\n'
        '            subject="Новая заметка",\n'
        '            content="Текст",\n'
        '            id=None,\n'
        '        )\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='editor/editor_window.py',
        target_qualname='editor.editor_window.EditorWindow.save_note',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='editor.editor_window.EditorWindow',
        changed_files=['editor/editor_window.py'],
        change_request=ChangeRequest(title='Сохранение', description='Создать заметку', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n'
                    'from typing import Optional\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                    '    updated_at: datetime = field(default_factory=datetime.now)\n'
                    '    id: Optional[str] = None\n'
                ),
            }
        ],
    )

    assert block.ok
    assert not any(issue.code == 'model_constructor_field_type_mismatch' for issue in block.issues)
    checked = block.details['model_surface_usage_check']['checked_constructor_calls'][0]
    assert checked['field_type_mismatches'] == []


def test_patch_static_semantics_blocks_none_for_default_typed_model_field(tmp_path: Path) -> None:
    original = (
        'import json\n'
        'from datetime import datetime\n'
        'from pathlib import Path\n'
        'from note.note_model import Note\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str) -> Note:\n'
        '        file_path = self.base_dir / f"{note_id}.note"\n'
        '        with file_path.open("r", encoding="utf-8") as handle:\n'
        '            data = json.load(handle)\n'
        '        created_at = None\n'
        '        if data.get("created_at"):\n'
        '            created_at = datetime.fromisoformat(data["created_at"])\n'
        '        updated_at = None\n'
        '        if data.get("updated_at"):\n'
        '            updated_at = datetime.fromisoformat(data["updated_at"])\n'
        '        return Note(\n'
        '            subject=data["subject"],\n'
        '            content=data["content"],\n'
        '            created_at=created_at,\n'
        '            updated_at=updated_at,\n'
        '            id=data.get("id") or note_id,\n'
        '        )\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Восстановить заметку из JSON', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n'
                    'from typing import Optional\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                    '    updated_at: datetime = field(default_factory=datetime.now)\n'
                    '    id: Optional[str] = None\n'
                ),
            }
        ],
    )

    assert not block.ok
    assert any(issue.code == 'model_constructor_default_field_overridden_with_none' for issue in block.issues)
    details = block.details['model_surface_usage_check']['checked_constructor_calls'][0]
    assert {'created_at', 'updated_at'} <= {item['field'] for item in details['default_none_mismatches']}


def test_patch_static_semantics_accepts_omitted_default_typed_model_field(tmp_path: Path) -> None:
    original = (
        'import json\n'
        'from datetime import datetime\n'
        'from pathlib import Path\n'
        'from note.note_model import Note\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str) -> Note:\n'
        '        file_path = self.base_dir / f"{note_id}.note"\n'
        '        with file_path.open("r", encoding="utf-8") as handle:\n'
        '            data = json.load(handle)\n'
        '        kwargs = {\n'
        '            "subject": data["subject"],\n'
        '            "content": data["content"],\n'
        '            "id": data.get("id") or note_id,\n'
        '        }\n'
        '        if data.get("created_at"):\n'
        '            kwargs["created_at"] = datetime.fromisoformat(data["created_at"])\n'
        '        if data.get("updated_at"):\n'
        '            kwargs["updated_at"] = datetime.fromisoformat(data["updated_at"])\n'
        '        return Note(**kwargs)\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Восстановить заметку из JSON', project='demo'),
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n'
                    'from typing import Optional\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                    '    updated_at: datetime = field(default_factory=datetime.now)\n'
                    '    id: Optional[str] = None\n'
                ),
            }
        ],
    )

    assert block.ok
    assert not any(issue.code == 'model_constructor_default_field_overridden_with_none' for issue in block.issues)


def test_patch_static_semantics_does_not_treat_local_annassign_value_names_as_annotations() -> None:
    original = (
        'import json\n'
        'from pathlib import Path\n'
        'from note.note_model import Note\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def load(self, note_id: str) -> Note:\n'
        '        raise NotImplementedError()\n',
        '    def load(self, note_id: str) -> Note:\n'
        '        file_path = self.base_dir / f"{note_id}.note"\n'
        '        with file_path.open("r", encoding="utf-8") as handle:\n'
        '            data = json.load(handle)\n'
        '        kwargs: dict = {\n'
        '            "subject": data["subject"],\n'
        '            "content": data["content"],\n'
        '            "id": data.get("id") or note_id,\n'
        '        }\n'
        '        return Note(**kwargs)\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Восстановить заметку из JSON', project='demo'),
        related_symbols=[],
    )

    assert not any(issue.code == 'unknown_annotation_name' for issue in block.issues)
    details = block.details.get('annotation_name_check') or {}
    unknown_names = set(details.get('unknown_names') or [])
    assert 'data' not in unknown_names
    assert 'note_id' not in unknown_names


def test_patch_static_semantics_does_not_treat_model_method_call_as_unknown_model_attribute() -> None:
    original = (
        'from pathlib import Path\n'
        'from datetime import datetime\n'
        'from note.note_model import Note\n'
        'from common.utils import NotePathBuilder\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir: Path) -> None:\n'
        '        self.base_dir = base_dir\n'
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        raise NotImplementedError()\n'
    )
    patched = original.replace(
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        raise NotImplementedError()\n',
        '    def get_save_path(self, note: Note) -> Path:\n'
        '        path_builder = NotePathBuilder(base_dir=str(self.base_dir))\n'
        '        filename = f"{note.subject}.note"\n'
        '        return path_builder.build_path(note.created_at, filename)\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.get_save_path',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Путь', description='Построить путь к файлу', project='demo'),
        model_surfaces=[
            {
                'name': 'NotePathBuilder',
                'qualname': 'common.utils.NotePathBuilder',
                'fields': ['base_dir'],
                'constructor_fields': ['base_dir'],
            }
        ],
        related_symbols=[
            {
                'kind': 'class',
                'name': 'Note',
                'qualname': 'note.note_model.Note',
                'source_code': (
                    'from dataclasses import dataclass, field\n'
                    'from datetime import datetime\n\n'
                    '@dataclass\n'
                    'class Note:\n'
                    '    subject: str\n'
                    '    content: str\n'
                    '    created_at: datetime = field(default_factory=datetime.now)\n'
                ),
            }
        ],
    )

    assert not any(issue.code == 'unknown_model_attribute' for issue in block.issues)
    model_details = block.details.get('model_surface_usage_check') or {}
    checked = model_details.get('checked_attributes') or []
    assert not any(item.get('attribute') == 'build_path' for item in checked)


def test_patch_static_semantics_rejects_self_access_to_module_constant(tmp_path: Path) -> None:
    original = (
        'DEFAULT_ENCODING = "utf-8"\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def load_from_file(self, file_path):\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'DEFAULT_ENCODING = "utf-8"\n\n'
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def load_from_file(self, file_path):\n'
        '        with file_path.open("r", encoding=self.DEFAULT_ENCODING) as f:\n'
        '            return f.read()\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='note/note_storage.py',
        target_qualname='note.note_storage.NoteStorage.load_from_file',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='note.note_storage.NoteStorage',
        changed_files=['note/note_storage.py'],
        change_request=ChangeRequest(title='Загрузка', description='Реализовать загрузку', project='demo'),
    )

    assert not block.ok
    assert any(issue.code == 'unknown_self_attribute' for issue in block.issues)
    unknown = block.details['self_attribute_usage_check']['unknown_attributes']
    assert any(
        item['attribute'] == 'DEFAULT_ENCODING'
        and item['suggested_replacements'] == ['DEFAULT_ENCODING']
        and item.get('replacement_kind') == 'module_level_name'
        and item.get('suggested_expression') == 'DEFAULT_ENCODING'
        for item in unknown
    )


def test_generated_test_static_semantics_rejects_unknown_project_attribute_assignment(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def search_by_content(self, query):\n'
        '        return []\n'
        '    def search_results_by_content(self, query):\n'
        '        return self.search_by_content(query)\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage(tmp_path):\n'
        '    storage = NoteStorage(tmp_path)\n'
        '    storage._notes = []\n'
        '    assert storage.search_results_by_content("x") == []\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.search_results_by_content',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.search_results_by_content'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_assigns_unknown_project_attribute' for issue in block.issues)
    details = block.details['project_attribute_assignment_check']
    assert details['checked_assignments'][0]['attribute'] == '_notes'


def test_generated_test_static_semantics_allows_overriding_visible_helper_method(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_storage.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class NoteStorage:\n'
        '    def __init__(self, base_dir):\n'
        '        self.base_dir = base_dir\n'
        '    def search_by_content(self, query):\n'
        '        return []\n'
        '    def search_results_by_content(self, query):\n'
        '        return self.search_by_content(query)\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_storage import NoteStorage\n\n'
        'def test_storage(tmp_path):\n'
        '    storage = NoteStorage(tmp_path)\n'
        '    storage.search_by_content = lambda query: ["ok"]\n'
        '    assert storage.search_results_by_content("x") == ["ok"]\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_storage.NoteStorage.search_results_by_content',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['note.note_storage.NoteStorage.search_results_by_content'],
    )

    assert block.ok
    assert block.details['project_attribute_assignment_check']['checked_assignments'][0]['attribute'] == 'search_by_content'


def test_generation_request_includes_available_imports_when_full_file_is_not_included(tmp_path: Path) -> None:
    config = load_config()
    project_root = tmp_path
    target_file = project_root / 'app' / 'storage.py'
    target_file.parent.mkdir(parents=True)
    module_source = (
        'import json\n'
        'from datetime import datetime\n'
        'from pathlib import Path\n\n'
        'class Storage:\n'
        '    def save(self, value):\n'
        '        return json.dumps({"value": value})\n'
    )
    target_file.write_text(module_source, encoding='utf-8')
    target = _symbol(
        qualname='app.storage.Storage.save',
        name='save',
        kind='method',
        source='def save(self, value):\n        return json.dumps({"value": value})\n',
        file_path='app/storage.py',
        parent='app.storage.Storage',
    )
    module = _symbol(
        qualname='app.storage',
        name='storage',
        kind='module',
        source=module_source,
        file_path='app/storage.py',
        parent=None,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )

    request = build_generation_request(
        project_root,
        ChangeRequest(title='Обновить сохранение', description='Использовать дату.', project='demo'),
        'app.storage.Storage.save',
        context_pack,
        config,
        operation='replace_symbol',
    )

    assert request['project_context']['full_file_source'] == ''
    available = request['project_context']['available_imports']
    assert {'name': 'json', 'kind': 'import', 'module': 'json', 'imported': '', 'asname': '', 'source': 'import json'} in available
    assert any(item['name'] == 'datetime' and item['source'] == 'from datetime import datetime' for item in available)
    assert request['options'].get('available_imports_count') == len(available)


def test_patch_static_semantics_rejects_preserved_assignment_used_after_output_data() -> None:
    original = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        path = self.path_for(item)\n'
        '        if item.identifier is None:\n'
        '            item.identifier = path.stem\n'
        '        data = {"id": item.identifier, "name": item.name}\n'
        '        json.dump(data, open(path, "w"))\n'
        '        return str(path)\n'
    )
    patched = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        path = self.path_for(item)\n'
        '        data = {"id": item.identifier, "name": item.name}\n'
        '        if item.identifier is None:\n'
        '            item.identifier = path.stem\n'
        '        json.dump(data, open(path, "w"))\n'
        '        return str(path)\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Сохранение объекта',
            description='При сохранении нужно сохранить текущую структуру записи и не менять формат данных.',
            project='demo',
        ),
        target_qualname='sample.store.Store.save',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['sample/store.py'],
        target_file='sample/store.py',
        related_symbols=[],
    )

    assert not block.ok
    assert any(issue.code == 'preserved_assignment_used_before_assignment' for issue in block.issues)
    details = block.details.get('preserved_assignment_order_check') or {}
    assert details.get('skipped') is False


def test_patch_static_semantics_allows_preserved_assignment_order_when_kept() -> None:
    original = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        path = self.path_for(item)\n'
        '        if item.identifier is None:\n'
        '            item.identifier = path.stem\n'
        '        data = {"id": item.identifier, "name": item.name}\n'
        '        json.dump(data, open(path, "w"))\n'
        '        return str(path)\n'
    )
    patched = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        path = self.path_for(item)\n'
        '        if item.identifier is None:\n'
        '            item.identifier = path.stem\n'
        '        item.updated_at = self.now()\n'
        '        data = {"id": item.identifier, "name": item.name}\n'
        '        json.dump(data, open(path, "w"))\n'
        '        return str(path)\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Сохранение объекта',
            description='При сохранении нужно сохранить текущую структуру записи и не менять формат данных.',
            project='demo',
        ),
        target_qualname='sample.store.Store.save',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['sample/store.py'],
        target_file='sample/store.py',
        related_symbols=[],
    )

    assert not any(issue.code == 'preserved_assignment_used_before_assignment' for issue in block.issues)


def test_patch_static_semantics_rejects_removed_preserved_dict_key() -> None:
    original = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        data = {"id": item.identifier, "name": item.name, "updated_at": item.updated_at}\n'
        '        json.dump(data, open("x", "w"))\n'
        '        return "x"\n'
    )
    patched = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        item.updated_at = self.now()\n'
        '        data = {"name": item.name, "updated_at": item.updated_at}\n'
        '        json.dump(data, open("x", "w"))\n'
        '        return "x"\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Сохранение объекта',
            description='При сохранении нужно сохранить текущий формат данных.',
            project='demo',
        ),
        target_qualname='sample.store.Store.save',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['sample/store.py'],
        target_file='sample/store.py',
        related_symbols=[],
    )

    assert not block.ok
    assert any(issue.code == 'preserved_dict_key_removed' for issue in block.issues)
    details = block.details.get('preserved_dict_key_check') or {}
    assert details.get('skipped') is False
    assert details['checked_dicts'][0]['removed_keys'] == ['id']


def test_patch_static_semantics_allows_preserved_dict_keys_when_kept() -> None:
    original = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        data = {"id": item.identifier, "name": item.name}\n'
        '        json.dump(data, open("x", "w"))\n'
        '        return "x"\n'
    )
    patched = (
        'import json\n\n'
        'class Store:\n'
        '    def save(self, item):\n'
        '        item.updated_at = self.now()\n'
        '        data = {"id": item.identifier, "name": item.name, "updated_at": item.updated_at}\n'
        '        json.dump(data, open("x", "w"))\n'
        '        return "x"\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(
            title='Сохранение объекта',
            description='При сохранении нужно сохранить текущий формат данных.',
            project='demo',
        ),
        target_qualname='sample.store.Store.save',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['sample/store.py'],
        target_file='sample/store.py',
        related_symbols=[],
    )

    assert not any(issue.code == 'preserved_dict_key_removed' for issue in block.issues)


def test_generated_test_static_semantics_flags_post_init_assignment_to_constructor_field(tmp_path: Path) -> None:
    module_file = tmp_path / 'note' / 'note_model.py'
    module_file.parent.mkdir(parents=True)
    module_file.write_text(
        'class Note:\n'
        '    def __init__(self, subject, content, id=None):\n'
        '        self.subject = subject\n'
        '        self.content = content\n'
        '        self._internal = None\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'from note.note_model import Note\n\n'
        'def test_note():\n'
        '    note = Note(subject="s", content="c")\n'
        '    note.id = "existing"\n'
        '    assert note.id == "existing"\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='note.note_model.Note.__init__',
        requested_operation='replace_symbol',
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_assigns_project_model_field_after_construction' for issue in block.issues)
    details = block.details['project_attribute_assignment_check']
    assert details['checked_assignments'][0]['visible_constructor_fields'] == ['content', 'id', 'subject']


def test_generation_request_includes_full_file_for_preserve_replace_mode(tmp_path: Path) -> None:
    config = load_config()
    project_root = tmp_path
    target_file = project_root / 'app' / 'storage.py'
    target_file.parent.mkdir(parents=True)
    module_source = (
        'import json\n\n'
        'class Storage:\n'
        '    def save(self, item):\n'
        '        data = {"id": item.id, "name": item.name}\n'
        '        return json.dumps(data)\n'
    )
    target_file.write_text(module_source, encoding='utf-8')
    target = _symbol(
        qualname='app.storage.Storage.save',
        name='save',
        kind='method',
        source='def save(self, item):\n        data = {"id": item.id, "name": item.name}\n        return json.dumps(data)\n',
        file_path='app/storage.py',
        parent='app.storage.Storage',
    )
    module = _symbol(
        qualname='app.storage',
        name='storage',
        kind='module',
        source=module_source,
        file_path='app/storage.py',
        parent=None,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )

    request = build_generation_request(
        project_root,
        ChangeRequest(
            title='Обновить сохранение',
            description='Сохранить текущую структуру и формат данных при замене метода.',
            project='demo',
        ),
        'app.storage.Storage.save',
        context_pack,
        config,
        operation='replace_symbol',
    )

    assert request['project_context']['full_file_source'] == module_source
    assert len(request['project_context']['full_file_source']) == len(module_source)


def test_insert_after_symbol_accepts_module_level_constants() -> None:
    from codecollector.domain.models import ChangeRequest
    from codecollector.validation.semantic_checks import validate_patch_static_semantics

    original = 'class SearchModes:\n    DATE = "date"\n'
    patched = (
        'class SearchModes:\n'
        '    DATE = "date"\n\n'
        'APP_NAME: str = "Заметки"\n'
        'AUTO_SAVE_INTERVAL_MS: int = 300000\n'
        'NOTES_DIR: str = "notes"\n'
    )

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Добавить общие значения',
            description='Добавить константы уровня модуля.',
            project='demo',
        ),
        target_qualname='common.constants.SearchModes',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['common/constants.py'],
        target_file='common/constants.py',
        insert_scope='module_body',
    )

    assert block.ok, [issue.code for issue in block.issues]
    assert block.details['new_symbols'] == {
        'APP_NAME': 'constant',
        'AUTO_SAVE_INTERVAL_MS': 'constant',
        'NOTES_DIR': 'constant',
    }


def test_patch_static_semantics_accepts_methods_from_visible_local_base_class(tmp_path: Path) -> None:
    original = (
        'class BaseWindow:\n'
        '    def set_content(self, widget):\n'
        '        pass\n\n'
        'class EditorWindow(BaseWindow):\n'
        '    def __init__(self):\n'
        '        self.editor = object()\n'
        '    def _setup_ui(self) -> None:\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'class BaseWindow:\n'
        '    def set_content(self, widget):\n'
        '        pass\n\n'
        'class EditorWindow(BaseWindow):\n'
        '    def __init__(self):\n'
        '        self.editor = object()\n'
        '    def _setup_ui(self) -> None:\n'
        '        self.set_content(self.editor)\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='editor/editor_window.py',
        target_qualname='editor.editor_window.EditorWindow._setup_ui',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='editor.editor_window.EditorWindow',
        changed_files=['editor/editor_window.py'],
        change_request=ChangeRequest(title='UI', description='Минимальная настройка UI', project='demo'),
    )

    assert block.ok
    details = block.details['self_attribute_usage_check']
    assert 'set_content' in details['inherited_methods']
    assert details['inherited_methods_by_base']['BaseWindow'] == ['set_content']


def test_patch_static_semantics_accepts_allowlisted_external_base_class_methods(tmp_path: Path) -> None:
    original = (
        'from PyQt5.QtWidgets import QMainWindow\n\n'
        'class EditorWindow(QMainWindow):\n'
        '    def __init__(self):\n'
        '        self.editor = object()\n'
        '    def _setup_ui(self) -> None:\n'
        '        raise NotImplementedError()\n'
    )
    patched = (
        'from PyQt5.QtWidgets import QMainWindow\n\n'
        'class EditorWindow(QMainWindow):\n'
        '    def __init__(self):\n'
        '        self.editor = object()\n'
        '    def _setup_ui(self) -> None:\n'
        '        self.setCentralWidget(self.editor)\n'
        '        self.statusBar().showMessage("Готов к работе")\n'
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='editor/editor_window.py',
        target_qualname='editor.editor_window.EditorWindow._setup_ui',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='editor.editor_window.EditorWindow',
        changed_files=['editor/editor_window.py'],
        change_request=ChangeRequest(title='UI', description='Минимальная настройка UI', project='demo'),
    )

    assert block.ok
    details = block.details['self_attribute_usage_check']
    assert details['base_class_names'] == ['QMainWindow']
    assert 'setCentralWidget' in details['inherited_methods']
    assert 'statusBar' in details['inherited_methods']



def test_patch_static_semantics_accepts_guarded_import_names_in_annotations() -> None:
    original = (
        'try:\n'
        '    from optional_pkg.widgets import Widget\n'
        'except ImportError:\n'
        '    Widget = None\n\n'
        'class Window:\n'
        '    def build(self):\n'
        '        self.widget = None\n'
    )
    patched = original.replace(
        '    def build(self):\n'
        '        self.widget = None\n',
        '    def build(self):\n'
        '        self.widget: Widget = self.create_widget()\n',
    )

    block = validate_patch_static_semantics(
        original_file_text=original,
        patched_file_text=patched,
        target_file='app/window.py',
        target_qualname='app.window.Window.build',
        requested_operation='replace_symbol',
        insert_scope='class_body',
        parent_qualname='app.window.Window',
        changed_files=['app/window.py'],
        change_request=ChangeRequest(title='Окно', description='Обновить построение окна', project='demo'),
        related_symbols=[],
    )

    assert not any(issue.code in {'unknown_runtime_name', 'unknown_annotation_name'} for issue in block.issues)
    assert 'Widget' in block.details['runtime_name_check']['available_names']


def test_generation_request_includes_guarded_imports_in_available_imports(tmp_path: Path) -> None:
    config = load_config()
    project_root = tmp_path
    target_file = project_root / 'app' / 'window.py'
    target_file.parent.mkdir(parents=True)
    module_source = (
        'try:\n'
        '    from optional_pkg.widgets import Widget\n'
        'except ImportError:\n'
        '    Widget = None\n\n'
        'class Window:\n'
        '    def build(self):\n'
        '        self.widget = Widget()\n'
    )
    target_file.write_text(module_source, encoding='utf-8')
    target = _symbol(
        qualname='app.window.Window.build',
        name='build',
        kind='method',
        source='def build(self):\n        self.widget = Widget()\n',
        file_path='app/window.py',
        parent='app.window.Window',
    )
    module = _symbol(
        qualname='app.window',
        name='window',
        kind='module',
        source=module_source,
        file_path='app/window.py',
        parent=None,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )

    request = build_generation_request(
        project_root,
        ChangeRequest(title='Окно', description='Обновить построение окна', project='demo'),
        'app.window.Window.build',
        context_pack,
        config,
        operation='replace_symbol',
    )

    available = request['project_context']['available_imports']
    assert any(
        item['name'] == 'Widget'
        and item['kind'] == 'from_import'
        and item['module'] == 'optional_pkg.widgets'
        and item['source'] == 'from optional_pkg.widgets import Widget'
        for item in available
    )
