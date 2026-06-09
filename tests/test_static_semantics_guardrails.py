from __future__ import annotations

from pathlib import Path

from codecollector.domain.models import ChangeRequest
from codecollector.orchestration.pipeline_service import PipelineService
from codecollector.validation.semantic_checks import (
    validate_code_artifact_static_semantics,
    validate_generated_test_relevance,
    validate_generated_test_static_semantics,
    validate_patch_static_semantics,
)


def test_patch_static_semantics_runs_with_current_contract() -> None:
    original = 'class Demo:\n    def target(self):\n        raise NotImplementedError()\n'
    patched = 'class Demo:\n    def target(self):\n        return 1\n'

    block = validate_patch_static_semantics(
        requested_operation='replace_symbol',
        change_request=ChangeRequest(title='Update', description='Update target', project='demo'),
        target_qualname='demo.Demo.target',
        original_file_text=original,
        patched_file_text=patched,
        changed_files=['demo.py'],
        target_file='demo.py',
        insert_scope='class_body',
        parent_qualname='demo.Demo',
    )

    assert block.name == 'patch_static_semantics'


def test_code_artifact_static_semantics_uses_current_expected_metadata_contract() -> None:
    block = validate_code_artifact_static_semantics(
        result_payload={
            'status': 'ok',
            'code_artifact': {
                'operation': 'replace_symbol',
                'target_qualname': 'demo.Demo.target',
                'code': 'def target(self):\n    return 1\n',
            },
        },
        step_name='external_repair',
        expected_operation='replace_symbol',
        expected_target_qualname='demo.Demo.target',
    )

    assert block.name == 'external_repair_static_semantics'


def test_generated_test_static_semantics_uses_current_signature(tmp_path: Path) -> None:
    test_file = tmp_path / 'tests' / 'test_generated.py'
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'def test_target_behavior():\n'
        '    assert 1 == 1\n',
        encoding='utf-8',
    )

    block = validate_generated_test_static_semantics(
        project_root=tmp_path,
        test_file_path=test_file,
        target_qualname='demo.Demo.target',
        requested_operation='replace_symbol',
        generated_symbol_names=['demo.Demo.target'],
    )

    assert block.name == 'generated_test_static_semantics'


def test_generated_test_relevance_uses_current_signature() -> None:
    block = validate_generated_test_relevance(
        test_source='def test_target_behavior():\n    assert "target"\n',
        requested_operation='replace_symbol',
        target_qualname='demo.Demo.target',
        generated_symbol_names=['demo.Demo.target'],
    )

    assert block.name == 'generated_test_relevance'


def test_generated_test_review_summary_has_no_repair_fields() -> None:
    service = PipelineService.__new__(PipelineService)
    summary = service._compact_generated_test_review_summary({
        'review': {
            'verdict': 'production_likely_ok_test_likely_bad',
            'confidence': 0.8,
            'production_code_quality': 'good',
            'generated_test_quality': 'bad',
            'should_keep_production_code': 'yes',
            'recommended_action': 'keep_production_code_exclude_test',
            'recommendation_summary': 'Проверить вручную.',
            'generated_test_repairability': 'repairable',
            'generated_test_repair_kind': 'bad_setup_heavy_parent_instance',
            'generated_test_repair_instructions': ['Исправить тест.'],
            'generated_test_repair_risks': ['Риск.'],
        }
    })

    assert summary is not None
    assert summary['verdict'] == 'production_likely_ok_test_likely_bad'
    assert 'generated_test_repairability' not in summary
    assert 'generated_test_repair_kind' not in summary
    assert 'generated_test_repair_instructions' not in summary
    assert 'generated_test_repair_risks' not in summary
