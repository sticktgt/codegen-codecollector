from pathlib import Path

from codecollector.orchestration.pipeline_service import PipelineService


def test_remove_rejected_generated_test_files_deletes_only_safe_relative_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    generated_test = workspace / "tests" / "test_generated_bad.py"
    generated_test.parent.mkdir(parents=True)
    generated_test.write_text("def broken(:\n", encoding="utf-8")

    service = object.__new__(PipelineService)

    removed = service._remove_rejected_generated_test_files(
        workspace,
        ["tests/test_generated_bad.py", "../outside.py", "/tmp/outside.py", ""],
    )

    assert removed == ["tests/test_generated_bad.py"]
    assert not generated_test.exists()


def test_remove_rejected_generated_test_files_ignores_missing_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    service = object.__new__(PipelineService)

    removed = service._remove_rejected_generated_test_files(
        workspace,
        ["tests/test_generated_missing.py"],
    )

    assert removed == []
