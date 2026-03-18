from __future__ import annotations

from pathlib import Path

import yaml

from codecollector.domain.models import PatchArtifact
from codecollector.orchestration.services import ProjectServices

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = REPO_ROOT / 'demo_projects' / 'sample_python_app'
GOLDEN_CASES_PATHS = [
    REPO_ROOT / 'tests' / 'golden' / 'demo_cases.yaml',
    REPO_ROOT / 'tests' / 'golden' / 'demo_cases_v2.yaml',
]


def load_cases() -> list[dict]:
    result: list[dict] = []
    for path in GOLDEN_CASES_PATHS:
        payload = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        result.extend(payload.get('cases', []))
    return result


def test_golden_cases_are_satisfied() -> None:
    services = ProjectServices(PROJECT_ROOT)
    services.build_index(full_rebuild=True)

    for case in load_cases():
        if case['kind'] == 'search':
            candidates = services.search(case['query'], limit=max(5, len(case.get('expected_candidates', []))))
            assert candidates, f"Search case {case['id']} returned no candidates"
            assert candidates[0].qualname == case['expected_top_qualname']
            if case.get('expected_relevance_category'):
                assert candidates[0].relevance_category == case['expected_relevance_category']
            qualnames = [candidate.qualname for candidate in candidates]
            for expected in case.get('expected_candidates', []):
                assert expected in qualnames
        elif case['kind'] == 'context':
            context = services.context(case['qualname'])
            neighbor_qualnames = [neighbor.qualname for neighbor in context.neighbors]
            for expected in case.get('expected_neighbors', []):
                assert expected in neighbor_qualnames
            related_test_qualnames = [test.qualname for test in context.related_tests]
            if case.get('expected_related_test'):
                assert case['expected_related_test'] in related_test_qualnames
            inbound_sources = [relation.source_qualname for relation in context.inbound_relations]
            for expected in case.get('expected_inbound_relations', []):
                assert expected in inbound_sources
            outbound_targets = [relation.target_qualname or relation.target_ref for relation in context.outbound_relations]
            for expected in case.get('expected_outbound_relations', []):
                assert any(expected in target for target in outbound_targets)
        elif case['kind'] == 'apply':
            artifact_file = REPO_ROOT / case['artifact_file']
            result = services.apply(
                PatchArtifact(
                    target_qualname=case['qualname'],
                    replacement_code=artifact_file.read_text(encoding='utf-8'),
                    operation=case['operation'],
                )
            )
            assert result.validation.is_valid is True
            if case.get('expected_related_test'):
                assert case['expected_related_test'] in result.impact.related_tests
            for expected in case.get('expected_impacted_callers', []):
                assert expected in result.impact.inbound_callers
            if case.get('expected_new_symbol'):
                assert case['expected_new_symbol'] in result.impact.symbols_in_changed_files
        else:
            raise AssertionError(f"Unknown golden case kind: {case['kind']}")
