from codecollector.domain.models import ChangeRequest
from codecollector.validation.semantic_checks import validate_patch_static_semantics


def _block(patched_method: str):
    original = (
        "class Service:\n"
        "    def load(self, note_id: str):\n"
        "        return note_id\n\n"
        "    def search(self):\n"
        "        raise NotImplementedError\n"
    )
    patched = (
        "class Service:\n"
        "    def load(self, note_id: str):\n"
        "        return note_id\n\n"
        f"{patched_method}\n"
    )
    return validate_patch_static_semantics(
        requested_operation="replace_symbol",
        change_request=ChangeRequest(title="Search", description="Implement search", project="demo"),
        target_qualname="pkg.mod.Service.search",
        original_file_text=original,
        patched_file_text=patched,
        changed_files=["pkg/mod.py"],
        target_file="pkg/mod.py",
        parent_qualname="pkg.mod.Service",
    )


def test_rejects_missing_required_arg_for_same_class_method_call():
    block = _block(
        "    def search(self):\n"
        "        return self.load()\n"
    )
    assert not block.ok
    assert any(issue.code == "self_method_call_missing_required_args" for issue in block.issues)


def test_rejects_unknown_keyword_for_same_class_method_call():
    block = _block(
        "    def search(self):\n"
        "        return self.load(path='x')\n"
    )
    assert not block.ok
    assert any(issue.code == "self_method_call_unknown_keyword_arg" for issue in block.issues)


def test_accepts_valid_same_class_method_call():
    block = _block(
        "    def search(self):\n"
        "        return self.load('note-1')\n"
    )
    assert block.ok
