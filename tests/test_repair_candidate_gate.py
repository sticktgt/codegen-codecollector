from codecollector.domain.models import VerificationBlock, VerificationIssue
from codecollector.orchestration.pipeline_service import PipelineService
from codecollector.validation.semantic_checks import build_verification_report


def _service() -> PipelineService:
    return object.__new__(PipelineService)


def test_repair_candidate_gate_accepts_clean_candidate() -> None:
    service = _service()

    block = service._build_repair_candidate_gate_block(
        stage="patch_static_semantics",
        original_blocks=[
            VerificationBlock(
                name="patch_static_semantics",
                ok=False,
                issues=[VerificationIssue(code="contract_call_argument_type_mismatch", message="bad type")],
            )
        ],
        candidate_blocks=[VerificationBlock(name="patch_static_semantics", ok=True)],
    )

    assert block.ok is True
    assert block.details["decision"] == "accepted"
    assert block.details["original_issue_codes"] == ["contract_call_argument_type_mismatch"]




def test_repair_candidate_gate_accepts_candidate_with_advisory_warnings() -> None:
    service = _service()

    block = service._build_repair_candidate_gate_block(
        stage="patch_static_semantics",
        original_blocks=[
            VerificationBlock(
                name="patch_static_semantics",
                ok=False,
                issues=[VerificationIssue(code="contract_call_argument_type_mismatch", message="bad type")],
            )
        ],
        candidate_blocks=[
            VerificationBlock(
                name="patch_static_semantics",
                ok=True,
                severity="warning",
                issues=[
                    VerificationIssue(
                        code="unused_import_change",
                        message="unused import",
                        severity="warning",
                    )
                ],
            )
        ],
    )

    assert block.ok is True
    assert block.details["decision"] == "accepted"
    assert block.details["candidate_issue_codes"] == []


def test_repair_candidate_gate_rejects_candidate_with_blocking_issues() -> None:
    service = _service()

    block = service._build_repair_candidate_gate_block(
        stage="patch_static_semantics",
        original_blocks=[
            VerificationBlock(
                name="patch_static_semantics",
                ok=False,
                issues=[VerificationIssue(code="contract_call_argument_type_mismatch", message="bad type")],
            )
        ],
        candidate_blocks=[
            VerificationBlock(
                name="patch_static_semantics",
                ok=False,
                issues=[VerificationIssue(code="unknown_runtime_name", message="missing import")],
            )
        ],
    )

    assert block.ok is False
    assert block.issues[0].code == "repair_candidate_rejected"
    assert block.details["candidate_issue_codes"] == ["unknown_runtime_name"]
    assert "unknown_runtime_name" in service._repair_candidate_rejection_warning(block)


def test_repair_candidate_gate_is_production_block_for_report() -> None:
    report = build_verification_report(
        blocks=[
            VerificationBlock(
                name="repair_candidate_gate",
                ok=False,
                issues=[VerificationIssue(code="repair_candidate_rejected", message="candidate failed")],
            )
        ]
    )

    assert report.passed is False
    assert report.verdict == "verification_failed"
    assert report.summary["production_failed"] is True


def test_repair_not_applied_block_preserves_repair_failure_as_verification_issue() -> None:
    service = _service()

    block = service._repair_not_applied_block(
        {
            'status': 'error',
            'error_type': 'operation_mismatch',
            'message': 'Repair returned operation replace_symbol, expected insert_after_symbol',
            'trace_path': '/tmp/trace.json',
        }
    )

    assert block.ok is False
    assert block.name == 'external_repair_not_applied'
    assert block.issues[0].code == 'repair_not_applied'
    assert block.details['error_type'] == 'operation_mismatch'
    assert block.details['trace_path'] == '/tmp/trace.json'


def test_repair_not_applied_block_keeps_original_verification_report_failed() -> None:
    service = _service()
    original = VerificationBlock(
        name='runtime_pytest_recommended',
        ok=False,
        issues=[VerificationIssue(code='duplicate_symbol_definition', message='duplicate symbol')],
    )
    repair_block = service._repair_not_applied_block(
        {
            'status': 'error',
            'error_type': 'operation_mismatch',
            'message': 'wrong operation',
        }
    )

    report = build_verification_report(blocks=[original, repair_block])

    assert report.passed is False
    assert report.verdict == 'verification_failed'
    codes = [issue.code for block in report.blocks for issue in block.issues]
    assert codes == ['duplicate_symbol_definition', 'repair_not_applied']
    assert 'operation_mismatch' in service._repair_not_applied_warning({'error_type': 'operation_mismatch', 'message': 'wrong operation'})
