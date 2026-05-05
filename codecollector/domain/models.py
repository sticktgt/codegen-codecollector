from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SymbolKind = Literal["module", "class", "function", "method"]
RelationKind = Literal[
    "imports",
    "contains",
    "calls",
    "covered_by_test",
    "belongs_to_layer",
    "implements_requirement",
    "exposed_by_controller",
]
RelationSource = Literal["index", "knowledge"]
RelationConfidence = Literal["high", "medium", "low"]
Severity = Literal["info", "warning", "error"]
PatchOperation = Literal["replace_symbol", "insert_after_symbol"]
InsertScope = Literal["module_body", "class_body"]


@dataclass(slots=True)
class CodeSpan:
    start_line: int
    end_line: int


@dataclass(slots=True)
class ChangeRequest:
    title: str
    description: str
    project: str
    constraints: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def search_text(self) -> str:
        parts = [self.title.strip(), self.description.strip()]
        return " ".join(part for part in parts if part)


@dataclass(slots=True)
class SymbolRecord:
    file_path: str
    module_name: str
    name: str
    qualname: str
    kind: SymbolKind
    parent_qualname: str | None
    start_line: int
    end_line: int
    docstring: str = ""
    source_code: str = ""


@dataclass(slots=True)
class RelationRecord:
    source_qualname: str
    relation_kind: RelationKind
    target_ref: str
    file_path: str
    target_qualname: str | None = None
    relation_source: RelationSource = "index"
    relation_confidence: RelationConfidence = "medium"


@dataclass(slots=True)
class SearchCandidate:
    qualname: str
    name: str
    kind: str
    file_path: str
    score: float
    confidence: float = 0.0
    relevance_category: str = "низкая"
    reasons: list[str] = field(default_factory=list)
    docstring: str = ""
    knowledge_title: str = ""
    requirements: list[str] = field(default_factory=list)
    ranked_by_llm: bool = False
    llm_recommended: bool = False
    llm_rank: int | None = None
    llm_reason: str = ""


@dataclass(slots=True)
class ContextPack:
    target: SymbolRecord
    neighbors: list[SymbolRecord]
    inbound_relations: list[RelationRecord]
    outbound_relations: list[RelationRecord]
    related_tests: list[SymbolRecord]
    requirement_ids: list[str] = field(default_factory=list)
    requirement_titles: list[str] = field(default_factory=list)
    knowledge_title: str = ""
    knowledge_description: str = ""
    recommended_tests: list[str] = field(default_factory=list)
    relation_confidence_summary: dict[str, dict[str, int]] = field(default_factory=dict)
    reference_artifacts: list[ReferenceArtifact] = field(default_factory=list)
    reference_summary: dict[str, Any] = field(default_factory=dict)




@dataclass(slots=True)
class ReferenceArtifact:
    artifact_id: str
    title: str
    description: str
    artifact_type: str
    usage_mode: str
    language: str
    relevance_score: float
    why_selected: str
    content_mode: str
    source_path: str
    content: str
    selected_span: dict[str, int] | None = None

@dataclass(slots=True)
class PatchArtifact:
    target_qualname: str
    replacement_code: str
    operation: PatchOperation = "replace_symbol"
    insert_scope: InsertScope | None = None
    expected_new_symbol_kind: str | None = None
    parent_qualname: str | None = None
    import_changes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class DiffSummary:
    changed_files: list[str]
    unified_diff: str


@dataclass(slots=True)
class ImpactSummary:
    target_qualname: str
    changed_files: list[str]
    symbols_in_changed_files: list[str]
    inbound_callers: list[str]
    related_tests: list[str]
    linked_requirements: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    recommended_test_commands: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationIssue:
    severity: Severity
    check_name: str
    message: str
    file_path: str | None = None


@dataclass(slots=True)
class ValidationReport:
    is_valid: bool
    issues: list[ValidationIssue]

@dataclass(slots=True)
class VerificationIssue:
    code: str
    message: str
    severity: Severity = "error"
    file_path: str | None = None
    symbol: str | None = None


@dataclass(slots=True)
class VerificationBlock:
    name: str
    ok: bool
    severity: Severity = "info"
    issues: list[VerificationIssue] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationReport:
    verdict: str
    passed: bool
    blocks: list[VerificationBlock] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class ApplyResult:
    workspace_path: Path
    artifact: PatchArtifact
    diff: DiffSummary
    validation: ValidationReport
    reindexed: bool
    impact: ImpactSummary


@dataclass(slots=True)
class GenerationReplay:
    mode: str
    change_request: ChangeRequest
    selected_target: str
    operation: PatchOperation
    context_excerpt: dict[str, Any]
    artifact_source: str
    replacement_code: str


@dataclass(slots=True)
class ExternalGenerationCall:
    mode: str
    command: list[str]
    request_path: str
    result_path: str
    trace_path: str | None
    request_summary: dict[str, Any] = field(default_factory=dict)
    result_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MergePlan:
    mode: str
    ready_for_manual_merge_review: bool
    workspace_path: str
    changed_files: list[str]
    symbols_in_changed_files: list[str]
    linked_requirements: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    recommended_test_commands: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PipelineStepRecord:
    step_name: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    summary: str
    error_type: str | None = None
    error_message: str | None = None
    exception_class: str | None = None

@dataclass(slots=True)
class PipelineExecutionSummary:
    status: str
    selected_target: str
    requested_operation: str | None = None
    final_operation: str | None = None
    insert_scope: str | None = None
    changed_files: list[str] = field(default_factory=list)
    symbols_in_changed_files: list[str] = field(default_factory=list)
    workspace_path: str | None = None
    verification_passed: bool | None = None
    has_generated_test: bool = False
    generated_test_files: list[str] = field(default_factory=list)
    repair_used: bool = False
    merge_mode: str | None = None
    merge_ready: bool | None = None
    linked_requirements: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    recommended_test_commands: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)
    code_generation_usage: dict[str, Any] | None = None
    test_generation_usage: dict[str, Any] | None = None
    repair_generation_usage: dict[str, Any] | None = None
    embedding_usage: dict[str, Any] | None = None

@dataclass(slots=True)
class GenerateApiResultSummary:
    status: str
    selected_target: str
    requested_operation: str | None = None
    final_operation: str | None = None
    insert_scope: str | None = None
    workspace_path: str | None = None
    changed_files: list[str] = field(default_factory=list)
    symbols_in_changed_files: list[str] = field(default_factory=list)
    verification_passed: bool | None = None
    merge_mode: str | None = None
    merge_ready: bool | None = None
    has_generated_test: bool = False
    generated_test_files: list[str] = field(default_factory=list)
    repair_used: bool = False
    linked_requirements: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    recommended_test_commands: list[str] = field(default_factory=list)
    excluded_files: list[str] = field(default_factory=list)

@dataclass(slots=True)
class AnalyzeApiResultSummary:
    status: str
    project_id: str
    requested_operation: str | None = None
    insert_scope: str | None = None
    recommended_target: str | None = None
    candidates_count: int = 0
    recall_candidates_count: int = 0
    returned_candidates_count: int = 0
    top_candidates: list[str] = field(default_factory=list)
    has_context_summary: bool = False
    operation_source: str | None = None
    operation_confidence: float | None = None
    request_quality_status: str | None = None
    manual_review_required: bool = False
    target_selection_source: str | None = None
    target_selection_confidence: float | None = None
    analysis_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SelectTargetApiResultSummary:
    status: str
    project_id: str
    requested_operation: str | None = None
    insert_scope: str | None = None
    recommended_target: str | None = None
    selected_target: str | None = None
    selection_changed: bool = False    

@dataclass(slots=True)
class PipelineRunResult:
    run_id: str
    run_label: str
    run_dir: Path
    change_request: ChangeRequest
    selected_target: str
    build_report: dict[str, Any] | None = None
    search_candidates: list[SearchCandidate] = field(default_factory=list)
    context_pack: ContextPack | None = None
    apply_result: ApplyResult | None = None
    merge_plan: MergePlan | None = None
    steps: list[PipelineStepRecord] = field(default_factory=list)
    generation_replay: GenerationReplay | None = None
    external_code_generation: ExternalGenerationCall | None = None
    external_test_generation: ExternalGenerationCall | None = None
    generated_test_apply: dict[str, Any] | None = None
    verification_report: VerificationReport | None = None
    repair_generation: ExternalGenerationCall | None = None
    warnings: list[str] = field(default_factory=list)
    execution_summary: PipelineExecutionSummary | None = None
