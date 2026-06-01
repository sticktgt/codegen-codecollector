from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codecollector.context.service import ContextService
from codecollector.domain.models import ChangeRequest, ContextPack, RelatedSymbolContext, RelationRecord, SymbolRecord
from codecollector.external_codegen.adapter import build_generation_request
from codecollector.validation.semantic_checks import validate_patch_static_semantics


class FakeStore:
    def __init__(self, symbols: dict[str, SymbolRecord], outbound: dict[str, list[RelationRecord]] | None = None, inbound: dict[str, list[RelationRecord]] | None = None) -> None:
        self.symbols = symbols
        self.outbound = outbound or {}
        self.inbound = inbound or {}

    def get_symbol(self, project_root: str, qualname: str) -> SymbolRecord | None:
        return self.symbols.get(qualname)

    def list_symbols_in_file(self, project_root: str, file_path: str) -> list[SymbolRecord]:
        return [item for item in self.symbols.values() if item.file_path == file_path]

    def list_outbound_relations(self, project_root: str, source_qualname: str) -> list[RelationRecord]:
        return list(self.outbound.get(source_qualname, []))

    def list_inbound_relations_for_qualname(self, project_root: str, target_qualname: str) -> list[RelationRecord]:
        return list(self.inbound.get(target_qualname, []))

    def list_inbound_relations_for_name(self, project_root: str, target_name: str) -> list[RelationRecord]:
        return []

    def list_symbols_by_short_name(self, project_root: str, short_name: str) -> list[SymbolRecord]:
        return [item for item in self.symbols.values() if item.name == short_name]

    def list_symbols(self, project_root: str) -> list[SymbolRecord]:
        return list(self.symbols.values())


class FakeOverlays:
    def requirement_details_for_symbol(self, qualname: str) -> list[dict[str, str]]:
        return []

    def symbol_title(self, qualname: str) -> str:
        return ""

    def symbol_description(self, qualname: str) -> str:
        return ""


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        codegenerator_include_full_file_for_generate_test=True,
        codegenerator_include_full_file_for_non_symbol_targets=True,
        codegenerator_generate_related_tests_max_items=0,
        codegenerator_generate_test_related_tests_max_items=0,
        codegenerator_repair_related_tests_max_items=0,
        codegenerator_generate_related_symbols_max_items=2,
        codegenerator_generate_related_symbol_chars=160,
        codegenerator_generate_test_related_symbols_max_items=2,
        codegenerator_generate_test_related_symbol_chars=160,
        codegenerator_repair_related_symbols_max_items=2,
        codegenerator_repair_related_symbol_chars=160,
        codegenerator_generate_reference_max_items=0,
        codegenerator_generate_test_reference_max_items=0,
        codegenerator_repair_reference_max_items=0,
        codegenerator_test_generation_mode='always',
    )


def test_context_service_collects_related_production_symbols_from_graph() -> None:
    target = SymbolRecord(
        file_path='support_app/api/ticket_api.py',
        module_name='support_app.api.ticket_api',
        name='create_ticket_endpoint',
        qualname='support_app.api.ticket_api.create_ticket_endpoint',
        kind='function',
        parent_qualname=None,
        start_line=1,
        end_line=3,
        source_code='def create_ticket_endpoint(payload):\n    return create_ticket(payload)\n',
    )
    service = SymbolRecord(
        file_path='support_app/services/ticket_service.py',
        module_name='support_app.services.ticket_service',
        name='create_ticket',
        qualname='support_app.services.ticket_service.create_ticket',
        kind='function',
        parent_qualname=None,
        start_line=1,
        end_line=3,
        source_code='def create_ticket(payload):\n    return {"id": payload["id"]}\n',
    )
    relation = RelationRecord(
        source_qualname=target.qualname,
        relation_kind='calls',
        target_ref='create_ticket',
        file_path=target.file_path,
        target_qualname=service.qualname,
        relation_confidence='high',
    )
    context = ContextService(
        Path('/tmp/project'),
        FakeStore({target.qualname: target, service.qualname: service}, outbound={target.qualname: [relation]}),
        FakeOverlays(),
    ).build_context(target.qualname)

    assert [item.qualname for item in context.related_symbols] == [service.qualname]
    assert context.related_symbols[0].role == 'called_by_target'
    assert context.related_symbols[0].signature == 'def create_ticket(payload):'


def test_generation_request_contains_contract_context_related_symbols(tmp_path: Path) -> None:
    source_dir = tmp_path / 'support_app' / 'api'
    source_dir.mkdir(parents=True)
    source_file = source_dir / 'ticket_api.py'
    source_file.write_text('def create_ticket_endpoint(payload):\n    return payload\n', encoding='utf-8')

    target = SymbolRecord(
        file_path='support_app/api/ticket_api.py',
        module_name='support_app.api.ticket_api',
        name='create_ticket_endpoint',
        qualname='support_app.api.ticket_api.create_ticket_endpoint',
        kind='function',
        parent_qualname=None,
        start_line=1,
        end_line=2,
        source_code='def create_ticket_endpoint(payload):\n    return payload\n',
    )
    related = RelatedSymbolContext(
        qualname='support_app.services.ticket_service.create_ticket',
        file_path='support_app/services/ticket_service.py',
        module_name='support_app.services.ticket_service',
        name='create_ticket',
        kind='function',
        parent_qualname=None,
        relation_kind='calls',
        relation_direction='outbound',
        relation_source='index',
        relation_confidence='high',
        role='called_by_target',
        signature='def create_ticket(payload):',
        source_code='def create_ticket(payload):\n    return {"id": payload["id"]}\n',
    )
    context = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[related],
    )

    request = build_generation_request(
        tmp_path,
        ChangeRequest(
            title='Добавить endpoint',
            description='Endpoint должен вызывать create_ticket.',
            project='demo',
        ),
        target.qualname,
        context,
        _config(),
        operation='replace_symbol',
    )

    contract_symbols = request['project_context']['contract_context']['related_symbols']
    assert contract_symbols[0]['qualname'] == related.qualname
    assert contract_symbols[0]['signature'] == 'def create_ticket(payload):'
    assert contract_symbols[0]['role'] == 'called_by_target'
    assert request['project_context']['related_symbols'] == contract_symbols


def test_class_target_collects_contracts_from_child_method_relations() -> None:
    controller = SymbolRecord(
        file_path='support_app/api/controllers.py',
        module_name='support_app.api.controllers',
        name='TicketController',
        qualname='support_app.api.controllers.TicketController',
        kind='class',
        parent_qualname='support_app.api.controllers',
        start_line=1,
        end_line=20,
        source_code='class TicketController:\n    pass\n',
    )
    endpoint = SymbolRecord(
        file_path='support_app/api/controllers.py',
        module_name='support_app.api.controllers',
        name='agent_summary_endpoint',
        qualname='support_app.api.controllers.TicketController.agent_summary_endpoint',
        kind='method',
        parent_qualname=controller.qualname,
        start_line=10,
        end_line=12,
        source_code='    def agent_summary_endpoint(self, agent_name: str) -> dict:\n        return build_agent_summary(agent_name, [])\n',
    )
    report_fn = SymbolRecord(
        file_path='support_app/services/report_service.py',
        module_name='support_app.services.report_service',
        name='build_agent_summary',
        qualname='support_app.services.report_service.build_agent_summary',
        kind='function',
        parent_qualname=None,
        start_line=1,
        end_line=2,
        source_code='def build_agent_summary(agent_name: str, tickets: list) -> AgentSummary:\n    return AgentSummary(agent_name, 0)\n',
    )
    relation = RelationRecord(
        source_qualname=endpoint.qualname,
        relation_kind='calls',
        target_ref='build_agent_summary',
        file_path=endpoint.file_path,
        target_qualname=report_fn.qualname,
        relation_confidence='high',
    )
    context = ContextService(
        Path('/tmp/project'),
        FakeStore(
            {
                controller.qualname: controller,
                endpoint.qualname: endpoint,
                report_fn.qualname: report_fn,
            },
            outbound={endpoint.qualname: [relation]},
        ),
        FakeOverlays(),
    ).build_context(controller.qualname)

    assert [item.qualname for item in context.related_symbols] == [report_fn.qualname]
    assert context.related_symbols[0].role == 'called_by_class_member'
    assert context.related_symbols[0].origin_qualname == endpoint.qualname
    assert context.related_symbols[0].signature.startswith('def build_agent_summary(agent_name: str, tickets: list)')


def test_generation_request_includes_related_symbol_origin_qualname(tmp_path: Path) -> None:
    source_dir = tmp_path / 'support_app' / 'api'
    source_dir.mkdir(parents=True)
    (source_dir / 'controllers.py').write_text('class TicketController:\n    pass\n', encoding='utf-8')

    target = SymbolRecord(
        file_path='support_app/api/controllers.py',
        module_name='support_app.api.controllers',
        name='TicketController',
        qualname='support_app.api.controllers.TicketController',
        kind='class',
        parent_qualname='support_app.api.controllers',
        start_line=1,
        end_line=2,
        source_code='class TicketController:\n    pass\n',
    )
    related = RelatedSymbolContext(
        qualname='support_app.services.report_service.build_agent_summary',
        file_path='support_app/services/report_service.py',
        module_name='support_app.services.report_service',
        name='build_agent_summary',
        kind='function',
        parent_qualname=None,
        relation_kind='calls',
        relation_direction='outbound',
        relation_source='index',
        relation_confidence='high',
        role='called_by_class_member',
        origin_qualname='support_app.api.controllers.TicketController.agent_summary_endpoint',
        signature='def build_agent_summary(agent_name: str, tickets: list) -> AgentSummary:',
        source_code='def build_agent_summary(agent_name: str, tickets: list) -> AgentSummary:\n    ...\n',
    )
    context = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[related],
    )

    request = build_generation_request(
        tmp_path,
        ChangeRequest(
            title='Добавить API-функцию',
            description='API должен использовать build_agent_summary.',
            project='demo',
        ),
        target.qualname,
        context,
        _config(),
        operation='insert_after_symbol',
        insert_scope='class_body',
    )

    symbol = request['project_context']['contract_context']['related_symbols'][0]
    assert symbol['qualname'] == related.qualname
    assert symbol['origin_qualname'] == related.origin_qualname
    assert symbol['role'] == 'called_by_class_member'



def test_patch_static_semantics_rejects_contract_call_with_missing_required_args() -> None:
    related = RelatedSymbolContext(
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
        source_code='def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:\n    ...\n',
    )
    original = """from support_app.services.report_service import build_agent_summary


class TicketController:
    pass
"""
    patched = """from support_app.services.report_service import build_agent_summary


class TicketController:
    def ticket_statistics_endpoint(self) -> dict:
        return build_agent_summary()
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить API', description='', project='demo'),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[related],
    )

    assert not block.ok
    issue_codes = [issue.code for issue in block.issues]
    assert 'contract_call_missing_required_positional_args' in issue_codes
    issue = next(issue for issue in block.issues if issue.code == 'contract_call_missing_required_positional_args')
    assert 'build_agent_summary' in issue.message
    assert 'требует минимум 2' in issue.message
    assert 'Для repair' in issue.message


def test_patch_static_semantics_allows_contract_call_with_required_args() -> None:
    related = RelatedSymbolContext(
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
        source_code='def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:\n    ...\n',
    )
    original = """from support_app.services.report_service import build_agent_summary


class TicketController:
    pass
"""
    patched = """from support_app.services.report_service import build_agent_summary


class TicketController:
    def ticket_statistics_endpoint(self, agent_name: str) -> dict:
        tickets = self.service.repository.list_by_agent(agent_name)
        summary = build_agent_summary(agent_name, tickets)
        return summary.__dict__
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить API', description='', project='demo'),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[related],
    )

    assert block.ok
    assert not [issue for issue in block.issues if issue.code.startswith('contract_call_')]
    details = block.details['contract_call_signature_check']
    assert details['checked_calls'][0]['supplied_positional'] == 2


def test_patch_static_semantics_rejects_duplicate_generated_method() -> None:
    original = """class TicketController:
    def existing_endpoint(self) -> dict:
        return {}
"""
    patched = """class TicketController:
    def existing_endpoint(self) -> dict:
        return {}

    def existing_endpoint(self) -> dict:
        return {'duplicate': True}
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(title='Добавить API', description='', project='demo'),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[],
    )

    assert not block.ok
    issue = next(issue for issue in block.issues if issue.code == 'duplicate_symbol_definition')
    assert 'support_app.api.controllers.TicketController.existing_endpoint' in issue.message
    assert 'Для repair' in issue.message


def test_validation_service_rejects_duplicate_symbols_before_reindex(tmp_path: Path) -> None:
    from codecollector.validation.service import ValidationService

    source_file = tmp_path / 'support_app' / 'api' / 'controllers.py'
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        """class TicketController:
    def existing_endpoint(self) -> dict:
        return {}

    def existing_endpoint(self) -> dict:
        return {'duplicate': True}
""",
        encoding='utf-8',
    )

    report = ValidationService().validate_project(tmp_path, changed_files=[source_file])

    assert not report.is_valid
    issue = next(item for item in report.issues if item.check_name == 'duplicate_symbol_definition')
    assert 'support_app.api.controllers.TicketController.existing_endpoint' in issue.message


def test_analysis_json_parser_recovers_trailing_commas() -> None:
    from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService

    service = AnalysisLlmAssistService.__new__(AnalysisLlmAssistService)
    parsed = service._parse_json_object(
        '```json\n{"recommended_target":"module.symbol", "ranked_candidates":[{"candidate_id":"c1",}],}\n```'
    )

    assert parsed['recommended_target'] == 'module.symbol'
    assert parsed['ranked_candidates'][0]['candidate_id'] == 'c1'


def test_analysis_rerank_fallback_extracts_target_from_invalid_json() -> None:
    from json import JSONDecodeError
    from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService

    service = AnalysisLlmAssistService.__new__(AnalysisLlmAssistService)
    service._safe_float = lambda value: float(value or 0.0)
    payload = service._fallback_parse_rerank_response(
        '{"recommended_operation":"insert_after_symbol"\n'
        ' "recommended_candidate_id":"c1"\n'
        ' "target_confidence":0.9}',
        [{'candidate_id': 'c1', 'qualname': 'pkg.Controller'}],
        JSONDecodeError('bad', '{}', 0),
    )

    assert payload['recommended_target'] == 'pkg.Controller'
    assert payload['ranked_candidates'][0]['candidate_id'] == 'c1'
    assert payload['warnings'][0]['code'] == 'analysis_llm_rerank_json_recovered'


def test_generated_test_static_semantics_rejects_mocker_fixture(tmp_path: Path) -> None:
    from codecollector.validation.semantic_checks import validate_generated_test_static_semantics

    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """def test_generated(mocker):
    assert True
""",
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='support_app.api.controllers.TicketController',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['support_app.api.controllers.TicketController.ticket_statistics_endpoint'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_uses_mocker_fixture' for issue in block.issues)
    assert block.details['pytest_mock_usage']


def test_pytest_error_output_detects_generated_test_path() -> None:
    from codecollector.orchestration.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    output = """==================================== ERRORS ====================================
_ ERROR at setup of TestGenerated.test_example _
file /tmp/workspace/tests/test_generated_sample.py, line 10
    def test_example(self, mocker):
E   fixture 'mocker' not found
=========================== short test summary info ============================
ERROR tests/test_generated_sample.py::TestGenerated::test_example
"""

    paths = service._pytest_failed_paths_from_output(output)

    assert 'tests/test_generated_sample.py' in paths


def test_analysis_rerank_prompt_uses_configured_compact_json_and_extra_trimming() -> None:
    from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService

    service = AnalysisLlmAssistService.__new__(AnalysisLlmAssistService)
    service.system_prompt = ''
    service.rerank_template = 'Payload:\n$payload_json'
    service.config = SimpleNamespace(
        analysis_llm_rerank_max_prompt_chars=900,
        analysis_llm_max_prompt_chars=900,
        analysis_llm_rerank_soft_overflow_ratio=1.0,
        analysis_llm_rerank_json_indent=0,
        analysis_llm_rerank_drop_operation_definitions_on_overflow=True,
        analysis_llm_rerank_candidate_drop_fields=['module_docstring', 'knowledge_title', 'requirements'],
        analysis_llm_rerank_candidate_keep_fields=[
            'candidate_id',
            'qualname',
            'kind',
            'file_path',
            'docstring',
            'search_score',
        ],
        analysis_llm_rerank_emergency_min_candidate_cards=3,
        analysis_min_candidate_cards=4,
        analysis_candidate_source_min_chars=40,
        analysis_candidate_related_test_min_chars=20,
        analysis_candidate_min_siblings=0,
    )

    payload = {
        'request': {'titles': ['title'], 'descriptions': ['description'], 'constraints': []},
        'operation_definitions': [{'name': 'insert_after_symbol', 'meaning': 'long text' * 10}],
        'candidate_cards': [
            {
                'candidate_id': f'c{index}',
                'qualname': f'pkg.mod.Symbol{index}',
                'kind': 'method',
                'file_path': 'pkg/mod.py',
                'module_docstring': 'module docs' * 20,
                'knowledge_title': 'knowledge title',
                'requirements': ['REQ-1'],
                'source_excerpt': 'source ' * 80,
                'siblings': [{'qualname': 'sibling', 'docstring': 'docs'}],
                'search_reasons': ['reason'] * 5,
                'related_tests': [{'source_excerpt': 'test ' * 80}],
                'inbound_relations': [{'relation_kind': 'calls'}],
                'outbound_relations': [{'relation_kind': 'calls'}],
                'docstring': 'candidate docs',
                'search_score': 1.0,
            }
            for index in range(6)
        ],
    }

    prompt, budget = service._build_limited_rerank_prompt(payload)

    assert len(prompt) <= 900
    assert '\n  "candidate_cards"' not in prompt
    assert 'operation_definitions:removed' in budget['trim_steps']
    assert any(step.startswith('candidate_cards') for step in budget['trim_steps'])


def test_analysis_json_compact_output_is_configurable() -> None:
    from codecollector.analysis.llm_assist_service import AnalysisLlmAssistService

    service = AnalysisLlmAssistService.__new__(AnalysisLlmAssistService)
    compact = service._json({'a': 1, 'b': [2, 3]}, indent=0)
    pretty = service._json({'a': 1, 'b': [2, 3]}, indent=2)

    assert compact == '{"a":1,"b":[2,3]}'
    assert '\n' in pretty


def test_generated_test_static_semantics_rejects_unresolved_project_name(tmp_path: Path) -> None:
    from codecollector.validation.semantic_checks import validate_generated_test_static_semantics

    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """
from support_app.api.controllers import TicketController


def test_generated() -> None:
    ticket = Ticket(ticket_id='T-1', title='Bug', description='Demo')
    assert ticket.ticket_id == 'T-1'
""".strip(),
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='support_app.api.controllers.TicketController',
        requested_operation='insert_after_symbol',
        generated_symbol_names=['support_app.api.controllers.TicketController.new_method'],
    )

    assert not block.ok
    assert any(issue.code == 'generated_test_uses_unresolved_name' for issue in block.issues)
    assert 'Ticket' in block.details['unresolved_names']


def test_patch_static_semantics_rejects_unrequested_literal_contract_arg() -> None:
    from codecollector.domain.models import ChangeRequest
    from codecollector.validation.semantic_checks import validate_patch_static_semantics

    original = """
from support_app.services.report_service import build_agent_summary


class TicketController:
    def agent_summary_endpoint(self, agent_name: str) -> dict:
        tickets = self.service.repository.list_by_agent(agent_name)
        summary = build_agent_summary(agent_name, tickets)
        return summary.__dict__
""".strip()

    patched = original + """

    def ticket_statistics_endpoint(self) -> dict:
        tickets = self.service.repository.list_all()
        summary = build_agent_summary('all', tickets)
        return summary.__dict__
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Добавь API-функцию для получения краткой статистики по тикетам.',
            description='Функция должна использовать существующую сервисную функцию, а не дублировать логику.',
            project='sample',
        ),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[
            {
                'qualname': 'support_app.services.report_service.build_agent_summary',
                'name': 'build_agent_summary',
                'kind': 'function',
                'signature': 'def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:',
            }
        ],
        import_changes=[],
    )

    assert not block.ok
    assert any(issue.code == 'contract_call_uses_unrequested_literal_arg' for issue in block.issues)


def test_patch_static_semantics_checks_contract_calls_only_in_new_symbol() -> None:
    from codecollector.domain.models import ChangeRequest
    from codecollector.validation.semantic_checks import validate_patch_static_semantics

    original = """
from support_app.services.report_service import build_agent_summary


class TicketController:
    def create_ticket_endpoint(self, payload: dict):
        priority = payload.get('priority', 'medium')
        return priority

    def agent_summary_endpoint(self, agent_name: str) -> dict:
        tickets = self.service.repository.list_by_agent(agent_name)
        summary = build_agent_summary(agent_name, tickets)
        return summary.__dict__
""".strip()

    patched = original + """

    def ticket_statistics_endpoint(self, agent_name: str) -> dict:
        tickets = self.service.repository.list_by_agent(agent_name)
        summary = build_agent_summary(agent_name, tickets)
        return summary.__dict__
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Добавь API-функцию для получения краткой статистики по тикетам.',
            description='Функция должна использовать существующую сервисную функцию, а не дублировать логику.',
            project='sample',
        ),
        target_qualname='support_app.api.controllers.TicketController',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['support_app/api/controllers.py'],
        target_file='support_app/api/controllers.py',
        insert_scope='class_body',
        parent_qualname='support_app.api.controllers.TicketController',
        related_symbols=[
            {
                'qualname': 'support_app.services.report_service.build_agent_summary',
                'parent_qualname': 'support_app.services.report_service',
                'name': 'build_agent_summary',
                'kind': 'function',
                'signature': 'def build_agent_summary(agent_name: str, tickets: list[Ticket]) -> AgentSummary:',
            },
            {
                'qualname': 'support_app.storage.ticket_repository.TicketRepository.get',
                'parent_qualname': 'support_app.storage.ticket_repository.TicketRepository',
                'name': 'get',
                'kind': 'method',
                'signature': 'def get(self, ticket_id: str) -> Ticket | None:',
            },
        ],
        import_changes=[],
    )

    assert block.ok, [issue.code for issue in block.issues]
    checked_calls = block.details['contract_call_signature_check']['checked_calls']
    assert any(item['call'] == 'build_agent_summary' for item in checked_calls)
    assert not any(item['call'] == 'payload.get' for item in checked_calls)


def test_patch_static_semantics_skips_unrelated_short_method_receiver() -> None:
    from codecollector.domain.models import ChangeRequest
    from codecollector.validation.semantic_checks import validate_patch_static_semantics

    original = """
class Controller:
    pass
""".strip()

    patched = original + """

def new_function(payload: dict) -> str:
    return payload.get('priority', 'medium')
"""

    block = validate_patch_static_semantics(
        requested_operation='insert_after_symbol',
        change_request=ChangeRequest(
            title='Добавь функцию.',
            description='Добавь функцию.',
            project='sample',
        ),
        target_qualname='pkg.Controller',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['pkg/mod.py'],
        target_file='pkg/mod.py',
        insert_scope='module_body',
        parent_qualname=None,
        related_symbols=[
            {
                'qualname': 'support_app.storage.ticket_repository.TicketRepository.get',
                'parent_qualname': 'support_app.storage.ticket_repository.TicketRepository',
                'name': 'get',
                'kind': 'method',
                'signature': 'def get(self, ticket_id: str) -> Ticket | None:',
            },
        ],
        import_changes=[],
    )

    assert block.ok, [issue.code for issue in block.issues]
    skipped_calls = block.details['contract_call_signature_check']['skipped_calls']
    assert any(item['call'] == 'payload.get' for item in skipped_calls)



def test_pytest_error_output_detects_generated_test_path_with_quotes() -> None:
    from codecollector.orchestration.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)

    output = '  File "/tmp/workspace/tests/test_generated_generate_test_auto_save.py", line 8 in test_auto_save_noop_when_not_modified'

    assert service._pytest_failed_paths_from_output(output) == {
        'tests/test_generated_generate_test_auto_save.py'
    }


def test_fatal_traceback_output_detects_generated_test_path() -> None:
    from codecollector.orchestration.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)

    output = '''Fatal Python error: Aborted

Current thread 0x00000000 (most recent call first):
  File "/tmp/workspace/editor/editor_window.py", line 99 in __init__
  File "/tmp/workspace/tests/test_generated_generate_test_auto_save.py", line 8 in test_auto_save_noop_when_not_modified
'''

    assert service._pytest_failed_paths_from_output(output) == {
        'tests/test_generated_generate_test_auto_save.py'
    }
