from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

st.set_page_config(page_title="CodeCollector Runs", layout="wide")


@dataclass(slots=True)
class RunRecord:
    path: Path
    payload: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.payload.get("run_id", self.path.stem))

    @property
    def run_label(self) -> str:
        return str(self.payload.get("run_label", ""))

    @property
    def project(self) -> str:
        change_request = self.payload.get("change_request", {}) or {}
        return str(change_request.get("project", ""))

    @property
    def target(self) -> str:
        return str(self.payload.get("selected_target", ""))

    @property
    def ready(self) -> bool:
        merge_plan = self.payload.get("merge_plan", {}) or {}
        return bool(merge_plan.get("ready_for_manual_merge_review", False))

    @property
    def verification_passed(self) -> bool | None:
        verification = self.payload.get("verification_report")
        if verification is None:
            return None
        return bool(verification.get("passed", False))

    @property
    def had_repair(self) -> bool:
        return self.payload.get("repair_generation") is not None

    @property
    def had_generated_tests(self) -> bool:
        generated = self.payload.get("generated_test_apply")
        return bool(generated and generated.get("count", 0) > 0)

    @property
    def total_duration_ms(self) -> int:
        steps = self.payload.get("steps", []) or []
        return int(sum(int(step.get("duration_ms", 0)) for step in steps))

    @property
    def status_label(self) -> str:
        passed = self.verification_passed
        if passed is True:
            return "passed"
        if passed is False:
            return "failed"
        return "unknown"


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def load_runs(root_dir: str) -> list[RunRecord]:
    root = Path(root_dir)
    if not root.exists():
        return []

    records: list[RunRecord] = []
    for path in sorted(root.glob("pipeline-*/pipeline_run_*.json"), reverse=True):
        payload = _safe_read_json(path)
        if payload is None:
            continue
        records.append(RunRecord(path=path, payload=payload))
    return records


def format_duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = int(seconds % 60)
    return f"{minutes}m {rem}s"


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_runs_table(runs: list[RunRecord]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_id": run.run_id,
                "Время": run.run_label,
                "Проект": run.project,
                "Target": run.target,
                "Статус": run.status_label,
                "Repair": "да" if run.had_repair else "нет",
                "Тест": "да" if run.had_generated_tests else "нет",
                "Длительность": format_duration(run.total_duration_ms),
            }
            for run in runs
        ]
    )


def render_summary(run: RunRecord) -> None:
    change_request = run.payload.get("change_request", {}) or {}
    generated_test_apply = run.payload.get("generated_test_apply") or {}

    m1, m2, m3, m4, m5 = st.columns([1.5, 0.9, 0.8, 0.9, 1.0])
    m1.metric("Проект", run.project or "—")
    m2.metric("Проверки", run.status_label)
    m3.metric("Repair", "да" if run.had_repair else "нет")
    m4.metric("Тестов создано", str(generated_test_apply.get("count", 0)))
    m5.metric("Длительность", format_duration(run.total_duration_ms))

    st.caption(f"Run ID: {run.run_id}")
    st.markdown(f"**Target:** `{run.target or '—'}`")
    st.markdown(f"**Статус merge:** {'готов к ручному merge' if run.ready else 'требует проверки'}")

    with st.expander("Запрос на изменение", expanded=True):
        st.markdown(f"**Название:** {change_request.get('title', '—')}")
        description = str(change_request.get("description", "")).strip()
        if description:
            st.markdown("**Описание:**")
            st.write(description)
        constraints = change_request.get("constraints", []) or []
        if constraints:
            st.markdown("**Ограничения запроса:**")
            for item in constraints:
                st.markdown(f"- {item}")
        notes = change_request.get("notes", []) or []
        if notes:
            st.markdown("**Дополнительные заметки:**")
            for item in notes:
                st.markdown(f"- {item}")


def render_steps(run: RunRecord) -> None:
    steps = run.payload.get("steps", []) or []
    if not steps:
        st.info("Шаги выполнения отсутствуют.")
        return

    df = pd.DataFrame(
        [
            {
                "Этап": step.get("step_name", ""),
                "Статус": step.get("status", ""),
                "Длительность": format_duration(int(step.get("duration_ms", 0))),
                "Описание": step.get("summary", ""),
            }
            for step in steps
        ]
    )
    st.dataframe(df, width="stretch", hide_index=True)


def render_search(run: RunRecord) -> None:
    items = run.payload.get("search_candidates", []) or []
    if not items:
        st.info("Результаты поиска отсутствуют.")
        return

    for idx, item in enumerate(items, start=1):
        title = f"{idx}. {item.get('qualname', '')}"
        with st.expander(title, expanded=(idx == 1)):
            meta_left, meta_right = st.columns([2, 1])
            with meta_left:
                st.caption("Файл")
                st.code(str(item.get("file_path", "")), language=None)
            with meta_right:
                st.caption("Оценка релевантности")
                st.code(str(item.get("score", "")), language=None)
            docstring = str(item.get("docstring", "")).strip()
            if docstring:
                st.caption("Краткое описание символа")
                st.write(docstring)
            reasons = item.get("reasons", []) or []
            if reasons:
                st.markdown("**Детали совпадения и ранжирования**")
                for reason in reasons:
                    st.markdown(f"- {reason}")


def render_context(run: RunRecord) -> None:
    context = run.payload.get("context_pack", {}) or {}
    target = context.get("target", {}) or {}
    st.markdown("**Целевой символ**")
    st.caption(str(target.get("qualname", "")))
    st.code(str(target.get("source_code", "")), language="python")

    reference_artifacts = context.get("reference_artifacts", []) or []
    if reference_artifacts:
        st.markdown("**Reference Library**")
        for idx, artifact in enumerate(reference_artifacts, start=1):
            with st.expander(f"{idx}. {artifact.get('title', 'reference artifact')}", expanded=False):
                st.markdown(f"**Тип:** {artifact.get('artifact_type', '')}")
                st.markdown(f"**Режим использования:** {artifact.get('usage_mode', '')}")
                st.markdown(f"**Источник:** `{artifact.get('source_path', '')}`")
                st.code(str(artifact.get("content", "")), language="python")

    with st.expander("Полный context_pack", expanded=False):
        st.code(compact_json(context), language="json")


def render_generation_context(request_payload: dict[str, Any]) -> None:
    project_context = request_payload.get("project_context", {}) or {}
    reference_context = request_payload.get("reference_context", {}) or {}

    with st.expander("Контекст для генерации", expanded=False):
        target_symbol = project_context.get("target_symbol", {}) or {}
        if target_symbol:
            st.markdown("**Целевой символ**")
            st.caption(str(target_symbol.get("qualname", "")))
            st.code(str(target_symbol.get("source", "")), language="python")

        module_outline = project_context.get("module_outline", []) or []
        if module_outline:
            st.markdown("**Соседний код / module outline**")
            st.code(compact_json(module_outline), language="json")

        related_tests = project_context.get("related_tests", []) or []
        if related_tests:
            st.markdown("**Связанные тесты**")
            st.code(compact_json(related_tests), language="json")

        reference_artifacts = reference_context.get("reference_artifacts", []) or []
        if reference_artifacts:
            st.markdown("**Reference artifacts**")
            for idx, artifact in enumerate(reference_artifacts, start=1):
                with st.expander(f"{idx}. {artifact.get('title', 'artifact')}", expanded=False):
                    st.markdown(f"**Тип:** {artifact.get('artifact_type', '')}")
                    st.markdown(f"**Режим:** {artifact.get('usage_mode', '')}")
                    st.markdown(f"**Источник:** `{artifact.get('source_path', '')}`")
                    st.code(str(artifact.get("content", "")), language="python")

        st.markdown("**Полный request payload**")
        st.code(compact_json(request_payload), language="json")


def render_context_metrics(request_payload: dict[str, Any]) -> None:
    metrics = request_payload.get("context_metrics", {}) or {}
    if not metrics:
        st.info("Метрики контекста отсутствуют.")
        return
    with st.expander("Размер и состав контекста", expanded=False):
        cols = st.columns(4)
        pairs = [
            ("request_chars", metrics.get("request_chars", "—")),
            ("request_chars_limit", metrics.get("request_chars_limit", "—")),
            ("target_source_chars", metrics.get("target_source_chars", "—")),
            ("reference_chars", metrics.get("reference_chars", "—")),
            ("related_test_chars", metrics.get("related_test_chars", "—")),
            ("full_file_chars", metrics.get("full_file_chars", "—")),
            ("reference_artifacts_count", metrics.get("reference_artifacts_count", "—")),
            ("related_tests_count", metrics.get("related_tests_count", "—")),
        ]
        for i, (label, value) in enumerate(pairs):
            cols[i % 4].metric(label, str(value))
        st.code(compact_json(metrics), language="json")


def _render_code_result(payload: dict[str, Any]) -> None:
    result_payload = payload.get("result_payload", {}) or {}
    code_artifact = result_payload.get("code_artifact") or {}
    if not code_artifact:
        st.info("Данные отсутствуют.")
        return
    st.markdown("**Сгенерированный код**")
    st.markdown(f"**Файл:** `{code_artifact.get('target_file', '')}`")
    st.markdown(f"**Символ:** `{code_artifact.get('target_qualname', '')}`")
    st.code(str(code_artifact.get("code", "")), language="python")


def _render_test_result(payload: dict[str, Any]) -> None:
    result_payload = payload.get("result_payload", {}) or {}
    test_artifact = result_payload.get("test_artifact") or {}
    if not test_artifact:
        st.info("Данные отсутствуют.")
        return
    st.markdown("**Сгенерированный тест**")
    st.markdown(f"**Файл:** `{test_artifact.get('file_path', '')}`")
    st.code(str(test_artifact.get("source_code", "")), language="python")


def render_external_block(kind: str, payload: dict[str, Any] | None) -> None:
    if not payload:
        st.info("Данные отсутствуют.")
        return

    request_payload = payload.get("request_payload", {}) or {}

    st.caption(f"Trace path: {payload.get('trace_path', '')}")
    p1, p2 = st.columns(2)
    with p1:
        st.markdown(f"**Request path:** `{payload.get('request_path', '')}`")
    with p2:
        st.markdown(f"**Result path:** `{payload.get('result_path', '')}`")

    if kind == "code":
        _render_code_result(payload)
    elif kind == "test":
        _render_test_result(payload)
    else:
        st.code(compact_json(payload.get("result_payload", {})), language="json")

    render_context_metrics(request_payload)
    render_generation_context(request_payload)


def render_diff(run: RunRecord) -> None:
    apply_result = run.payload.get("apply_result", {}) or {}
    diff = apply_result.get("diff", {}) or {}
    impact = apply_result.get("impact", {}) or {}
    merge_plan = run.payload.get("merge_plan", {}) or {}
    generated = run.payload.get("generated_test_apply") or {}

    changed_files = merge_plan.get("changed_files") or impact.get("changed_files") or diff.get("changed_files") or []
    if changed_files:
        st.markdown("**Измененные и созданные файлы**")
        for path in changed_files:
            st.markdown(f"- `{path}`")
    if generated:
        st.caption(f"Добавлено тестовых файлов: {generated.get('count', 0)}")

    st.markdown("**Diff по измененному исходному коду**")
    st.code(str(diff.get("unified_diff", "")), language="diff")


def render_verification(run: RunRecord) -> None:
    verification = run.payload.get("verification_report", {}) or {}
    if not verification:
        st.info("Проверки отсутствуют.")
        return

    st.markdown(f"**Результат:** {'passed' if verification.get('passed') else 'failed'}")
    results = verification.get("results", {}) or {}
    for key, value in results.items():
        with st.expander(key, expanded=(key == "pytest_recommended")):
            st.code(compact_json(value), language="json")


def render_merge(run: RunRecord) -> None:
    merge_plan = run.payload.get("merge_plan", {}) or {}
    st.code(compact_json(merge_plan), language="json")


st.sidebar.title("CodeCollector UI")
runs_root = st.sidebar.text_input("Каталог с run artifacts", value=".runs")
runs = load_runs(runs_root)

if not runs:
    st.warning("В указанном каталоге не найдено ни одного pipeline_run_*.json")
    st.stop()

projects = sorted({run.project for run in runs if run.project})
statuses = sorted({run.status_label for run in runs})

selected_project = st.sidebar.selectbox("Проект", ["Все"] + projects)
selected_status = st.sidebar.selectbox("Статус", ["Все"] + statuses)
search_text = st.sidebar.text_input("Поиск по target / title")

filtered_runs = []
for run in runs:
    if selected_project != "Все" and run.project != selected_project:
        continue
    if selected_status != "Все" and run.status_label != selected_status:
        continue
    haystack = " ".join(
        [
            run.run_id,
            run.target,
            str((run.payload.get("change_request", {}) or {}).get("title", "")),
        ]
    ).lower()
    if search_text and search_text.lower() not in haystack:
        continue
    filtered_runs.append(run)

st.sidebar.caption(f"Найдено запусков: {len(filtered_runs)}")
if not filtered_runs:
    st.info("По текущим фильтрам запусков не найдено.")
    st.stop()

st.title("Просмотр запусков pipeline")

runs_df = build_runs_table(filtered_runs)
selection = st.dataframe(
    runs_df.drop(columns=["run_id"]),
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
)
selected_rows = selection.get("selection", {}).get("rows", []) if isinstance(selection, dict) else []
selected_idx = selected_rows[0] if selected_rows else 0
run = filtered_runs[selected_idx]

render_summary(run)


tab_overview, tab_steps, tab_search, tab_context, tab_codegen, tab_testgen, tab_repair, tab_diff, tab_verification, tab_merge, tab_raw = st.tabs(
    [
        "Общее",
        "Этапы",
        "Поиск",
        "Контекст",
        "Генерация кода",
        "Генерация теста",
        "Repair",
        "Diff",
        "Проверки",
        "Merge",
        "Raw",
    ]
)

with tab_overview:
    st.code(compact_json({
        "run_id": run.run_id,
        "project": run.project,
        "selected_target": run.target,
        "ready_for_manual_merge_review": run.ready,
        "had_repair": run.had_repair,
        "had_generated_tests": run.had_generated_tests,
        "total_duration_ms": run.total_duration_ms,
    }), language="json")

with tab_steps:
    render_steps(run)

with tab_search:
    render_search(run)

with tab_context:
    render_context(run)

with tab_codegen:
    render_external_block("code", run.payload.get("external_code_generation"))

with tab_testgen:
    render_external_block("test", run.payload.get("external_test_generation"))
    generated = run.payload.get("generated_test_apply")
    if generated:
        with st.expander("generated_test_apply", expanded=False):
            st.code(compact_json(generated), language="json")

with tab_repair:
    render_external_block("repair", run.payload.get("repair_generation"))

with tab_diff:
    render_diff(run)

with tab_verification:
    render_verification(run)

with tab_merge:
    render_merge(run)

with tab_raw:
    with st.expander("Полный pipeline_run.json", expanded=False):
        st.code(compact_json(run.payload), language="json")
