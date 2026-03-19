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
PatchOperation = Literal["replace_symbol", "insert_after_symbol", "add_symbol"]


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


@dataclass(slots=True)
class PatchArtifact:
    target_qualname: str
    replacement_code: str
    operation: PatchOperation = "replace_symbol"


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
class MergePlan:
    mode: str
    ready_for_manual_merge_review: bool
    workspace_path: str
    changed_files: list[str]
    symbols_in_changed_files: list[str]
    linked_requirements: list[str] = field(default_factory=list)
    recommended_tests: list[str] = field(default_factory=list)
    recommended_test_commands: list[str] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PipelineStepRecord:
    step_name: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    summary: str


@dataclass(slots=True)
class PipelineRunResult:
    run_id: str
    run_label: str
    run_dir: Path
    change_request: ChangeRequest
    selected_target: str
    build_report: dict[str, Any]
    search_candidates: list[SearchCandidate]
    context_pack: ContextPack
    generation_replay: GenerationReplay
    apply_result: ApplyResult
    merge_plan: MergePlan
    steps: list[PipelineStepRecord]
