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
from codecollector.validation.semantic_checks import validate_generated_test_static_semantics, validate_patch_static_semantics


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
