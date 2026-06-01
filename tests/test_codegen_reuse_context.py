from pathlib import Path

from codecollector.config import load_config
from codecollector.domain.models import ChangeRequest, ContextPack, RelatedSymbolContext, SymbolRecord
from codecollector.external_codegen.adapter import build_generation_request


def test_generation_request_includes_same_class_methods_and_analyze_reuse_hint(tmp_path: Path) -> None:
    source = '''from pathlib import Path

class NoteStorage:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def find_note_file_by_id(self, note_id: str) -> Path:
        """Find note file by id."""
        return self.base_dir / f"{note_id}.note"

    def load(self, note_id: str):
        """Load note by id."""
        raise NotImplementedError
'''
    module_dir = tmp_path / 'note'
    module_dir.mkdir()
    (module_dir / 'note_storage.py').write_text(source, encoding='utf-8')

    target = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='load',
        qualname='note.note_storage.NoteStorage.load',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=9,
        end_line=11,
        docstring='Load note by id.',
        source_code='    def load(self, note_id: str):\n        raise NotImplementedError',
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    change_request = ChangeRequest(
        title='Загрузка заметки из файла',
        description='Использовать существующую логику поиска файла и восстановить заметку.',
        project='demo',
        context_hints={
            'reuse_existing_logic': {
                'mode': 'recommended',
                'confidence': 0.82,
                'reason': 'Use existing helper for file lookup.',
                'contracts': [
                    {
                        'qualname': 'note.note_storage.NoteStorage.find_note_file_by_id',
                        'role': 'same_class_helper',
                        'reason': 'Finds a note file by id.',
                    }
                ],
            }
        },
    )

    request = build_generation_request(
        tmp_path,
        change_request,
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
        insert_scope='class_body',
    )

    same_class_methods = request['project_context']['same_class_methods']
    assert same_class_methods
    helper = next(item for item in same_class_methods if item['qualname'] == 'note.note_storage.NoteStorage.find_note_file_by_id')
    assert 'reuse_recommended' not in helper
    assert 'find_note_file_by_id' in helper['source_excerpt']

    reuse = request['project_context']['reuse_existing_logic']
    assert reuse['mode'] == 'recommended'
    assert reuse['contracts'][0]['qualname'] == 'note.note_storage.NoteStorage.find_note_file_by_id'


def test_generation_request_includes_same_class_methods_without_auto_reuse_recommendation(tmp_path: Path) -> None:
    source = '''from pathlib import Path

class NoteStorage:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def find_note_file_by_id(self, note_id: str) -> Path:
        """Ищет файл заметки по идентификатору внутри хранилища."""
        return self.base_dir / f"{note_id}.note"

    def generate_filename(self, note) -> str:
        """Генерирует имя файла заметки."""
        return "demo.note"

    def load(self, note_id: str):
        """Загружает заметку по идентификатору."""
        raise NotImplementedError
'''
    module_dir = tmp_path / 'note'
    module_dir.mkdir()
    (module_dir / 'note_storage.py').write_text(source, encoding='utf-8')

    target = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='load',
        qualname='note.note_storage.NoteStorage.load',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=15,
        end_line=17,
        docstring='Загружает заметку по идентификатору.',
        source_code='    def load(self, note_id: str):\n        raise NotImplementedError',
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    change_request = ChangeRequest(
        title='Загрузка заметки из файла',
        description='Метод должен найти файл заметки по идентификатору через существующую логику поиска файла и восстановить заметку.',
        project='demo',
    )

    request = build_generation_request(
        tmp_path,
        change_request,
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
        insert_scope='class_body',
    )

    same_class_methods = request['project_context']['same_class_methods']
    assert any(item['qualname'] == 'note.note_storage.NoteStorage.find_note_file_by_id' for item in same_class_methods)
    assert not any(item.get('reuse_recommended') for item in same_class_methods)


def test_repair_request_preserves_same_class_methods_from_generation_request(tmp_path: Path) -> None:
    from codecollector.external_codegen.adapter import build_repair_request

    target = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='load',
        qualname='note.note_storage.NoteStorage.load',
        kind='method',
        parent_qualname='note.note_storage.NoteStorage',
        start_line=10,
        end_line=12,
        docstring='Load note by id.',
        source_code='    def load(self, note_id: str):\n        raise NotImplementedError',
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    change_request = ChangeRequest(
        title='Загрузка заметки из файла',
        description='Метод должен найти файл заметки через существующую логику поиска файла и восстановить заметку.',
        project='demo',
    )
    previous_generation_request = {
        'project_context': {
            'same_class_methods': [
                {
                    'qualname': 'note.note_storage.NoteStorage.find_note_file_by_id',
                    'name': 'find_note_file_by_id',
                    'kind': 'method',
                    'signature': 'def find_note_file_by_id(self, note_id: str) -> Path:',
                    'docstring': 'Find note file by id.',
                    'source_excerpt': 'def find_note_file_by_id(self, note_id: str) -> Path:\n    ...',
                }
            ],
            'reuse_existing_logic': {
                'mode': 'recommended',
                'confidence': 0.75,
                'reason': 'Use existing helper.',
                'contracts': [
                    {
                        'qualname': 'note.note_storage.NoteStorage.find_note_file_by_id',
                        'role': 'same_class_helper',
                        'reason': 'Finds a note file by id.',
                    }
                ],
            },
            'allowed_api_surface': {},
            'required_contracts': [],
            'required_class_members': [],
            'model_surfaces': [],
        }
    }
    previous_result_payload = {
        'request_id': 'generate-load',
        'code_artifact': {
            'operation': 'replace_symbol',
            'target_qualname': target.qualname,
            'target_file': target.file_path,
            'code': 'def load(self, note_id: str):\n    return self._find_file(note_id)',
        },
    }
    verification_summary = {
        'failure_summary': {'stage': 'verification'},
        'failed_blocks': [],
    }

    request = build_repair_request(
        change_request=change_request,
        target_qualname=target.qualname,
        previous_result_payload=previous_result_payload,
        context_pack=context_pack,
        verification_summary=verification_summary,
        requested_operation='replace_symbol',
        insert_scope='class_body',
        previous_generation_request=previous_generation_request,
    )

    project_context = request['project_context']
    assert project_context['same_class_methods'][0]['qualname'] == 'note.note_storage.NoteStorage.find_note_file_by_id'
    assert project_context['reuse_existing_logic']['mode'] == 'recommended'
    assert project_context['reuse_existing_logic']['contracts'][0]['qualname'] == 'note.note_storage.NoteStorage.find_note_file_by_id'


def test_generation_request_adds_explicit_project_contracts_and_result_surface(tmp_path: Path) -> None:
    note_dir = tmp_path / 'note'
    note_dir.mkdir()
    (note_dir / 'note_search.py').write_text('''
class SearchResult:
    def __init__(self, note, preview: str, match_positions: list[int]) -> None:
        self.note = note
        self.preview = preview
        self.match_positions = match_positions


def build_context_fragment(text: str, query: str) -> str:
    return text[:10]


def find_match_positions(text: str, query: str) -> list[int]:
    return []
''', encoding='utf-8')
    storage_source = '''
class NoteStorage:
    def search_by_content(self, query: str):
        return []
'''
    (note_dir / 'note_storage.py').write_text(storage_source, encoding='utf-8')

    target = SymbolRecord(
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='NoteStorage',
        qualname='note.note_storage.NoteStorage',
        kind='class',
        parent_qualname=None,
        start_line=2,
        end_line=4,
        docstring='',
        source_code=storage_source,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    change_request = ChangeRequest(
        title='Добавить результаты поиска',
        description='Добавить метод, который использует build_context_fragment и find_match_positions и возвращает SearchResult.',
        constraints=[
            'Для контекстного фрагмента использовать build_context_fragment.',
            'Для позиций совпадений использовать find_match_positions.',
            'Создавать SearchResult только с видимыми аргументами note, preview и match_positions.',
        ],
        project='demo',
        context_hints={
            'reuse_existing_logic': {
                'mode': 'required',
                'confidence': 0.95,
                'reason': 'Нужно переиспользовать существующие helper-функции.',
                'contracts': [
                    {'qualname': 'note.note_search.build_context_fragment', 'role': 'project_contract'},
                    {'qualname': 'note.note_search.find_match_positions', 'role': 'project_contract'},
                ],
            }
        },
    )

    request = build_generation_request(
        tmp_path,
        change_request,
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='insert_after_symbol',
        insert_scope='class_body',
    )

    related_qualnames = {item['qualname'] for item in request['project_context']['related_symbols']}
    assert 'note.note_search.build_context_fragment' in related_qualnames
    assert 'note.note_search.find_match_positions' in related_qualnames
    assert 'note.note_search.SearchResult' in related_qualnames

    surfaces = {item['qualname']: item for item in request['project_context']['model_surfaces']}
    assert surfaces['note.note_search.SearchResult']['constructor_fields'] == ['note', 'preview', 'match_positions']


def test_generation_request_includes_existing_consumer_import_context_for_constants(tmp_path: Path) -> None:
    constants_source = '''class FileExtensions:
    NOTE: str = ".note"

class SearchModes:
    DATE: str = "date"
    CONTENT: str = "content"
'''
    constants_dir = tmp_path / 'common'
    constants_dir.mkdir()
    (constants_dir / 'constants.py').write_text(constants_source, encoding='utf-8')

    editor_dir = tmp_path / 'editor'
    editor_dir.mkdir()
    (editor_dir / 'editor_window.py').write_text(
        'from common.constants import APP_NAME, AUTO_SAVE_INTERVAL_MS, NOTES_DIR\n\n'
        'class EditorWindow:\n'
        '    def __init__(self):\n'
        '        self.title = APP_NAME\n'
        '        self.interval = AUTO_SAVE_INTERVAL_MS\n'
        '        self.notes_dir = NOTES_DIR\n',
        encoding='utf-8',
    )

    target = SymbolRecord(
        file_path='common/constants.py',
        module_name='common.constants',
        name='SearchModes',
        qualname='common.constants.SearchModes',
        kind='class',
        parent_qualname='common.constants',
        start_line=4,
        end_line=6,
        docstring='',
        source_code='class SearchModes:\n    DATE: str = "date"\n    CONTENT: str = "content"',
    )
    module = SymbolRecord(
        file_path='common/constants.py',
        module_name='common.constants',
        name='constants',
        qualname='common.constants',
        kind='module',
        parent_qualname=None,
        start_line=1,
        end_line=6,
        docstring='',
        source_code=constants_source,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[module],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[],
    )
    change_request = ChangeRequest(
        title='Добавить общие значения, необходимые для запуска главного окна',
        description=(
            'В модуле общих констант нужно добавить значения, которые уже ожидает существующий код. '
            'После доработки существующий код должен получить эти значения из common.constants без изменения редактора.'
        ),
        project='demo',
        constraints=[
            'Имена добавляемых значений должны соответствовать тому, как они уже используются в существующем коде.',
            'Не добавлять новый способ доступа к этим значениям, если существующий код уже обращается к ним напрямую из модуля общих констант.',
        ],
    )

    request = build_generation_request(
        tmp_path,
        change_request,
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='insert_after_symbol',
        insert_scope='module_body',
    )

    assert request['target']['expected_new_symbol_kind'] == 'module_constants'
    consumer_context = request['project_context']['consumer_context']
    assert consumer_context
    rendered = consumer_context[0]['source_excerpt']
    assert 'from common.constants import APP_NAME, AUTO_SAVE_INTERVAL_MS, NOTES_DIR' in rendered
    assert consumer_context[0]['required_export_names'] == [
        'APP_NAME',
        'AUTO_SAVE_INTERVAL_MS',
        'NOTES_DIR',
    ]
    related = request['project_context']['contract_context']['related_symbols']
    assert any(item.get('role') == 'existing_consumer_context' for item in related)


def test_generation_request_builds_dependency_api_from_related_class_methods(tmp_path: Path) -> None:
    editor_dir = tmp_path / 'editor'
    note_dir = tmp_path / 'note'
    editor_dir.mkdir()
    note_dir.mkdir()

    editor_source = '''from note.note_storage import NoteStorage

class EditorWindow:
    def __init__(self):
        self.storage: NoteStorage = NoteStorage(base_dir="notes")
        self.note = None

    def save_note(self) -> None:
        raise NotImplementedError
'''
    storage_source = '''class NoteStorage:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir

    def save(self, note):
        return "saved"
'''
    (editor_dir / 'editor_window.py').write_text(editor_source, encoding='utf-8')
    (note_dir / 'note_storage.py').write_text(storage_source, encoding='utf-8')

    target = SymbolRecord(
        file_path='editor/editor_window.py',
        module_name='editor.editor_window',
        name='save_note',
        qualname='editor.editor_window.EditorWindow.save_note',
        kind='method',
        parent_qualname='editor.editor_window.EditorWindow',
        start_line=8,
        end_line=9,
        docstring='',
        source_code='    def save_note(self) -> None:\n        raise NotImplementedError',
    )
    parent = SymbolRecord(
        file_path='editor/editor_window.py',
        module_name='editor.editor_window',
        name='EditorWindow',
        qualname='editor.editor_window.EditorWindow',
        kind='class',
        parent_qualname=None,
        start_line=3,
        end_line=9,
        docstring='',
        source_code=editor_source,
    )
    storage = RelatedSymbolContext(
        qualname='note.note_storage.NoteStorage',
        file_path='note/note_storage.py',
        module_name='note.note_storage',
        name='NoteStorage',
        kind='class',
        parent_qualname=None,
        relation_kind='imports',
        relation_direction='outbound',
        relation_source='index',
        relation_confidence='high',
        role='imported_contract',
        origin_qualname='editor.editor_window',
        signature='class NoteStorage:',
        docstring='',
        source_code=storage_source,
    )
    context_pack = ContextPack(
        target=target,
        neighbors=[parent],
        inbound_relations=[],
        outbound_relations=[],
        related_tests=[],
        related_symbols=[storage],
    )

    request = build_generation_request(
        tmp_path,
        ChangeRequest(
            title='Реализовать сохранение заметки',
            description='Использовать существующее хранилище заметок для сохранения текущей заметки.',
            project='demo',
        ),
        target.qualname,
        context_pack,
        load_config(),
        mode='generate',
        operation='replace_symbol',
    )

    dependencies = {
        item['access_path']: item
        for item in request['project_context']['allowed_api_surface']['dependencies']
    }
    assert 'self.storage' in dependencies
    assert dependencies['self.storage']['type_name'] == 'NoteStorage'
    assert 'save' in {item['name'] for item in dependencies['self.storage']['allowed_methods']}
