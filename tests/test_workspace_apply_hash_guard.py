from __future__ import annotations

import json
from pathlib import Path

import pytest

from codecollector.config import load_config
from codecollector.workspace.workspace_service import (
    WORKSPACE_BASELINE_FILENAME,
    ResolvedWorkspace,
    WorkspaceApplyConflictError,
    WorkspaceService,
)


def _resolved(tmp_path: Path, project_root: Path, workspace_path: Path) -> ResolvedWorkspace:
    return ResolvedWorkspace(
        workspace_id='ws-1',
        workspace_path=workspace_path,
        session_id=None,
        project_id='proj-1',
        project_root=project_root,
        run_id=None,
    )


def test_workspace_hash_guard_allows_unchanged_base_file(tmp_path: Path) -> None:
    project_root = tmp_path / 'project'
    workspace = tmp_path / 'workspace'
    project_root.mkdir()
    workspace.mkdir()
    project_file = project_root / 'app.py'
    project_file.write_text('def f():\n    return 1\n', encoding='utf-8')

    service = WorkspaceService(tmp_path, load_config())
    digest = service._sha256_file(project_file)
    (workspace / WORKSPACE_BASELINE_FILENAME).write_text(
        json.dumps({
            'version': 1,
            'created_at': '2026-06-11T10:00:00+00:00',
            'files': {
                'app.py': {'exists': True, 'sha256': digest},
            },
        }),
        encoding='utf-8',
    )

    result = service._verify_workspace_base_hashes(
        _resolved(tmp_path, project_root, workspace),
        changed_files=['app.py'],
        excluded_files=set(),
    )

    assert result['status'] == 'ok'
    assert result['checked_files'] == ['app.py']
    assert result['conflicts'] == []


def test_workspace_hash_guard_blocks_changed_base_file(tmp_path: Path) -> None:
    project_root = tmp_path / 'project'
    workspace = tmp_path / 'workspace'
    project_root.mkdir()
    workspace.mkdir()
    project_file = project_root / 'app.py'
    project_file.write_text('def f():\n    return 1\n', encoding='utf-8')

    service = WorkspaceService(tmp_path, load_config())
    old_digest = service._sha256_file(project_file)
    project_file.write_text('def f():\n    return 2\n', encoding='utf-8')
    (workspace / WORKSPACE_BASELINE_FILENAME).write_text(
        json.dumps({
            'version': 1,
            'created_at': '2026-06-11T10:00:00+00:00',
            'files': {
                'app.py': {'exists': True, 'sha256': old_digest},
            },
        }),
        encoding='utf-8',
    )

    with pytest.raises(WorkspaceApplyConflictError) as exc_info:
        service._verify_workspace_base_hashes(
            _resolved(tmp_path, project_root, workspace),
            changed_files=['app.py'],
            excluded_files=set(),
        )

    details = exc_info.value.to_dict()
    assert details['status'] == 'blocked'
    assert details['reason'] == 'project_changed_after_workspace_creation'
    assert details['conflicts'][0]['file'] == 'app.py'
    assert details['conflicts'][0]['reason'] == 'current_file_changed_after_workspace_creation'


def test_workspace_hash_guard_blocks_new_file_created_after_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / 'project'
    workspace = tmp_path / 'workspace'
    project_root.mkdir()
    workspace.mkdir()
    (project_root / 'tests').mkdir()
    (workspace / WORKSPACE_BASELINE_FILENAME).write_text(
        json.dumps({
            'version': 1,
            'created_at': '2026-06-11T10:00:00+00:00',
            'files': {
                'tests/test_generated_target.py': {'exists': False, 'sha256': None},
            },
        }),
        encoding='utf-8',
    )
    (project_root / 'tests/test_generated_target.py').write_text('def test_existing():\n    pass\n', encoding='utf-8')

    service = WorkspaceService(tmp_path, load_config())
    with pytest.raises(WorkspaceApplyConflictError) as exc_info:
        service._verify_workspace_base_hashes(
            _resolved(tmp_path, project_root, workspace),
            changed_files=['tests/test_generated_target.py'],
            excluded_files=set(),
        )

    assert exc_info.value.to_dict()['conflicts'][0]['reason'] == 'current_file_created_after_workspace_creation'


def test_workspace_hash_guard_skips_old_workspace_without_baseline(tmp_path: Path) -> None:
    project_root = tmp_path / 'project'
    workspace = tmp_path / 'workspace'
    project_root.mkdir()
    workspace.mkdir()

    service = WorkspaceService(tmp_path, load_config())
    result = service._verify_workspace_base_hashes(
        _resolved(tmp_path, project_root, workspace),
        changed_files=['app.py'],
        excluded_files=set(),
    )

    assert result['status'] == 'skipped'
    assert result['reason'] == 'missing_workspace_baseline'
    assert result['unprotected_files'] == ['app.py']


def test_workspace_baseline_metadata_is_skipped_path(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path, load_config())

    assert service._is_skipped_path(WORKSPACE_BASELINE_FILENAME) is True
