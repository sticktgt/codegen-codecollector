from pathlib import Path

import yaml

from codecollector.config import load_config
from codecollector.domain.models import SymbolRecord
from codecollector.onboarding.knowledge_builder import KnowledgeBuilder
from codecollector.onboarding.knowledge_enrichment_service import KnowledgeEnrichmentService


def _symbols() -> list[SymbolRecord]:
    return [
        SymbolRecord(
            file_path='game/board.py',
            module_name='game.board',
            name='board',
            qualname='game.board',
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=10,
        ),
        SymbolRecord(
            file_path='game/board.py',
            module_name='game.board',
            name='Board',
            qualname='game.board.Board',
            kind='class',
            parent_qualname='game.board',
            start_line=3,
            end_line=10,
        ),
        SymbolRecord(
            file_path='ui/tk_ui.py',
            module_name='ui.tk_ui',
            name='tk_ui',
            qualname='ui.tk_ui',
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=10,
        ),
        SymbolRecord(
            file_path='ui/tk_ui.py',
            module_name='ui.tk_ui',
            name='TkUI',
            qualname='ui.tk_ui.TkUI',
            kind='class',
            parent_qualname='ui.tk_ui',
            start_line=3,
            end_line=10,
        ),
        SymbolRecord(
            file_path='ui/tk_ui.py',
            module_name='ui.tk_ui',
            name='on_button_click',
            qualname='ui.tk_ui.TkUI.on_button_click',
            kind='method',
            parent_qualname='ui.tk_ui.TkUI',
            start_line=5,
            end_line=8,
        ),
    ]


def test_knowledge_builder_applies_architecture_enrichment_over_base_values(tmp_path: Path) -> None:
    config = load_config()
    project_root = tmp_path / 'src'
    project_root.mkdir()

    enrichment = {
        'project': {
            'title': 'Крестики-нолики',
            'description': 'Игра с MVC-архитектурой.',
        },
        'modules': {
            'game.board': {
                'title': 'Игровое поле',
                'description': 'Хранит состояние поля 3x3.',
                'layer': 'model',
                'keywords': ['поле', 'model'],
            },
            'missing.module': {'title': 'Не должен попасть в knowledge'},
        },
        'symbols': {
            'game.board.Board': {
                'title': 'Board',
                'description': 'Модель игрового поля.',
                'keywords': ['board'],
            },
            'missing.Symbol': {'title': 'Не должен попасть в knowledge'},
        },
        'architecture': {
            'style': 'MVC',
            'patterns': ['MVC'],
            'layers': {
                'model': ['game.board'],
                'view': ['ui.tk_ui', 'ui.missing'],
            },
            'unmatched_mentions': [{'name': 'ui_interface.py', 'reason': 'Нет в индексе'}],
        },
    }

    report = KnowledgeBuilder(project_root, config).rebuild(_symbols(), enrichment=enrichment)
    payload = yaml.safe_load(Path(report['knowledge_path']).read_text(encoding='utf-8'))

    assert payload['project']['title'] == 'Крестики-нолики'
    assert payload['modules']['game.board']['title'] == 'Игровое поле'
    assert payload['modules']['game.board']['layer'] == 'model'
    assert 'missing.module' not in payload['modules']
    assert payload['symbols']['game.board.Board']['description'] == 'Модель игрового поля.'
    assert 'missing.Symbol' not in payload['symbols']
    assert payload['architecture']['style'] == 'MVC'
    assert payload['architecture']['layers']['view'] == ['ui.tk_ui']
    assert 'unmatched_mentions' not in payload['architecture']
    assert 'unresolved_steps' not in payload['architecture']
    assert any('missing.module' in warning for warning in report['enrichment_warnings'])
    assert any('ui.missing' in warning for warning in report['enrichment_warnings'])



def test_knowledge_enrichment_reports_unresolved_flow_step_references_without_changing_flow_payload() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    normalized = service._normalize_llm_payload(
        {
            'architecture': {
                'flows': [
                    {
                        'name': 'Обработка хода',
                        'steps': [
                            'GameController.make_move получает ход от TkUI.on_button_click.',
                            'GameController.start_new_game начинает новую партию.',
                            'self.controller обновляет состояние без проверки как project symbol.',
                        ],
                    }
                ]
            }
        },
        _symbols()
        + [
            SymbolRecord(
                file_path='game/game_controller.py',
                module_name='game.game_controller',
                name='game_controller',
                qualname='game.game_controller',
                kind='module',
                parent_qualname=None,
                start_line=1,
                end_line=20,
            ),
            SymbolRecord(
                file_path='game/game_controller.py',
                module_name='game.game_controller',
                name='GameController',
                qualname='game.game_controller.GameController',
                kind='class',
                parent_qualname='game.game_controller',
                start_line=3,
                end_line=20,
            ),
            SymbolRecord(
                file_path='game/game_controller.py',
                module_name='game.game_controller',
                name='make_move',
                qualname='game.game_controller.GameController.make_move',
                kind='method',
                parent_qualname='game.game_controller.GameController',
                start_line=8,
                end_line=12,
            ),
            SymbolRecord(
                file_path='ui/tk_ui.py',
                module_name='ui.tk_ui',
                name='on_button_click',
                qualname='ui.tk_ui.TkUI.on_button_click',
                kind='method',
                parent_qualname='ui.tk_ui.TkUI',
                start_line=5,
                end_line=8,
            ),
        ],
    )

    flow = normalized['architecture']['flows'][0]
    assert flow['steps'][1] == 'GameController.start_new_game начинает новую партию.'
    assert 'unresolved_steps' not in flow
    assert 'unresolved_steps' not in normalized['architecture']

    unmatched = normalized['_unmatched_mentions']
    unresolved_names = {item['name'] for item in unmatched if item.get('kind') == 'flow_step_reference'}
    assert 'GameController.start_new_game' in unresolved_names
    assert 'GameController.make_move' not in unresolved_names
    assert 'TkUI.on_button_click' not in unresolved_names
    assert 'self.controller' not in unresolved_names
    assert any('architecture.flows' in warning for warning in normalized['_warnings'])


def test_knowledge_enrichment_normalizes_only_known_modules_and_symbols() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    normalized = service._normalize_llm_payload(
        {
            'modules': {
                'game/board.py': {'title': 'Игровое поле'},
                'ui_interface.py': {'title': 'Несуществующий интерфейс'},
            },
            'symbols': {
                'game.board.Board': {'description': 'Модель поля'},
                'game.board.Unknown': {'description': 'Неизвестный symbol'},
            },
            'architecture': {
                'layers': {
                    'model': ['board.py'],
                    'view': ['ui_interface.py'],
                }
            },
        },
        _symbols(),
    )

    assert normalized['modules']['game.board']['title'] == 'Игровое поле'
    assert 'ui_interface.py' not in normalized.get('modules', {})
    assert normalized['symbols']['game.board.Board']['description'] == 'Модель поля'
    assert 'game.board.Unknown' not in normalized.get('symbols', {})
    assert normalized['architecture']['layers']['model'] == ['game.board']
    assert 'view' not in normalized['architecture']['layers']
    unmatched_names = {item['name'] for item in normalized['_unmatched_mentions']}
    assert {'ui_interface.py', 'game.board.Unknown'} <= unmatched_names


def test_knowledge_enrichment_prompt_trim_keeps_complete_symbol_list() -> None:
    config = load_config()
    config.onboarding_architecture_enrichment_max_prompt_chars = 4500
    config.onboarding_architecture_enrichment_soft_overflow_ratio = 1.15
    config.onboarding_architecture_enrichment_docstring_chars = 300
    symbols = _symbols()
    for symbol in symbols:
        symbol.docstring = 'подробное описание ' * 80

    service = KnowledgeEnrichmentService(Path.cwd(), config)
    prepared = service._prepare_prompt(
        architect_text='Архитектурное описание. ' * 50,
        project_root=Path('/tmp/example_project/src'),
        symbols=symbols,
    )

    assert 'drop_symbol_docstrings' in prepared.budget['trim_steps']
    assert prepared.prompt_chars <= prepared.budget['soft_max_prompt_chars']
    context_mode = prepared.budget['context_mode']
    assert context_mode['known_files'] == 'complete'
    assert context_mode['known_modules'] == 'complete'
    assert context_mode['known_symbols'] == 'complete'
    assert context_mode['known_symbols_count'] == context_mode['included_symbols_count']


def test_knowledge_enrichment_prompt_fails_when_valid_context_does_not_fit() -> None:
    config = load_config()
    config.onboarding_architecture_enrichment_max_prompt_chars = 1000
    config.onboarding_architecture_enrichment_soft_overflow_ratio = 1.0
    config.onboarding_architecture_doc_max_chars = 12000
    service = KnowledgeEnrichmentService(Path.cwd(), config)

    try:
        service._prepare_prompt(
            architect_text='Архитектурное описание. ' * 1000,
            project_root=Path('/tmp/example_project/src'),
            symbols=_symbols(),
        )
    except ValueError as exc:
        assert 'Prompt enrichment слишком большой' in str(exc)
    else:
        raise AssertionError('Expected ValueError for oversized enrichment prompt')


def test_knowledge_enrichment_parses_json_with_missing_comma_between_object_fields() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    payload = service._parse_json_object(
        '{"project": {"title": "Demo"}\n'
        ' "modules": {"game.board": {"title": "Board"}}}'
    )

    assert payload['project']['title'] == 'Demo'
    assert payload['modules']['game.board']['title'] == 'Board'


def test_onboarding_finds_configured_architecture_doc_name(tmp_path: Path) -> None:
    from codecollector.onboarding.onboarding_service import OnboardingService

    config = load_config()
    config.onboarding_architecture_doc_names = ['ARCHITECTURE.md']
    onboarding_root = tmp_path / 'package'
    onboarding_root.mkdir()
    architecture_path = onboarding_root / 'ARCHITECTURE.md'
    architecture_path.write_text('# Архитектура', encoding='utf-8')

    service = OnboardingService(tmp_path / 'tool', config)

    assert service._find_architecture_doc(onboarding_root) == architecture_path.resolve()


def test_project_service_rejects_duplicate_project_root(tmp_path: Path) -> None:
    from codecollector.projects.project_service import ProjectService

    config = load_config()
    config.state_root_dirname = '.state-test'
    project_root = tmp_path / 'project' / 'src'
    project_root.mkdir(parents=True)
    service = ProjectService(tmp_path / 'tool', config)

    first = service.register_project(project_name='first', project_root=project_root)

    try:
        service.register_project(project_name='second', project_root=project_root)
    except ValueError as exc:
        assert 'already' in str(exc) or 'уже зарегистрирован' in str(exc)
        assert first.project_id in str(exc)
        if hasattr(exc, 'to_dict'):
            assert exc.to_dict()['error_code'] == 'project_root_already_registered'
            assert exc.to_dict()['existing_project_id'] == first.project_id
    else:
        raise AssertionError('Expected duplicate project_root registration to fail')


def test_knowledge_enrichment_trace_is_written_under_tool_runs(tmp_path: Path) -> None:
    config = load_config()
    config.runs_root_dirname = '.runs-test'
    service = KnowledgeEnrichmentService(tmp_path / 'tool', config)
    project_root = tmp_path / 'project' / 'src'
    project_root.mkdir(parents=True)
    architect_path = tmp_path / 'project' / 'ARCHITECT.md'
    architect_path.write_text('# Архитектура', encoding='utf-8')

    trace_path = service._write_trace_file(
        project_root=project_root,
        architect_path=architect_path,
        content='{',
        raw={'done_reason': 'length', 'prompt_eval_count': 10, 'eval_count': 20},
        error='truncated',
    )

    path = Path(trace_path)
    assert path.exists()
    assert path.is_relative_to((tmp_path / 'tool' / '.runs-test').resolve())
    assert '.codecollector' not in path.parts


def test_cli_error_payload_is_structured_json() -> None:
    from argparse import Namespace

    from codecollector.api.cli import _error_payload
    from codecollector.projects.project_service import ProjectRootAlreadyRegisteredError
    from codecollector.projects.project_registry import RegisteredProject

    project = RegisteredProject.from_dict({
        'project_id': 'proj-test',
        'project_name': 'demo',
        'project_root': '/tmp/demo/src',
        'languages': ['python'],
        'verification_commands': [],
        'index_excludes': [],
        'reference_library_ids': [],
        'reference_library_paths': [],
        'status': 'ready',
        'created_at': '',
        'updated_at': '',
        'knowledge_path': '',
        'knowledge_updated_at': '',
    })
    exc = ProjectRootAlreadyRegisteredError(project_root=Path('/tmp/demo/src'), existing_project=project)

    payload = _error_payload(exc, Namespace(command='projects', projects_command='onboard'))

    assert payload['status'] == 'failed'
    assert payload['command'] == 'projects'
    assert payload['subcommand'] == 'onboard'
    assert payload['details']['error_code'] == 'project_root_already_registered'
    assert payload['details']['existing_project_id'] == 'proj-test'
    assert 'error_code' not in payload
    assert 'existing_project_id' not in payload
    assert 'traceback' not in payload


def test_knowledge_enrichment_treats_abort_done_reason_as_incomplete_response() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    message = service._incomplete_done_reason('abort')

    assert 'прервали генерацию' in message
    assert service._incomplete_done_reason('stop') == ''


def test_project_cleanup_warns_when_graph_and_vector_deleted_counts_differ(tmp_path: Path) -> None:
    from codecollector.projects.project_cleanup_service import ProjectCleanupService

    service = ProjectCleanupService(tmp_path / 'tool', load_config())
    result = {
        'project_root': '/tmp/example/src',
        'graph_deleted': {'cc_search_documents': 44},
        'vector_deleted': 43,
        'warnings': [],
    }

    service._append_index_cleanup_mismatch_warning(result)

    assert result['warnings']
    assert 'cc_search_documents=44' in result['warnings'][0]
    assert 'vector_deleted=43' in result['warnings'][0]


def test_knowledge_enrichment_prompt_never_omits_symbols_for_budget() -> None:
    config = load_config()
    config.onboarding_architecture_enrichment_max_prompt_chars = 22000
    config.onboarding_architecture_enrichment_soft_overflow_ratio = 1.0
    config.onboarding_architecture_doc_max_chars = 18000
    config.onboarding_architecture_doc_min_chars = 3000
    config.onboarding_architecture_doc_min_ratio = 0.25
    symbols = []
    for index in range(60):
        module = f'pkg.module_{index}'
        symbols.append(SymbolRecord(
            file_path=f'pkg/module_{index}.py',
            module_name=module,
            name=f'module_{index}',
            qualname=module,
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=20,
        ))
        symbols.append(SymbolRecord(
            file_path=f'pkg/module_{index}.py',
            module_name=module,
            name=f'Service{index}',
            qualname=f'{module}.Service{index}',
            kind='class',
            parent_qualname=module,
            start_line=3,
            end_line=20,
        ))

    service = KnowledgeEnrichmentService(Path.cwd(), config)
    prepared = service._prepare_prompt(
        architect_text='Архитектурное описание проекта. ' * 1000,
        project_root=Path('/tmp/example_project/src'),
        symbols=symbols,
    )

    context_mode = prepared.budget['context_mode']
    assert context_mode['known_symbols'] == 'complete'
    assert context_mode['known_symbols_count'] == context_mode['included_symbols_count']
    assert context_mode['included_symbols_count'] == 60
    assert context_mode['symbol_fields'] == 'minimal_qualname_kind_module'
    assert not any('omit_symbols' in step for step in prepared.budget['trim_steps'])


def test_knowledge_enrichment_compact_symbol_fields_are_minimal() -> None:
    config = load_config()
    service = KnowledgeEnrichmentService(Path.cwd(), config)
    payload = service._build_payload(
        architect_text='Архитектура',
        project_root=Path('/tmp/example_project/src'),
        symbols=_symbols(),
        include_module_docstrings=False,
        include_symbol_docstrings=False,
        symbols_mode='minimal',
        trim_steps=['compact_symbol_fields'],
    )

    symbol_entry = next(item for item in payload['known_symbols'] if item['qualname'] == 'game.board.Board')
    assert set(symbol_entry) == {'qualname', 'kind', 'module'}
    assert symbol_entry['module'] == 'game.board'
    assert payload['context_completeness']['known_symbols'] == 'complete'


def test_knowledge_enrichment_ignores_llm_reported_unmatched_mentions() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    normalized = service._normalize_llm_payload(
        {
            'unmatched_mentions': [
                {'name': 'app.legacy', 'reason': 'Модель решила, что модуль устарел'},
            ]
        },
        _symbols(),
    )

    assert normalized['_unmatched_mentions'] == []


def test_knowledge_enrichment_reports_ambiguous_short_flow_reference() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    base = _symbols()
    symbols = base + [
        SymbolRecord(
            file_path='app/note_manager.py',
            module_name='app.note_manager',
            name='note_manager',
            qualname='app.note_manager',
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=20,
        ),
        SymbolRecord(
            file_path='app/note_manager.py',
            module_name='app.note_manager',
            name='NoteManager',
            qualname='app.note_manager.NoteManager',
            kind='class',
            parent_qualname='app.note_manager',
            start_line=3,
            end_line=20,
        ),
        SymbolRecord(
            file_path='app/note_manager.py',
            module_name='app.note_manager',
            name='create_note',
            qualname='app.note_manager.NoteManager.create_note',
            kind='method',
            parent_qualname='app.note_manager.NoteManager',
            start_line=8,
            end_line=12,
        ),
        SymbolRecord(
            file_path='note/note_manager.py',
            module_name='note.note_manager',
            name='note_manager',
            qualname='note.note_manager',
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=20,
        ),
        SymbolRecord(
            file_path='note/note_manager.py',
            module_name='note.note_manager',
            name='NoteManager',
            qualname='note.note_manager.NoteManager',
            kind='class',
            parent_qualname='note.note_manager',
            start_line=3,
            end_line=20,
        ),
        SymbolRecord(
            file_path='note/note_manager.py',
            module_name='note.note_manager',
            name='create_note',
            qualname='note.note_manager.NoteManager.create_note',
            kind='method',
            parent_qualname='note.note_manager.NoteManager',
            start_line=8,
            end_line=12,
        ),
    ]

    normalized = service._normalize_llm_payload(
        {'architecture': {'flows': [{'name': 'Создание', 'steps': ['NoteManager.create_note()']}] }},
        symbols,
    )

    unmatched = normalized['_unmatched_mentions']
    assert len(unmatched) == 1
    assert unmatched[0]['name'] == 'NoteManager.create_note'
    assert unmatched[0]['reason'] == 'ambiguous_reference'
    assert set(unmatched[0]['suggested_matches']) == {
        'app.note_manager.NoteManager.create_note',
        'note.note_manager.NoteManager.create_note',
    }


def test_knowledge_enrichment_reports_similar_flow_reference_when_exact_missing() -> None:
    service = KnowledgeEnrichmentService(Path.cwd(), load_config())
    symbols = _symbols() + [
        SymbolRecord(
            file_path='search/search_engine.py',
            module_name='search.search_engine',
            name='search_engine',
            qualname='search.search_engine',
            kind='module',
            parent_qualname=None,
            start_line=1,
            end_line=20,
        ),
        SymbolRecord(
            file_path='search/search_engine.py',
            module_name='search.search_engine',
            name='search_notes',
            qualname='search.search_engine.search_notes',
            kind='function',
            parent_qualname='search.search_engine',
            start_line=3,
            end_line=8,
        ),
    ]

    normalized = service._normalize_llm_payload(
        {'architecture': {'flows': [{'name': 'Поиск', 'steps': ['SearchEngine.search_notes()']}] }},
        symbols,
    )

    unmatched = normalized['_unmatched_mentions']
    assert len(unmatched) == 1
    assert unmatched[0]['name'] == 'SearchEngine.search_notes'
    assert unmatched[0]['reason'] == 'reference_not_found_exactly_but_similar_symbols_exist'
    assert 'search.search_engine.search_notes' in unmatched[0]['suggested_matches']
