from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parents[2]
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))

import pandas as pd
import streamlit as st

from codecollector.config import load_config
from codecollector.domain.models import ChangeRequest, PatchArtifact
from codecollector.logger import configure_logging, get_logger
from codecollector.orchestration.services import ProjectServices

CONFIG = load_config()
configure_logging(CONFIG.log_level, CONFIG.log_format)
LOGGER = get_logger(__name__)
DEFAULT_PROJECT = CONFIG.ui_default_demo_project_path
ARTIFACTS_ROOT = ROOT_PATH / 'demo_artifacts'
CATEGORY_BADGES = {
    'высокая': '🟢 высокая',
    'средняя': '🟡 средняя',
    'низкая': '⚪ низкая',
}


def _candidate_rows(candidates: list[dict]) -> pd.DataFrame:
    rows = []
    for item in candidates:
        rows.append({
            'qualname': item['qualname'],
            'kind': item['kind'],
            'file_path': item['file_path'],
            'confidence': item.get('confidence', 0.0),
            'relevance': CATEGORY_BADGES.get(item.get('relevance_category', 'низкая'), item.get('relevance_category', 'низкая')),
            'knowledge_title': item.get('knowledge_title', ''),
            'requirements': ', '.join(item.get('requirements', [])),
        })
    return pd.DataFrame(rows)


def _render_candidates(candidates: list[dict]) -> None:
    st.subheader('Shortlist кандидатов')
    st.dataframe(_candidate_rows(candidates), width='stretch', hide_index=True)
    with st.expander('Почему именно эти кандидаты'):
        for item in candidates:
            st.markdown(f"**{item['qualname']}**")
            st.caption(
                f"Категория: {CATEGORY_BADGES.get(item.get('relevance_category', 'низкая'), item.get('relevance_category', 'низкая'))} "
                f"· score={item['score']} · confidence={item.get('confidence', 0.0):.2f}"
            )
            if item.get('knowledge_title'):
                st.write(f"Knowledge title: {item['knowledge_title']}")
            if item.get('requirements'):
                st.write(f"Requirements: {', '.join(item['requirements'])}")
            for reason in item.get('reasons', []):
                st.write(f"- {reason}")
            if item.get('docstring'):
                st.code(item['docstring'])


def _render_context(context_pack: dict) -> None:
    st.subheader('Context pack')
    target = context_pack['target']
    left, right = st.columns([1, 1])
    with left:
        st.markdown(f"**Target:** `{target['qualname']}`")
        st.caption(f"Файл: {target['file_path']} · строки {target['start_line']}-{target['end_line']}")
        if context_pack.get('knowledge_title'):
            st.write(f"**Knowledge title:** {context_pack['knowledge_title']}")
        if context_pack.get('knowledge_description'):
            st.write(context_pack['knowledge_description'])
        st.code(target['source_code'], language='python')
    with right:
        st.markdown('**Knowledge**')
        if context_pack.get('knowledge_title'):
            st.write(f"Название: {context_pack['knowledge_title']}")
        if context_pack.get('knowledge_description'):
            st.write(context_pack['knowledge_description'])
        else:
            st.write('Дополнительного knowledge-описания нет.')
        st.markdown('**Связанные требования**')
        if context_pack.get('requirement_titles'):
            for req_id, req_title in zip(context_pack.get('requirement_ids', []), context_pack.get('requirement_titles', [])):
                st.write(f'- `{req_id}` — {req_title}')
        else:
            st.write('Связанных требований пока нет.')
        st.markdown('**Рекомендуемые тесты к прогону**')
        related_tests = context_pack.get('recommended_tests', [])
        if related_tests:
            for test in related_tests:
                st.write(f'- `{test}`')
        else:
            st.write('Рекомендуемые тесты пока не найдены.')
    with st.expander('Соседние сущности'):
        for item in context_pack.get('neighbors', []):
            st.markdown(f"**{item['qualname']}**")
            st.code(item['source_code'], language='python')
    rel_left, rel_right = st.columns(2)
    with rel_left:
        st.markdown('**Входящие связи**')
        for relation in context_pack.get('inbound_relations', []):
            target_label = relation.get('target_qualname') or relation['target_ref']
            confidence = relation.get('relation_confidence', 'medium')
            st.write(f"- `{relation['source_qualname']}` → `{target_label}` ({relation['relation_kind']}, confidence={confidence})")
    with rel_right:
        st.markdown('**Исходящие связи**')
        for relation in context_pack.get('outbound_relations', []):
            target_label = relation.get('target_qualname') or relation['target_ref']
            confidence = relation.get('relation_confidence', 'medium')
            st.write(f"- `{relation['source_qualname']}` → `{target_label}` ({relation['relation_kind']}, confidence={confidence})")


def _render_apply_result(result: dict) -> None:
    st.subheader('Результат apply')
    metric1, metric2, metric3 = st.columns(3)
    metric1.metric('Workspace', Path(result['workspace_path']).name)
    metric2.metric('Структурная валидация', 'OK' if result['validation']['is_valid'] else 'ERROR')
    metric3.metric('Переиндексация', 'Да' if result['reindexed'] else 'Нет')

    impact = result['impact']
    st.markdown('**Impact summary**')
    st.write(f"- Target: `{impact['target_qualname']}`")
    st.write(f"- Измененные файлы: {', '.join(impact.get('changed_files', [])) or '—'}")
    st.write(f"- Символы в измененных файлах: {', '.join(impact.get('symbols_in_changed_files', [])) or '—'}")
    st.write(f"- Вызывающие символы: {', '.join(impact.get('inbound_callers', [])) or '—'}")
    st.write(f"- Рекомендуемые тесты: {', '.join(impact.get('recommended_tests', [])) or '—'}")
    if impact.get('recommended_test_commands'):
        for command in impact['recommended_test_commands']:
            st.code(command, language='bash')
    st.write(f"- Связанные требования: {', '.join(impact.get('linked_requirements', [])) or '—'}")
    if impact.get('notes'):
        for note in impact['notes']:
            st.write(f"- {note}")
    if result['validation']['issues']:
        with st.expander('Validation issues'):
            st.json(result['validation']['issues'])
    with st.expander('Unified diff'):
        st.code(result['diff']['unified_diff'], language='diff')


def _render_pipeline_result(result: dict) -> None:
    st.subheader('Полный dry-run pipeline')
    st.write(f"**Run ID:** `{result['run_id']}`")
    st.write(f"**Run dir:** `{result['run_dir']}`")
    st.markdown('**Change request**')
    st.json(result['change_request'])

    timeline_rows = [
        {
            'step': item['step_name'],
            'status': item['status'],
            'duration_ms': item['duration_ms'],
            'summary': item['summary'],
        }
        for item in result['steps']
    ]
    st.markdown('**Тайминг шагов**')
    st.dataframe(pd.DataFrame(timeline_rows), width='stretch', hide_index=True)

    with st.expander('1. Shortlist'):
        _render_candidates(result['search_candidates'])
    with st.expander('2. Context pack'):
        _render_context(result['context_pack'])
    with st.expander('3. External code generation step (replay)'):
        replay = result['generation_replay']
        st.write(f"Режим: `{replay['mode']}`")
        st.write(f"Artifact source: `{replay['artifact_source']}`")
        st.markdown('**Переданный набор**')
        st.json(replay['context_excerpt'])
        st.markdown('**Полученный артефакт**')
        st.code(replay['replacement_code'], language='python')
    with st.expander('4. Apply + validation + impact'):
        _render_apply_result(result['apply_result'])
    with st.expander('5. Merge to master (dry-run)'):
        merge_plan = result['merge_plan']
        st.write(f"Режим: `{merge_plan['mode']}`")
        st.write(f"Готовность к ручному review merge: {'Да' if merge_plan['ready_for_manual_merge_review'] else 'Нет'}")
        if merge_plan.get('recommended_test_commands'):
            st.markdown('**Рекомендуемые команды для тестов**')
            for command in merge_plan['recommended_test_commands']:
                st.code(command, language='bash')
        for line in merge_plan.get('summary_lines', []):
            st.write(f'- {line}')


st.set_page_config(page_title='codecollector', layout='wide')
st.title('codecollector')
st.caption('PoC для поиска места изменения, сборки context pack, replay-generation и dry-run pipeline поверх Python-проекта')

project_root = Path(st.sidebar.text_input('Путь к проекту', str(DEFAULT_PROJECT))).resolve()
services = ProjectServices(project_root, config=CONFIG)
demo_artifact_options = sorted(path.name for path in ARTIFACTS_ROOT.glob('*.py'))

with st.sidebar:
    if st.button('Построить / обновить индекс'):
        try:
            st.session_state['build_report'] = asdict(services.build_index(full_rebuild=False))
        except Exception as exc:
            LOGGER.exception('Build index failed: %s', exc)
            st.error(str(exc))
    if 'build_report' in st.session_state:
        st.markdown('**Последний build index**')
        st.json(st.session_state['build_report'])

tab_search, tab_apply, tab_pipeline = st.tabs(['Поиск и context', 'Apply в staging', 'Полный dry-run pipeline'])

with tab_search:
    search_query = st.text_input('Текст change request / запроса', 'Изменить формирование текста уведомления о назначении тикета', key='search_query')
    limit = st.slider('Количество кандидатов', min_value=1, max_value=10, value=CONFIG.search_default_limit)
    use_vector_search = st.checkbox('Использовать векторный поиск по описаниям', value=CONFIG.search_vector_enabled, key='search_vector_enabled')
    if st.button('Найти кандидатов'):
        try:
            st.session_state['candidates'] = [asdict(item) for item in services.search(search_query, limit=limit, use_vector_search=use_vector_search)]
        except Exception as exc:
            LOGGER.exception('Search failed: %s', exc)
            st.error(str(exc))

    candidates = st.session_state.get('candidates', [])
    if candidates:
        _render_candidates(candidates)
        selected_qualname = st.selectbox('Target qualname', [item['qualname'] for item in candidates], key='search_target')
        st.session_state['selected_qualname'] = selected_qualname
        if st.button('Собрать context pack'):
            try:
                context_pack = services.context(selected_qualname)
                st.session_state['context_pack'] = {
                    'target': asdict(context_pack.target),
                    'neighbors': [asdict(item) for item in context_pack.neighbors],
                    'inbound_relations': [asdict(item) for item in context_pack.inbound_relations],
                    'outbound_relations': [asdict(item) for item in context_pack.outbound_relations],
                    'related_tests': [asdict(item) for item in context_pack.related_tests],
                    'recommended_tests': context_pack.recommended_tests,
                        'knowledge_title': context_pack.knowledge_title,
                    'knowledge_description': context_pack.knowledge_description,
                    'requirement_ids': context_pack.requirement_ids,
                    'requirement_titles': context_pack.requirement_titles,
                }
            except Exception as exc:
                LOGGER.exception('Context build failed: %s', exc)
                st.error(str(exc))
    if 'context_pack' in st.session_state:
        _render_context(st.session_state['context_pack'])

with tab_apply:
    apply_target = st.text_input('Target qualname для apply', st.session_state.get('selected_qualname', 'support_app.services.notification_service.build_assignment_message'))
    apply_operation = st.selectbox('Операция', ['replace_symbol', 'insert_after_symbol', 'add_symbol'])
    selected_artifact = st.selectbox('Demo artifact', demo_artifact_options, index=max(demo_artifact_options.index('build_assignment_message_v2.py'), 0) if 'build_assignment_message_v2.py' in demo_artifact_options else 0)
    if st.button('Применить в staging'):
        try:
            artifact_path = ARTIFACTS_ROOT / selected_artifact
            artifact = PatchArtifact(
                target_qualname=apply_target,
                replacement_code=artifact_path.read_text(encoding='utf-8'),
                operation=apply_operation,
            )
            result = services.apply(artifact)
            st.session_state['apply_result'] = {
                'workspace_path': str(result.workspace_path),
                'artifact': asdict(result.artifact),
                'diff': asdict(result.diff),
                'validation': {
                    'is_valid': result.validation.is_valid,
                    'issues': [asdict(item) for item in result.validation.issues],
                },
                'reindexed': result.reindexed,
                'impact': asdict(result.impact),
            }
        except Exception as exc:
            LOGGER.exception('Apply failed: %s', exc)
            st.error(str(exc))
    if 'apply_result' in st.session_state:
        _render_apply_result(st.session_state['apply_result'])

with tab_pipeline:
    title = st.text_input('Change request: title', 'Изменить формирование текста уведомления о назначении тикета', key='pipeline_title')
    description = st.text_area('Change request: description', 'Сделать текст уведомления русскоязычным и использовать фразу «теперь назначен на».', key='pipeline_description')
    constraints_raw = st.text_area('Constraints (по одной на строку)', 'Не менять внешний контракт API\nИзменить только текст уведомления', key='pipeline_constraints')
    pipeline_limit = st.slider('Размер shortlist для pipeline', min_value=1, max_value=10, value=CONFIG.search_default_limit, key='pipeline_limit')
    pipeline_use_vector = st.checkbox('Использовать векторный поиск по описаниям в pipeline', value=CONFIG.search_vector_enabled, key='pipeline_vector_enabled')

    if st.button('Получить shortlist для change request'):
        try:
            constraints = [line.strip() for line in constraints_raw.splitlines() if line.strip()]
            change_request = ChangeRequest(title=title, description=description, constraints=constraints, project=project_root.name)
            st.session_state['pipeline_change_request'] = asdict(change_request)
            st.session_state['pipeline_candidates'] = [asdict(item) for item in services.search(change_request.search_text(), limit=pipeline_limit, use_vector_search=pipeline_use_vector)]
        except Exception as exc:
            LOGGER.exception('Pipeline shortlist failed: %s', exc)
            st.error(str(exc))

    pipeline_candidates = st.session_state.get('pipeline_candidates', [])
    if pipeline_candidates:
        _render_candidates(pipeline_candidates)
        selected_pipeline_target = st.selectbox('Подтвердить target для pipeline', [item['qualname'] for item in pipeline_candidates], key='pipeline_selected_target')
        selected_pipeline_artifact = st.selectbox('Replay artifact для pipeline', demo_artifact_options, key='pipeline_selected_artifact')
        pipeline_operation = st.selectbox('Операция pipeline', ['replace_symbol', 'insert_after_symbol', 'add_symbol'], key='pipeline_operation')
        if st.button('Запустить полный dry-run pipeline'):
            try:
                constraints = [line.strip() for line in constraints_raw.splitlines() if line.strip()]
                change_request = ChangeRequest(title=title, description=description, constraints=constraints, project=project_root.name)
                result = services.pipeline_replay(
                    change_request=change_request,
                    selected_target=selected_pipeline_target,
                    artifact_file=ARTIFACTS_ROOT / selected_pipeline_artifact,
                    operation=pipeline_operation,
                    limit=pipeline_limit,
                    use_vector_search=pipeline_use_vector,
                )
                st.session_state['pipeline_result'] = {
                    'run_id': result.run_id,
                    'run_label': result.run_label,
                    'run_dir': str(result.run_dir),
                    'change_request': asdict(result.change_request),
                    'selected_target': result.selected_target,
                    'build_report': result.build_report,
                    'steps': [asdict(item) for item in result.steps],
                    'search_candidates': [asdict(item) for item in result.search_candidates],
                    'context_pack': {
                        'target': asdict(result.context_pack.target),
                        'neighbors': [asdict(item) for item in result.context_pack.neighbors],
                        'inbound_relations': [asdict(item) for item in result.context_pack.inbound_relations],
                        'outbound_relations': [asdict(item) for item in result.context_pack.outbound_relations],
                        'related_tests': [asdict(item) for item in result.context_pack.related_tests],
                        'recommended_tests': result.context_pack.recommended_tests,
                        'knowledge_title': result.context_pack.knowledge_title,
                        'knowledge_description': result.context_pack.knowledge_description,
                        'requirement_ids': result.context_pack.requirement_ids,
                        'requirement_titles': result.context_pack.requirement_titles,
                    },
                    'generation_replay': asdict(result.generation_replay),
                    'apply_result': {
                        'workspace_path': str(result.apply_result.workspace_path),
                        'artifact': asdict(result.apply_result.artifact),
                        'diff': asdict(result.apply_result.diff),
                        'validation': {
                            'is_valid': result.apply_result.validation.is_valid,
                            'issues': [asdict(item) for item in result.apply_result.validation.issues],
                        },
                        'reindexed': result.apply_result.reindexed,
                        'impact': asdict(result.apply_result.impact),
                    },
                    'merge_plan': asdict(result.merge_plan),
                }
            except Exception as exc:
                LOGGER.exception('Pipeline replay failed: %s', exc)
                st.error(str(exc))

    if 'pipeline_result' in st.session_state:
        _render_pipeline_result(st.session_state['pipeline_result'])
